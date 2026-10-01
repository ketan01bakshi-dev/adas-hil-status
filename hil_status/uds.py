"""UDS (ISO 14229-1) client for the two read-only services the status check needs:
TesterPresent (0x3E) and ReadDataByIdentifier (0x22). Transport-agnostic: works
on anything with send(bytes) / recv(timeout) -> bytes | None (ISO-TP or DoIP).
"""

from __future__ import annotations

import time
from typing import Protocol

# Standard identification DIDs (ISO 14229-1 Annex C). OEMs pick which ones they fill;
# the rig config decides which DID answers "SW version" / "HW version" for each ECU.
STANDARD_DIDS = {
    "spare_part_number": 0xF187,
    "vm_sw_number": 0xF188,
    "vm_sw_version": 0xF189,
    "supplier_id": 0xF18A,
    "serial_number": 0xF18C,
    "vin": 0xF190,
    "vm_hw_number": 0xF191,
    "supplier_hw_number": 0xF192,
    "supplier_hw_version": 0xF193,
    "supplier_sw_number": 0xF194,
    "supplier_sw_version": 0xF195,
}

DEFAULT_DIDS = {"sw_version": 0xF195, "hw_version": 0xF193, "serial_number": 0xF18C}

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7F: "serviceNotSupportedInActiveSession",
}


class Transport(Protocol):
    def send(self, payload: bytes) -> None: ...
    def recv(self, timeout: float) -> bytes | None: ...


class UdsError(RuntimeError):
    pass


class UdsTimeout(UdsError):
    pass


class UdsNegativeResponse(UdsError):
    def __init__(self, sid: int, nrc: int):
        self.sid, self.nrc = sid, nrc
        super().__init__(f"NRC 0x{nrc:02X} {NRC_NAMES.get(nrc, 'unknown')} for service 0x{sid:02X}")


def decode_did(raw: bytes) -> str:
    """Identification DIDs are normally ASCII padded with 0x00/0xFF/spaces; fall back to hex."""
    text = raw.rstrip(b"\x00\xff ").lstrip(b"\x00 ")
    if text and all(0x20 <= b < 0x7F for b in text):
        return text.decode("ascii")
    return raw.hex(" ").upper()


class UdsClient:
    def __init__(self, transport: Transport, p2: float = 1.0, p2_star: float = 5.0):
        self.transport, self.p2, self.p2_star = transport, p2, p2_star

    def request(self, req: bytes) -> bytes:
        sid = req[0]
        self.transport.send(req)
        timeout = self.p2
        while True:
            resp = self.transport.recv(timeout)
            if resp is None:
                raise UdsTimeout(f"no response to service 0x{sid:02X} within {timeout:.1f} s")
            if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == sid:
                if resp[2] == 0x78:  # response pending: ECU asks for more time (P2*)
                    timeout = self.p2_star
                    continue
                raise UdsNegativeResponse(sid, resp[2])
            if resp and resp[0] == sid + 0x40:
                return resp
            raise UdsError(f"unexpected response {resp.hex(' ')} to service 0x{sid:02X}")

    def tester_present(self) -> float:
        """Return round-trip time in milliseconds."""
        t0 = time.perf_counter()
        self.request(bytes([0x3E, 0x00]))
        return (time.perf_counter() - t0) * 1000

    def read_did(self, did: int) -> bytes:
        resp = self.request(bytes([0x22, did >> 8, did & 0xFF]))
        if len(resp) < 3 or (resp[1] << 8 | resp[2]) != did:
            raise UdsError(f"response echoes wrong DID: {resp[:3].hex(' ')}")
        return resp[3:]
