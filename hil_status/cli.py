"""Command line: python -m hil_status --config config/rig.toml [--json out.json] [--html out.html]

Exit code: 0 PASS, 1 WARN, 2 FAIL, 3 tool/config error. A test job can run this
before a campaign and refuse to start on anything but 0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .inventory import InventoryError, load_inventory
from .model import EXIT_CODES
from .report import to_console, to_html, to_json
from .rig import XilRig
from .sim import SimRig
from .status import collect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hil_status", description="ADAS HIL bench status and ECU versions")
    ap.add_argument("--config", default="config/rig.toml", help="rig config (TOML)")
    ap.add_argument("--inventory", help="ECU inventory text file (overrides [rig] inventory)")
    ap.add_argument("--backend", choices=("sim", "xil"), help="override [rig] backend")
    ap.add_argument("--json", type=Path, help="write the status as JSON")
    ap.add_argument("--html", type=Path, help="write an HTML dashboard")
    ap.add_argument("--quiet", action="store_true", help="no console table")
    ap.add_argument("--serve", action="store_true", help="run the live dashboard instead of a single check")
    ap.add_argument("--host", default="127.0.0.1", help="dashboard bind address (default: this PC only)")
    ap.add_argument("--port", type=int, default=8790, help="dashboard port")
    ap.add_argument("--interval", type=int, default=15, help="seconds between checks in --serve mode")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.backend:
            cfg.backend = args.backend
        inv_path = Path(args.inventory) if args.inventory else cfg.base_dir / cfg.inventory
        if not args.inventory and not cfg.inventory:
            raise ConfigError("no inventory: pass --inventory or set [rig] inventory")
        inventory = load_inventory(inv_path)
    except (OSError, ConfigError, InventoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    backend = SimRig(cfg, inventory) if cfg.backend == "sim" else XilRig(cfg)
    if args.serve:
        from .server import serve

        serve(cfg, inv_path, backend, args.host, args.port, max(2, args.interval))
        return 0

    status = collect(cfg, inventory, backend, inventory_file=str(inv_path))

    if not args.quiet:
        print(to_console(status))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(to_json(status), encoding="utf-8")
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(to_html(status), encoding="utf-8")
    return EXIT_CODES[status.overall]


if __name__ == "__main__":
    raise SystemExit(main())
