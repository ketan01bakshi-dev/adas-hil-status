import can
import pytest

from hil_status.doip import DoipTransport
from hil_status.isotp_can import IsoTpCan
from hil_status.sim import SimCanEcu, SimDoipServer
from hil_status.uds import UdsClient, UdsNegativeResponse, UdsTimeout, decode_did

LONG = b"A_VERY_LONG_SOFTWARE_VERSION_STRING_0123456789"  # forces ISO-TP multi-frame (> 2 sequence wraps)


class FakeTransport:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def send(self, payload):
        self.sent.append(payload)

    def recv(self, timeout):
        return self.replies.pop(0) if self.replies else None


def test_uds_waits_through_response_pending():
    t = FakeTransport([bytes([0x7F, 0x22, 0x78]), bytes([0x7F, 0x22, 0x78]), bytes([0x62, 0xF1, 0x95]) + b"SW1"])
    assert UdsClient(t).read_did(0xF195) == b"SW1"


def test_uds_negative_and_timeout():
    with pytest.raises(UdsNegativeResponse, match="0x31 requestOutOfRange"):
        UdsClient(FakeTransport([bytes([0x7F, 0x22, 0x31])])).read_did(0xF195)
    with pytest.raises(UdsTimeout):
        UdsClient(FakeTransport([])).tester_present()


def test_decode_did():
    assert decode_did(b"HW_C2\x00\x00\xff") == "HW_C2"
    assert decode_did(b"\x01\x02") == "01 02"


@pytest.fixture
def can_ecu():
    ecu = SimCanEcu("t_isotp", 0x7E2, 0x7EA, 0x7DF, {0xF195: LONG, 0xF193: b"HW1"}).start()
    bus = can.Bus(interface="virtual", channel="t_isotp")
    yield bus
    bus.shutdown()
    ecu.stop()


def test_isotp_single_and_multi_frame(can_ecu):
    uds = UdsClient(IsoTpCan(can_ecu, 0x7E2, 0x7EA))
    assert uds.tester_present() >= 0
    assert uds.read_did(0xF193) == b"HW1"
    assert uds.read_did(0xF195) == LONG
    with pytest.raises(UdsNegativeResponse):
        uds.read_did(0x1234)


def test_doip_roundtrip():
    server = SimDoipServer(0x1010, {0xF195: LONG}).start()
    try:
        with DoipTransport("127.0.0.1", server.port, 0x0E80, 0x1010) as t:
            uds = UdsClient(t)
            uds.tester_present()
            assert uds.read_did(0xF195) == LONG
    finally:
        server.stop()
