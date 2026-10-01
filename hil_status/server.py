"""Live dashboard: re-checks the bench every N seconds and serves the page on localhost.

    python -m hil_status --config config/rig.toml --serve [--port 8765] [--interval 15]

Standard library only. The rig backend (XIL MAPort / virtual bench) stays open between
checks and is re-opened after a failure; the inventory file is re-read on every check,
so a sheet updated after a flash shows up without a restart.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .campaign import attach_campaign
from .config import RigConfig
from .inventory import InventoryError, load_inventory
from .model import InventoryEntry, RigStatus
from .report import history_point, to_html
from .rig import RigBackend
from .status import collect


class PersistentBackend:
    """Keeps the inner backend open across checks; drops it after an error so the next check re-opens."""

    def __init__(self, inner: RigBackend):
        self.inner, self.name, self.is_open = inner, inner.name, False

    def open(self) -> None:
        if not self.is_open:
            self.inner.open()
            self.is_open = True

    def close(self) -> None:  # collect() calls this after each check: keep the session
        pass

    def shutdown(self) -> None:
        if self.is_open:
            self.is_open = False
            self.inner.close()

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            self.shutdown()
            raise

    def rig_info(self):
        return self._guard(self.inner.rig_info)

    def read_current(self, access):
        return self._guard(self.inner.read_current, access)

    def can_bus_kwargs(self):
        return self.inner.can_bus_kwargs()


class Monitor:
    def __init__(self, cfg: RigConfig, inventory_path: Path, backend: RigBackend, interval_s: int, keep: int = 120):
        self.cfg, self.inventory_path, self.interval_s = cfg, inventory_path, interval_s
        self.backend = PersistentBackend(backend)
        self.history: deque[dict] = deque(maxlen=keep)
        self.status: RigStatus | None = None
        self._inventory: list[InventoryEntry] = []
        self._lock = threading.Lock()  # one check at a time (shared CAN channel / MAPort)
        self._wake = threading.Event()
        self._stop = threading.Event()

    def check(self) -> RigStatus:
        with self._lock:
            note = None
            try:
                self._inventory = load_inventory(self.inventory_path)
            except (OSError, InventoryError) as exc:
                if not self._inventory:
                    raise
                note = f"inventory file unreadable, showing last good copy: {exc}"
            status = collect(self.cfg, self._inventory, self.backend, str(self.inventory_path))
            attach_campaign(self.cfg, status)  # results file re-read every check: live campaign progress
            if note:
                status.notes.insert(0, note)
            self.status = status
            self.history.append(history_point(status))
            return status

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:  # never kill the loop; the page shows staleness
                print(f"check failed: {exc}")
            self._wake.wait(self.interval_s)
            self._wake.clear()

    def refresh_now(self) -> None:
        self.check()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.backend.shutdown()

    def payload(self) -> dict:
        assert self.status is not None
        return {"status": self.status.to_dict(), "history": list(self.history), "live": True, "interval_s": self.interval_s}


def make_handler(mon: Monitor):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if mon.status is None:
                return self._send(503, b"first check still running, reload in a few seconds", "text/plain")
            if path in ("/", "/index.html"):
                html = to_html(mon.status, list(mon.history), live=True, interval_s=mon.interval_s)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/status":
                return self._send(200, json.dumps(mon.payload(), default=str).encode(), "application/json")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if urlsplit(self.path).path != "/api/refresh":
                return self._send(404, b"not found", "text/plain")
            mon.refresh_now()
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")

        def log_message(self, *args):  # keep the console for check results
            pass

    return Handler


def serve(cfg: RigConfig, inventory_path: Path, backend: RigBackend, host: str, port: int, interval_s: int) -> None:
    mon = Monitor(cfg, inventory_path, backend, interval_s)
    mon.check()  # first result before the page is served
    threading.Thread(target=mon.run_forever, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(mon))
    print(f"ADAS HIL dashboard on http://{host}:{port}/  (checks every {interval_s} s, Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        mon.stop()
        time.sleep(0.1)
