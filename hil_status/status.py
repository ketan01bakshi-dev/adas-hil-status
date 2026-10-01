"""Collect the bench status: rig state, CAN bus health, and per-ECU power / link / versions.

Expected versions come from the inventory text file; actual versions are read live over
UDS where the ECU supports it, and any difference is reported as VERSION_MISMATCH.
Everything here is read-only: TesterPresent and ReadDataByIdentifier in the default session.
"""

from __future__ import annotations

from datetime import datetime

import can

from .config import EcuAccess, RigConfig
from .doip import DoipError, DoipTransport, tcp_probe
from .isotp_can import IsoTpCan, IsoTpError, discover_responders
from .model import EcuState, EcuStatus, InventoryEntry, Overall, RigStatus, VERSION_FIELDS
from .rig import RigBackend
from .uds import UdsClient, UdsError, UdsNegativeResponse, UdsTimeout, decode_did

FAILING = {EcuState.NO_RESPONSE, EcuState.NOT_POWERED, EcuState.ERROR}


def _same_version(expected: str, actual: str) -> bool:
    return expected.strip().casefold() == actual.strip().casefold()


def _read_identification(uds: UdsClient, acc: EcuAccess, st: EcuStatus, inv: InventoryEntry) -> None:
    unread = []
    for field, did in acc.dids.items():
        try:
            st.actual[field] = decode_did(uds.read_did(did))
        except UdsNegativeResponse as exc:
            unread.append(f"{field} (DID 0x{did:04X}): {exc}")
        except UdsTimeout:
            unread.append(f"{field} (DID 0x{did:04X}): timeout")

    for field in VERSION_FIELDS:
        expected = getattr(inv, field)
        actual = st.actual.get(field)
        if expected and actual is not None and not _same_version(expected, actual):
            st.mismatches.append(f"{field}: inventory '{expected}', ECU reports '{actual}'")

    st.state = EcuState.VERSION_MISMATCH if st.mismatches else EcuState.OK
    st.detail = "; ".join(["read failed: " + u for u in unread]) if unread else "versions read over UDS"


def _probe_uds(uds_factory, acc: EcuAccess, st: EcuStatus, inv: InventoryEntry, p2: float, p2_star: float) -> None:
    try:
        with uds_factory() as transport:
            uds = UdsClient(transport, p2=p2, p2_star=p2_star)
            st.response_ms = round(uds.tester_present(), 1)
            _read_identification(uds, acc, st, inv)
    except UdsTimeout:
        st.state, st.detail = EcuState.NO_RESPONSE, "no answer to TesterPresent"
    except OSError as exc:  # DoIP: connection refused / host unreachable / timeout
        st.state, st.detail = EcuState.NO_RESPONSE, f"no DoIP connection: {exc}"
    except (UdsError, IsoTpError, DoipError) as exc:
        st.state, st.detail = EcuState.ERROR, str(exc)


class _NoClose:
    """Context wrapper so a shared CAN ISO-TP channel can use the same `with` path as DoIP."""

    def __init__(self, transport):
        self.transport = transport

    def __enter__(self):
        return self.transport

    def __exit__(self, *exc):
        return False


def probe_ecu(
    inv: InventoryEntry,
    cfg: RigConfig,
    backend: RigBackend,
    bus: can.BusABC | None,
) -> EcuStatus:
    acc = cfg.access_for(inv.name)
    st = EcuStatus(
        name=inv.name,
        ecu_type=inv.ecu_type,
        transport=acc.transport if acc else "-",
        address=acc.address if acc else "-",
        state=EcuState.NOT_PROBED,
        expected={f: getattr(inv, f) for f in VERSION_FIELDS if getattr(inv, f)},
        extra=inv.extra,
    )
    if acc is None:
        st.detail = f"no [ecus.{inv.name}] entry in rig config; versions from inventory only"
        return st

    # 1. Power: supply current measured by the HIL model (load/power-switch channel).
    if acc.current_var:
        try:
            current = backend.read_current(acc)
        except Exception as exc:  # XIL read failed: keep probing the bus, note it
            st.detail = f"supply current unreadable: {exc}; "
        else:
            if current is not None:
                st.supply_current_a = round(current, 3)
                st.powered = current >= acc.min_current_a
                if not st.powered:
                    st.state = EcuState.NOT_POWERED
                    st.detail = f"supply current {current:.2f} A < {acc.min_current_a:.2f} A threshold"
                    return st

    # 2. Link + 3. versions
    prefix = st.detail
    if acc.transport == "none":
        st.state = EcuState.OK if st.powered else EcuState.NOT_PROBED
        st.detail = prefix + "power check only; versions from inventory"
    elif acc.transport == "tcp":
        try:
            st.response_ms = round(tcp_probe(acc.host, acc.port), 1)
            st.state, st.detail = EcuState.OK, prefix + "Ethernet reachable; no UDS, versions from inventory"
        except OSError as exc:
            st.state, st.detail = EcuState.NO_RESPONSE, prefix + f"not reachable: {exc}"
    elif acc.transport == "can":
        if bus is None:
            st.state, st.detail = EcuState.ERROR, prefix + "diagnostic CAN bus not available"
            return st
        isotp = IsoTpCan(bus, acc.tx_id, acc.rx_id, extended_id=cfg.can.extended_id, padding=cfg.can.padding)
        _probe_uds(lambda: _NoClose(isotp), acc, st, inv, cfg.p2_s, cfg.p2_star_s)
        st.detail = prefix + st.detail
    elif acc.transport == "doip":
        _probe_uds(
            lambda: DoipTransport(
                acc.host, acc.port, cfg.doip_tester_address, acc.logical_address,
                protocol_version=cfg.doip_protocol_version,
            ),
            acc, st, inv, cfg.p2_s, cfg.p2_star_s,
        )
        st.detail = prefix + st.detail
    return st


