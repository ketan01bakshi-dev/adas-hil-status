"""Virtual ADAS bench: simulated ECUs that answer real UDS over a python-can virtual bus,
DoIP over localhost TCP, and a TCP-only LIDAR. Lets the whole tool run with no hardware.

Faults come from [sim.ecus.<NAME>] in the rig config:
    powered = false        supply current 0 A, ECU silent
    responsive = false     powered but silent (crashed / wrong bus / bootloader hang)
    sw_version = "..."     what the ECU really reports (default: the inventory value)
    hw_version = "..."
    current_a = 0.9        simulated supply current when powered
[sim] unknown_responders = [0x7EE]   extra CAN IDs answering functional TesterPresent.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import zlib
from dataclasses import replace

import can

from . import doip
from .config import EcuAccess, RigConfig
from .model import InventoryEntry


def uds_respond(dids: dict[int, bytes], req: bytes, functional: bool) -> bytes | None:
    if not req:
        return None
    sid = req[0]
    if sid == 0x3E:
        if len(req) != 2:
            return bytes([0x7F, 0x3E, 0x13])
        return None if req[1] & 0x80 else bytes([0x7E, 0x00])
    if functional:
        return None
    if sid == 0x22:
        if len(req) != 3:
            return bytes([0x7F, 0x22, 0x13])
        did = req[1] << 8 | req[2]
        if did in dids:
            return bytes([0x62, req[1], req[2]]) + dids[did]
        return bytes([0x7F, 0x22, 0x31])
    return bytes([0x7F, sid, 0x11])


class _Worker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):  # pragma: no cover - overridden
        raise NotImplementedError


class SimCanEcu(_Worker):
    def __init__(self, channel: str, req_id: int | None, resp_id: int, functional_id: int, dids: dict[int, bytes]):
        super().__init__()
        self.req_id, self.resp_id, self.functional_id, self.dids = req_id, resp_id, functional_id, dids
        self.bus = can.Bus(interface="virtual", channel=channel)  # created now: listening before start() returns

    def _send(self, data: bytes) -> None:
        self.bus.send(can.Message(arbitration_id=self.resp_id, data=data + b"\xcc" * (8 - len(data)), is_extended_id=False))

    def _send_isotp(self, payload: bytes) -> None:
        if len(payload) <= 7:
            self._send(bytes([len(payload)]) + payload)
            return
        self._send(bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6])
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:  # wait for the tester's flow control
            msg = self.bus.recv(timeout=0.1)
            if msg and msg.arbitration_id == self.req_id and msg.data[0] >> 4 == 0x3:
                break
        else:
            return
        rest, seq = payload[6:], 1
        while rest:
            self._send(bytes([0x20 | seq]) + rest[:7])
            rest, seq = rest[7:], (seq + 1) & 0x0F

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                msg = self.bus.recv(timeout=0.05)
                if msg is None or msg.arbitration_id not in (self.req_id, self.functional_id):
                    continue
                d = bytes(msg.data)
                if not d or d[0] >> 4 != 0:
                    continue
                resp = uds_respond(self.dids, d[1 : 1 + (d[0] & 0x0F)], msg.arbitration_id == self.functional_id)
                if resp:
                    self._send_isotp(resp)
        finally:
            self.bus.shutdown()


class SimDoipServer(_Worker):
    def __init__(self, logical_address: int, dids: dict[int, bytes], version: int = 0x02):
        super().__init__()
        self.la, self.dids, self.version = logical_address, dids, version
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        with conn:
            try:
                while not self._stop.is_set():
                    ptype, payload = doip.read_message(conn)
                    if ptype == doip.ROUTING_ACTIVATION_REQ:
                        sa = struct.unpack(">H", payload[:2])[0]
                        conn.sendall(doip.frame(self.version, doip.ROUTING_ACTIVATION_RES,
                                                struct.pack(">HHB4x", sa, self.la, doip.ROUTING_SUCCESS)))
                    elif ptype == doip.DIAG_MESSAGE:
                        sa, ta = struct.unpack(">HH", payload[:4])
                        if ta != self.la:
                            conn.sendall(doip.frame(self.version, doip.DIAG_NACK, struct.pack(">HHB", self.la, sa, 0x03)))
                            continue
                        conn.sendall(doip.frame(self.version, doip.DIAG_ACK, struct.pack(">HHB", self.la, sa, 0x00)))
                        resp = uds_respond(self.dids, payload[4:], functional=False)
                        if resp:
                            conn.sendall(doip.frame(self.version, doip.DIAG_MESSAGE, struct.pack(">HH", self.la, sa) + resp))
            except (OSError, doip.DoipError):
                return

    def _run(self) -> None:
        with self.sock:
            while not self._stop.is_set():
                try:
                    conn, _ = self.sock.accept()
                except TimeoutError:
                    continue
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()


class SimTcpDevice(_Worker):
    """Accept-and-close listener: stands in for a LIDAR's Ethernet service port."""

    def __init__(self):
        super().__init__()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]

    def _run(self) -> None:
        with self.sock:
            while not self._stop.is_set():
                try:
                    conn, _ = self.sock.accept()
                    conn.close()
                except TimeoutError:
                    continue


