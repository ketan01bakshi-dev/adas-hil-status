"""Data types shared by the inventory parser, the collector and the reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class EcuState(str, Enum):
    OK = "OK"
    VERSION_MISMATCH = "VERSION_MISMATCH"  # answers, but reports other versions than the inventory
    NO_RESPONSE = "NO_RESPONSE"  # powered (or power unknown) but silent on its bus
    NOT_POWERED = "NOT_POWERED"  # supply current below threshold: not connected / switched off
    NOT_PROBED = "NOT_PROBED"  # in the inventory, but no access entry in the rig config
    ERROR = "ERROR"  # the probe itself failed (bad config, protocol error)


class Overall(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# Exit codes for the CLI, so a test-automation job can gate on the bench state.
EXIT_CODES = {Overall.PASS: 0, Overall.WARN: 1, Overall.FAIL: 2}

VERSION_FIELDS = ("sw_version", "hw_version")


@dataclass
class InventoryEntry:
    """One ECU line/block from the bench inventory text file."""

    name: str
    ecu_type: str = ""
    sw_version: str = ""
    hw_version: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    line: int = 0


@dataclass
class EcuStatus:
    name: str
    ecu_type: str
    transport: str
    address: str
    state: EcuState
    powered: bool | None = None
    supply_current_a: float | None = None
    response_ms: float | None = None
    expected: dict[str, str] = field(default_factory=dict)  # from the inventory file
    actual: dict[str, str] = field(default_factory=dict)  # read live from the ECU
    mismatches: list[str] = field(default_factory=list)
    detail: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def connected(self) -> bool:
        return self.state in (EcuState.OK, EcuState.VERSION_MISMATCH)


@dataclass
class RigStatus:
    rig_name: str
    backend: str
    timestamp: str
    inventory_file: str
    overall: Overall
    rig: dict[str, object] = field(default_factory=dict)
    bus: dict[str, object] = field(default_factory=dict)
    ecus: list[EcuStatus] = field(default_factory=list)
    unknown_responders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["overall"] = self.overall.value
        for ecu, src in zip(data["ecus"], self.ecus):
            ecu["state"] = src.state.value
            ecu["connected"] = src.connected
        return data
