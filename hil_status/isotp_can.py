"""Minimal ISO 15765-2 (ISO-TP) transport over python-can, classic 8-byte frames.

Enough for diagnostic reads: requests up to 7 bytes (single frame), responses of
any length (first frame + flow control + consecutive frames). No dependency
beyond python-can, so it runs on Vector, PEAK, Kvaser or a virtual bus alike.
"""

from __future__ import annotations

import time

import can


class IsoTpError(RuntimeError):
    pass


def _pad(data: bytes, padding: int) -> bytes:
    return data + bytes([padding]) * (8 - len(data))


class IsoTpCan:
    def __init__(
        self,
        bus: can.BusABC,
        tx_id: int,
        rx_id: int,
        *,
        extended_id: bool = False,
        padding: int = 0xCC,
        cf_timeout: float = 1.0,
    ):
        self.bus, self.tx_id, self.rx_id = bus, tx_id, rx_id
        self.extended_id, self.padding, self.cf_timeout = extended_id, padding, cf_timeout

    def _send_frame(self, data: bytes) -> None:
        self.bus.send(
            can.Message(arbitration_id=self.tx_id, data=_pad(data, self.padding), is_extended_id=self.extended_id)
        )

    def _recv_frame(self, deadline: float) -> bytes | None:
        while (left := deadline - time.monotonic()) > 0:
            msg = self.bus.recv(timeout=left)
            if msg is None:
                return None
            if msg.arbitration_id == self.rx_id and not msg.is_error_frame and msg.dlc > 0:
                return bytes(msg.data)
        return None

    def send(self, payload: bytes) -> None:
        if not 0 < len(payload) <= 7:
            raise IsoTpError("only single-frame requests (1..7 bytes) are supported")
        self._send_frame(bytes([len(payload)]) + payload)

    def recv(self, timeout: float) -> bytes | None:
        """Return one complete ISO-TP payload, or None if nothing arrived in time."""
        frame = self._recv_frame(time.monotonic() + timeout)
        if frame is None:
            return None
        pci = frame[0] >> 4
        if pci == 0x0:  # single frame
            return frame[1 : 1 + (frame[0] & 0x0F)]
        if pci != 0x1:
            raise IsoTpError(f"unexpected frame type 0x{pci:X} from 0x{self.rx_id:X}")

        size = ((frame[0] & 0x0F) << 8) | frame[1]
        data = bytearray(frame[2:8])
        self._send_frame(bytes([0x30, 0x00, 0x00]))  # flow control: continue, no block limit, no STmin
        seq = 1
        while len(data) < size:
            cf = self._recv_frame(time.monotonic() + self.cf_timeout)
            if cf is None:
                raise IsoTpError(f"timeout waiting for consecutive frame {seq} from 0x{self.rx_id:X}")
            if cf[0] >> 4 != 0x2 or (cf[0] & 0x0F) != seq:
                raise IsoTpError(f"bad consecutive frame {cf[0]:#04x}, expected sequence {seq}")
            data += cf[1:8]
            seq = (seq + 1) & 0x0F
        return bytes(data[:size])


def discover_responders(
    bus: can.BusABC,
    functional_id: int,
    window_s: float,
    *,
    extended_id: bool = False,
    padding: int = 0xCC,
) -> set[int]:
    """Send a functional TesterPresent and return the CAN IDs that answered.

    TesterPresent (0x3E 0x00) is a no-op in every session, so this is safe on a
    running bench. Requires the ECUs to accept functional requests (most do).
    """
    bus.send(
        can.Message(
            arbitration_id=functional_id,
            data=_pad(bytes([0x02, 0x3E, 0x00]), padding),
            is_extended_id=extended_id,
        )
    )
    seen: set[int] = set()
    deadline = time.monotonic() + window_s
    while (left := deadline - time.monotonic()) > 0:
        msg = bus.recv(timeout=left)
        if msg is None:
            break
        d = bytes(msg.data)
        positive = len(d) >= 2 and d[0] >> 4 == 0 and d[1] == 0x7E
        negative = len(d) >= 3 and d[0] >> 4 == 0 and d[1] == 0x7F and d[2] == 0x3E
        if positive or negative:
            seen.add(msg.arbitration_id)
    return seen