def _closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


class SimRig:
    """Virtual bench. On open() it rewrites every Ethernet endpoint in cfg to localhost,
    so a sim run can never touch a real IP address."""

    name = "sim"

    def __init__(self, cfg: RigConfig, inventory: list[InventoryEntry]):
        self.cfg, self.inventory = cfg, inventory
        self.channel = f"adas_hil_sim_{id(self):x}"
        self.workers: list[_Worker] = []
        self.currents: dict[str, float] = {}

    def open(self) -> None:
        faults = {k.casefold(): v for k, v in self.cfg.sim.get("ecus", {}).items()}
        for entry in self.inventory:
            key = entry.name.casefold()
            acc = self.cfg.access_for(entry.name)
            if acc is None:
                continue
            f = faults.get(key, {})
            powered = f.get("powered", True)
            alive = powered and f.get("responsive", True)
            self.currents[key] = f.get("current_a", 0.85) if powered else 0.0

            values = {
                "sw_version": f.get("sw_version", entry.sw_version),
                "hw_version": f.get("hw_version", entry.hw_version),
                "serial_number": f.get("serial_number", f"SN{zlib.crc32(entry.name.encode()):08X}"),
            }
            dids = {acc.dids[k]: v.encode() for k, v in values.items() if k in acc.dids and v}
            self._add(acc, key, dids, alive)

        for resp_id in self.cfg.sim.get("unknown_responders", []):
            self.workers.append(SimCanEcu(self.channel, None, resp_id, self.cfg.can.functional_id, {}).start())

    def _add(self, acc: EcuAccess, key: str, dids: dict[int, bytes], alive: bool) -> None:
        if acc.transport == "can":
            if alive:
                self.workers.append(SimCanEcu(self.channel, acc.tx_id, acc.rx_id, self.cfg.can.functional_id, dids).start())
            return
        if acc.transport not in ("doip", "tcp"):
            return
        port = _closed_port()
        if alive and acc.transport == "doip":
            server = SimDoipServer(acc.logical_address, dids, self.cfg.doip_protocol_version).start()
            self.workers.append(server)
            port = server.port
        elif alive:
            device = SimTcpDevice().start()
            self.workers.append(device)
            port = device.port
        self.cfg.ecus[key] = replace(acc, host="127.0.0.1", port=port)

    def close(self) -> None:
        for w in self.workers:
            w.stop()
        self.workers.clear()

    def rig_info(self) -> dict[str, object]:
        return {
            "simulation_state": self.cfg.sim.get("simulation_state", "RUNNING"),
            "application": "virtual ADAS bench (no hardware)",
            "kl30_voltage_v": self.cfg.sim.get("kl30_voltage_v", 13.5),
            "kl15": "ON",
        }

    def read_current(self, access: EcuAccess) -> float | None:
        if not access.current_var:
            return None
        return self.currents.get(access.name.casefold(), 0.0)

    def can_bus_kwargs(self) -> dict[str, object]:
        return {"interface": "virtual", "channel": self.channel}
