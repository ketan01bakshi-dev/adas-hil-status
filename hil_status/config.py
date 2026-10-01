"""Rig configuration (TOML): how to reach each ECU and where the dSPACE model exposes power.

The inventory text file says WHAT should be on the bench (names + versions);
this file says HOW to reach each ECU. They are joined by ECU name (case-insensitive).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .uds import DEFAULT_DIDS, STANDARD_DIDS

TRANSPORTS = ("can", "doip", "tcp", "none")


class ConfigError(ValueError):
    pass


@dataclass
class EcuAccess:
    name: str
    transport: str = "none"
    # CAN / ISO-TP (physical addressing)
    tx_id: int | None = None
    rx_id: int | None = None
    # DoIP / TCP
    host: str = ""
    port: int = 13400
    logical_address: int | None = None
    # Power, read from the dSPACE model through XIL API (variable path); None = not measured
    current_var: str | None = None
    min_current_a: float = 0.05
    dids: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_DIDS))

    @property
    def address(self) -> str:
        if self.transport == "can" and self.tx_id is not None:
            return f"CAN 0x{self.tx_id:03X}/0x{self.rx_id:03X}"
        if self.transport == "doip":
            return f"DoIP {self.host}:{self.port} LA 0x{self.logical_address or 0:04X}"
        if self.transport == "tcp":
            return f"TCP {self.host}:{self.port}"
        return "-"


@dataclass
class CanSettings:
    interface: str = "virtual"
    channel: str = "0"
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: int | None = None
    extended_id: bool = False
    padding: int = 0xCC
    functional_id: int = 0x7DF
    discovery: bool = True
    discovery_window_s: float = 0.3


@dataclass
class XilSettings:
    vendor: str = "dSPACE GmbH"
    product: str = "XIL API"
    product_version: str = ""
    port_name: str = "hil_status"
    port_config: str = ""
    assemblies: list[str] = field(default_factory=list)
    simulation_state_var: str | None = None
    kl30_voltage_var: str | None = None
    kl15_var: str | None = None
    extra_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class RigConfig:
    name: str = "ADAS HIL"
    backend: str = "sim"
    inventory: str = ""
    p2_s: float = 1.0
    p2_star_s: float = 5.0
    doip_tester_address: int = 0x0E80
    doip_protocol_version: int = 0x02
    can: CanSettings = field(default_factory=CanSettings)
    xil: XilSettings = field(default_factory=XilSettings)
    ecus: dict[str, EcuAccess] = field(default_factory=dict)  # key: casefolded name
    sim: dict = field(default_factory=dict)
    campaign: dict = field(default_factory=dict)  # name, release, results, requirements (paths relative to config)
    base_dir: Path = Path(".")

    def access_for(self, name: str) -> EcuAccess | None:
        return self.ecus.get(name.casefold())


def _did(value: int | str) -> int:
    if isinstance(value, int):
        return value
    if value in STANDARD_DIDS:
        return STANDARD_DIDS[value]
    return int(value, 0)


def _pick(cls, data: dict, where: str):
    known = cls.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"[{where}] unknown keys: {', '.join(sorted(unknown))}")
    return cls(**data)


def parse_config(data: dict, base_dir: Path = Path(".")) -> RigConfig:
    rig = dict(data.get("rig", {}))
    doip = data.get("doip", {})
    cfg = RigConfig(
        name=rig.pop("name", "ADAS HIL"),
        backend=rig.pop("backend", "sim"),
        inventory=rig.pop("inventory", ""),
        p2_s=rig.pop("p2_s", 1.0),
        p2_star_s=rig.pop("p2_star_s", 5.0),
        doip_tester_address=doip.get("tester_address", 0x0E80),
        doip_protocol_version=doip.get("protocol_version", 0x02),
        can=_pick(CanSettings, data.get("can", {}), "can"),
        xil=_pick(XilSettings, data.get("xil", {}), "xil"),
        sim=data.get("sim", {}),
        campaign=data.get("campaign", {}),
        base_dir=base_dir,
    )
    if cfg.campaign and not {"results", "requirements"} <= set(cfg.campaign):
        raise ConfigError("[campaign] needs both results and requirements")
    if rig:
        raise ConfigError(f"[rig] unknown keys: {', '.join(sorted(rig))}")
    if cfg.backend not in ("sim", "xil"):
        raise ConfigError(f"[rig] backend must be 'sim' or 'xil', not {cfg.backend!r}")

    for name, raw in data.get("ecus", {}).items():
        raw = dict(raw)
        dids = dict(DEFAULT_DIDS)
        dids.update({k: _did(v) for k, v in raw.pop("dids", {}).items()})
        acc = _pick(EcuAccess, {"name": name, **raw, "dids": dids}, f"ecus.{name}")
        if acc.transport not in TRANSPORTS:
            raise ConfigError(f"[ecus.{name}] transport must be one of {TRANSPORTS}")
        if acc.transport == "can" and (acc.tx_id is None or acc.rx_id is None):
            raise ConfigError(f"[ecus.{name}] CAN transport needs tx_id and rx_id")
        if acc.transport == "doip" and (not acc.host or acc.logical_address is None):
            raise ConfigError(f"[ecus.{name}] DoIP transport needs host and logical_address")
        if acc.transport == "tcp" and not acc.host:
            raise ConfigError(f"[ecus.{name}] TCP transport needs host")
        cfg.ecus[name.casefold()] = acc
    return cfg


def load_config(path: str | Path) -> RigConfig:
    path = Path(path)
    with path.open("rb") as fh:
        return parse_config(tomllib.load(fh), base_dir=path.parent)
