"""Rig backends: where simulation state and ECU supply currents come from.

`XilRig` talks to a dSPACE HIL (SCALEXIO / DS1006 / MicroAutoBox) through the ASAM XIL
API .NET implementation that ships with dSPACE, via pythonnet. It is strictly read-only:
it attaches to the running application and reads model variables. It never starts or
stops the simulation, never downloads an application and never switches power.

`SimRig` (in sim.py) is a virtual bench with the same interface for laptops and CI.
"""

from __future__ import annotations

from typing import Protocol

from .config import EcuAccess, RigConfig


class RigError(RuntimeError):
    pass


class RigBackend(Protocol):
    name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def rig_info(self) -> dict[str, object]: ...
    def read_current(self, access: EcuAccess) -> float | None: ...
    def can_bus_kwargs(self) -> dict[str, object]: ...


def real_can_bus_kwargs(cfg: RigConfig) -> dict[str, object]:
    kw: dict[str, object] = {"interface": cfg.can.interface, "channel": cfg.can.channel, "bitrate": cfg.can.bitrate}
    if cfg.can.fd:
        kw["fd"] = True
        if cfg.can.data_bitrate:
            kw["data_bitrate"] = cfg.can.data_bitrate
    return kw


class XilRig:
    """dSPACE HIL through ASAM XIL API (MAPort). Needs `pip install pythonnet` and a dSPACE
    XIL API installation on this PC.

    The assembly names, vendor/product/version strings and MAPort calls below follow the
    ASAM XIL 2.x .NET API used by dSPACE's Python demos. Copy the exact `clr.AddReference`
    strings and product version from the XIL API demo of YOUR dSPACE release into
    [xil] in the rig config; they change between releases.
    """

    name = "xil"

    def __init__(self, cfg: RigConfig):
        self.cfg = cfg
        self.x = cfg.xil
        self._port = None

    def open(self) -> None:
        if not self.x.port_config or not self.x.product_version:
            raise RigError("[xil] needs port_config and product_version")
        try:
            import clr  # pythonnet
        except ImportError as exc:
            raise RigError("XIL backend needs pythonnet: pip install pythonnet") from exc
        for assembly in self.x.assemblies:
            clr.AddReference(assembly)
        from ASAM.XIL.Implementation.TestbenchFactory.Testbench import TestbenchFactory  # type: ignore

        testbench = TestbenchFactory().CreateVendorSpecificTestbench(
            self.x.vendor, self.x.product, self.x.product_version
        )
        port = testbench.MAPortFactory.CreateMAPort(self.x.port_name)
        port_config = port.LoadConfiguration(str(self.cfg.base_dir / self.x.port_config))
        # forceConfig=False: attach to the application already running on the platform.
        # Check on your release that this never re-downloads a different application.
        port.Configure(port_config, False)
        self._port = port

    def close(self) -> None:
        if self._port is not None:
            self._port.Dispose()
            self._port = None

    def read(self, path: str) -> object:
        if self._port is None:
            raise RigError("MAPort not open")
        value = self._port.Read(path)
        return getattr(value, "Value", value)

    def rig_info(self) -> dict[str, object]:
        if self._port is None:
            raise RigError("MAPort not open")
        info: dict[str, object] = {
            "simulation_state": str(self._port.State),  # e.g. eSIMULATION_RUNNING
            "port_config": self.x.port_config,
        }
        if self.x.simulation_state_var:
            info["model_state"] = self.read(self.x.simulation_state_var)
        if self.x.kl30_voltage_var:
            info["kl30_voltage_v"] = round(float(self.read(self.x.kl30_voltage_var)), 2)
        if self.x.kl15_var:
            info["kl15"] = "ON" if float(self.read(self.x.kl15_var)) else "OFF"
        for label, path in self.x.extra_vars.items():
            info[label] = self.read(path)
        return info

    def read_current(self, access: EcuAccess) -> float | None:
        if not access.current_var:
            return None
        return float(self.read(access.current_var))

    def can_bus_kwargs(self) -> dict[str, object]:
        return real_can_bus_kwargs(self.cfg)
