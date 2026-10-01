"""Minimal DoIP (ISO 13400-2) client: TCP connect, routing activation, UDS diagnostic messages.

Used for Ethernet ECUs (ADAS domain controller, Ethernet cameras). Read-only use only.
"""

from __future__ import annotations

import socket
import struct
import time

HEADER = struct.Struct(">BBHI")  # version, inverse version, payload type, payload length

GENERIC_NACK = 0x0000
ROUTING_ACTIVATION_REQ = 0x0005
ROUTING_ACTIVATION_RES = 0x0006
ALIVE_CHECK_REQ = 0x0007
ALIVE_CHECK_RES = 0x0008
DIAG_MESSAGE = 0x8001
DIAG_ACK = 0x8002
DIAG_NACK = 0x8003

ROUTING_SUCCESS = 0x10
MAX_PAYLOAD = 1 << 20


class DoipError(RuntimeError):
    pass


def frame(version: int, ptype: int, payload: bytes) -> bytes:
    return HEADER.pack(version, version ^ 0xFF, ptype, len(payload)) + payload


def read_message(sock: socket.socket) -> tuple[int, bytes]:
    def exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise DoipError("connection closed by DoIP entity")
            buf += chunk
        return bytes(buf)

    version, inverse, ptype, length = HEADER.unpack(exact(HEADER.size))
    if version ^ 0xFF != inverse:
        raise DoipError(f"bad DoIP header: version 0x{version:02X}, inverse 0x{inverse:02X}")
    if length > MAX_PAYLOAD:
        raise DoipError(f"DoIP payload too large: {length} bytes")
    return ptype, exact(length)


class DoipTransport:
    def __init__(
        self,
        host: str,
        port: int,
        tester_address: int,
        target_address: int,
        *,
        protocol_version: int = 0x02,
        connect_timeout: float = 2.0,
    ):
        self.host, self.port = host, port
        self.sa, self.ta = tester_address, target_address
        self.version, self.connect_timeout = protocol_version, connect_timeout
        self.sock: socket.socket | None = None

    def __enter__(self) -> "DoipTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._write(ROUTING_ACTIVATION_REQ, struct.pack(">HB4x", self.sa, 0x00))  # 0x00 = default activation
        ptype, payload = self._read(time.monotonic() + self.connect_timeout)
        if ptype != ROUTING_ACTIVATION_RES or len(payload) < 5:
            raise DoipError(f"routing activation: unexpected payload type 0x{ptype:04X}")
        if payload[4] != ROUTING_SUCCESS:
            raise DoipError(f"routing activation denied, code 0x{payload[4]:02X}")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def _write(self, ptype: int, payload: bytes) -> None:
        assert self.sock is not None, "DoIP transport not open"
        self.sock.sendall(frame(self.version, ptype, payload))

    def _read(self, deadline: float) -> tuple[int, bytes]:
        """Next message that is not an alive check (answered transparently). Raises TimeoutError."""
        assert self.sock is not None, "DoIP transport not open"
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.sock.settimeout(left)
            ptype, payload = read_message(self.sock)
            if ptype == ALIVE_CHECK_REQ:
                self._write(ALIVE_CHECK_RES, struct.pack(">H", self.sa))
                continue
            if ptype == GENERIC_NACK:
                raise DoipError(f"generic DoIP NACK, code 0x{payload[0]:02X}")
            return ptype, payload

    def send(self, payload: bytes) -> None:
        self._write(DIAG_MESSAGE, struct.pack(">HH", self.sa, self.ta) + payload)
        try:
            ptype, ack = self._read(time.monotonic() + self.connect_timeout)
        except TimeoutError:
            raise DoipError("no diagnostic message ACK") from None
        if ptype == DIAG_NACK:
            raise DoipError(f"diagnostic message NACK, code 0x{ack[4]:02X}")
        if ptype != DIAG_ACK:
            raise DoipError(f"expected diagnostic ACK, got payload type 0x{ptype:04X}")

    def recv(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                ptype, payload = self._read(deadline)
            except (TimeoutError, socket.timeout):
                return None
            if ptype == DIAG_MESSAGE and struct.unpack(">H", payload[:2])[0] == self.ta:
                return payload[4:]


def tcp_probe(host: str, port: int, timeout: float = 1.0) -> float:
    """Open and close a TCP connection; return connect time in ms. Raises OSError if unreachable."""
    t0 = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return (time.perf_counter() - t0) * 1000
