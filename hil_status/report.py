"""Render a RigStatus as a console table, JSON, or the HTML dashboard."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .campaign import campaign_console
from .model import EcuState, EcuStatus, RigStatus

TEMPLATE = Path(__file__).with_name("dashboard.html")


def _version_cell(e: EcuStatus, field: str) -> str:
    exp, act = e.expected.get(field, ""), e.actual.get(field)
    if act is None:
        return f"{exp} (file)" if exp else "-"
    if exp and any(m.startswith(field) for m in e.mismatches):
        return f"{exp} -> {act}"
    return act


def _power_cell(e: EcuStatus) -> str:
    if e.supply_current_a is None:
        return "?"
    return f"{'ON' if e.powered else 'OFF'} {e.supply_current_a:.2f}A"


def to_console(s: RigStatus) -> str:
    out = [
        f"{s.rig_name}: {s.overall.value}   (backend {s.backend}, {s.timestamp})",
        "Rig: " + ("  ".join(f"{k}={v}" for k, v in s.rig.items()) or "-"),
    ]
    if s.bus:
        out.append("CAN: " + "  ".join(f"{k}={v}" for k, v in s.bus.items()))
    header = ("ECU", "Type", "State", "Power", "Link ms", "SW version", "HW version", "Address")
    rows = [
        (
            e.name, e.ecu_type or "-", e.state.value, _power_cell(e),
            f"{e.response_ms:.1f}" if e.response_ms is not None else "-",
            _version_cell(e, "sw_version"), _version_cell(e, "hw_version"), e.address,
        )
        for e in s.ecus
    ]
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    out += ["", line(header), line(tuple("-" * w for w in widths)), *map(line, rows)]

    problems = [e for e in s.ecus if e.state != EcuState.OK or "read failed" in e.detail]
    if problems:
        out.append("")
        for e in problems:
            out.append(f"  {e.name}: {e.state.value} - {e.detail}")
            out += [f"      {m}" for m in e.mismatches]
    if s.notes:
        out += ["", *(f"  note: {n}" for n in s.notes)]
    connected = sum(e.connected for e in s.ecus)
    out += ["", f"{connected}/{len(s.ecus)} inventory ECUs connected."]
    if s.campaign:
        out += ["", *campaign_console(s.campaign)]
    return "\n".join(out)


def to_json(s: RigStatus) -> str:
    return json.dumps(s.to_dict(), indent=2, default=str)


def history_point(s: RigStatus) -> dict:
    return {"t": s.timestamp, "overall": s.overall.value, "ecus": {e.name: e.state.value for e in s.ecus}}


def to_html(s: RigStatus, history: list[dict] | None = None, live: bool = False, interval_s: int = 0) -> str:
    """The dashboard page. Static export embeds one snapshot; the live server passes
    live=True and the page then polls /api/status every interval_s seconds."""
    payload = {
        "status": s.to_dict(),
        "history": history if history is not None else [history_point(s)],
        "live": live,
        "interval_s": interval_s,
    }
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", escape(s.rig_name)).replace("__DATA__", data)
