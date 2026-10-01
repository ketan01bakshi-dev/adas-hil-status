"""Parse the bench's ECU inventory text file.

Bench sheets come in many shapes, so four layouts are auto-detected:

1. INI sections         [ADAS_ECU] / sw_version = ...
2. Delimited table      header row + rows split by | , ; or TAB (Markdown tables too)
3. Aligned table        header row + columns separated by 2+ spaces
4. key: value blocks    one ECU per block, blocks separated by blank lines

Column/key names are matched through aliases ("SW Version", "software", "FW" ...).
Lines starting with # or // are comments. Unknown columns are kept in `extra`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import InventoryEntry

ALIASES: dict[str, set[str]] = {
    "name": {"name", "ecu", "ecu name", "node", "node name", "device", "component", "ecu id"},
    "ecu_type": {"type", "ecu type", "category", "sensor", "sensor type", "role", "description"},
    "sw_version": {
        "sw", "sw version", "sw ver", "swversion", "software", "software version",
        "fw", "fw version", "firmware", "firmware version", "application version",
    },
    "hw_version": {
        "hw", "hw version", "hw ver", "hwversion", "hardware", "hardware version",
        "hw rev", "hw revision", "hardware revision", "hw part",
    },
}

_SECTION = re.compile(r"^\[(?P<name>[^\]]+)\]$")
_KV = re.compile(r"^(?P<key>[^=:]+?)\s*[=:]\s*(?P<value>.*)$")
_MD_RULE = re.compile(r"^[\s|:+-]+$")


class InventoryError(ValueError):
    pass


def _norm_key(key: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_.]", " ", key.strip().lower()))


def canonical_key(key: str) -> str:
    k = _norm_key(key)
    for canon, names in ALIASES.items():
        if k in names:
            return canon
    return k.replace(" ", "_")


def _content_lines(text: str) -> list[tuple[int, str]]:
    out = []
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("#") or line.startswith("//"):
            line = ""
        out.append((no, line))
    return out


def _entry(fields: dict[str, str], line: int) -> InventoryEntry:
    name = fields.pop("name", "").strip()
    if not name:
        raise InventoryError(f"line {line}: ECU without a name")
    return InventoryEntry(
        name=name,
        ecu_type=fields.pop("ecu_type", "").strip(),
        sw_version=fields.pop("sw_version", "").strip(),
        hw_version=fields.pop("hw_version", "").strip(),
        extra={k: v.strip() for k, v in fields.items() if v.strip()},
        line=line,
    )


def _parse_sections(lines: list[tuple[int, str]]) -> list[InventoryEntry]:
    entries, current, start = [], None, 0
    for no, line in lines:
        if not line:
            continue
        m = _SECTION.match(line)
        if m:
            if current is not None:
                entries.append(_entry(current, start))
            current, start = {"name": m["name"].strip()}, no
            continue
        kv = _KV.match(line)
        if current is None or not kv:
            raise InventoryError(f"line {no}: expected [SECTION] or key = value, got {line!r}")
        key = canonical_key(kv["key"])
        if key == "name":  # a section header already names the ECU
            key = "alias"
        current[key] = kv["value"]
    if current is not None:
        entries.append(_entry(current, start))
    return entries


def _parse_blocks(lines: list[tuple[int, str]]) -> list[InventoryEntry]:
    entries, current, start = [], {}, 0
    for no, line in lines + [(0, "")]:
        if not line:
            if current:
                entries.append(_entry(current, start))
            current = {}
            continue
        kv = _KV.match(line)
        if not kv:
            raise InventoryError(f"line {no}: expected 'key: value', got {line!r}")
        if not current:
            start = no
        current[canonical_key(kv["key"])] = kv["value"]
    return entries


def _split_row(line: str, delim: str | None) -> list[str]:
    if delim is None:
        return [c.strip() for c in re.split(r"\s{2,}", line)]
    if delim == "|":
        line = line.strip("|")
    return [c.strip() for c in line.split(delim)]


def _detect_table(header: str) -> str | None | bool:
    """Return the delimiter of a table header, None for space-aligned, False if not a table."""
    for delim in ("|", "\t", ";", ","):
        cols = _split_row(header, delim)
        if len(cols) >= 2 and {"name"} <= {canonical_key(c) for c in cols}:
            return delim
    cols = _split_row(header, None)
    if len(cols) >= 2 and "name" in {canonical_key(c) for c in cols}:
        return None
    return False


def _parse_table(lines: list[tuple[int, str]], delim: str | None) -> list[InventoryEntry]:
    rows = [(no, l) for no, l in lines if l and not _MD_RULE.match(l)]
    header = [canonical_key(c) for c in _split_row(rows[0][1], delim)]
    entries = []
    for no, line in rows[1:]:
        cells = _split_row(line, delim)
        if len(cells) > len(header):
            raise InventoryError(f"line {no}: {len(cells)} columns, header has {len(header)}")
        cells += [""] * (len(header) - len(cells))
        entries.append(_entry(dict(zip(header, cells)), no))
    return entries


def parse_inventory(text: str) -> list[InventoryEntry]:
    lines = _content_lines(text)
    filled = [(no, l) for no, l in lines if l]
    if not filled:
        raise InventoryError("inventory is empty")

    if any(_SECTION.match(l) for _, l in filled):
        entries = _parse_sections(lines)
    else:
        kind = _detect_table(filled[0][1])
        entries = _parse_blocks(lines) if kind is False else _parse_table(lines, kind)

    seen: dict[str, int] = {}
    for e in entries:
        key = e.name.casefold()
        if key in seen:
            raise InventoryError(f"line {e.line}: ECU {e.name!r} already listed on line {seen[key]}")
        seen[key] = e.line
    return entries


def load_inventory(path: str | Path) -> list[InventoryEntry]:
    return parse_inventory(Path(path).read_text(encoding="utf-8-sig"))