def _overall(rig_ok: bool | None, ecus: list[EcuStatus], unknown: list[str], notes: list[str]) -> Overall:
    if rig_ok is False or any(e.state in FAILING for e in ecus):
        return Overall.FAIL
    if unknown or any(e.state in (EcuState.VERSION_MISMATCH, EcuState.NOT_PROBED) for e in ecus):
        return Overall.WARN
    if any("read failed" in e.detail for e in ecus) or notes:
        return Overall.WARN
    return Overall.PASS


def collect(
    cfg: RigConfig,
    inventory: list[InventoryEntry],
    backend: RigBackend,
    inventory_file: str = "",
) -> RigStatus:
    notes: list[str] = []
    rig_info: dict[str, object] = {}
    bus_info: dict[str, object] = {}
    rig_ok: bool | None = None
    bus: can.BusABC | None = None
    statuses: list[EcuStatus] = []
    unknown: list[str] = []

    try:
        try:
            backend.open()
            rig_info = backend.rig_info()
            state = str(rig_info.get("simulation_state", ""))
            rig_ok = "RUNNING" in state.upper() if state else None
            if rig_ok is False:
                notes.append(f"HIL simulation is not running ({state})")
        except Exception as exc:
            rig_ok = False
            rig_info = {"error": str(exc)}
            notes.append(f"rig backend '{backend.name}' unavailable: {exc}")

        can_accesses = [a for e in inventory if (a := cfg.access_for(e.name)) and a.transport == "can"]
        if can_accesses:
            kwargs = backend.can_bus_kwargs()
            bus_info = {k: v for k, v in kwargs.items() if k in ("interface", "channel", "bitrate", "fd")}
            try:
                bus = can.Bus(**kwargs)
                bus_info["state"] = getattr(bus.state, "name", str(bus.state))
                if cfg.can.discovery:
                    ids = discover_responders(
                        bus, cfg.can.functional_id, cfg.can.discovery_window_s,
                        extended_id=cfg.can.extended_id, padding=cfg.can.padding,
                    )
                    bus_info["responders"] = [f"0x{i:03X}" for i in sorted(ids)]
                    known = {a.rx_id for a in cfg.ecus.values() if a.transport == "can"}
                    unknown = [f"0x{i:03X}" for i in sorted(ids - known)]
                    if unknown:
                        notes.append(f"CAN IDs answering TesterPresent but not in the rig config: {', '.join(unknown)}")
            except Exception as exc:
                bus = None
                bus_info["error"] = str(exc)
                notes.append(f"cannot open diagnostic CAN bus: {exc}")

        statuses = [probe_ecu(inv, cfg, backend, bus) for inv in inventory]

        listed = {e.name.casefold() for e in inventory}
        for key, acc in cfg.ecus.items():
            if key not in listed:
                notes.append(f"{acc.name} is in the rig config but not in the inventory file (not probed)")
    finally:
        if bus is not None:
            bus.shutdown()
        try:
            backend.close()
        except Exception as exc:
            notes.append(f"rig backend close failed: {exc}")

    return RigStatus(
        rig_name=cfg.name,
        backend=backend.name,
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        inventory_file=inventory_file,
        overall=_overall(rig_ok, statuses, unknown, notes),
        rig=rig_info,
        bus=bus_info,
        ecus=statuses,
        unknown_responders=unknown,
        notes=notes,
    )
