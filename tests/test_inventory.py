import pytest

from hil_status.inventory import InventoryError, parse_inventory


def _names(entries):
    return [(e.name, e.ecu_type, e.sw_version, e.hw_version) for e in entries]


EXPECTED = [("ADAS_ECU", "ADAS ECU", "ADAS_SW_1.0", "HW_C2"), ("FRONT_RADAR", "Radar", "RAD_SW_3.8", "HW_A3")]


def test_pipe_table_with_markdown_rule_and_comments():
    text = """# bench sheet
| ECU Name | Type | SW Version | HW Version | Bus |
|---|---|---|---|---|
| ADAS_ECU | ADAS ECU | ADAS_SW_1.0 | HW_C2 | Ethernet |
| FRONT_RADAR | Radar | RAD_SW_3.8 | HW_A3 | CAN |
"""
    entries = parse_inventory(text)
    assert _names(entries) == EXPECTED
    assert entries[0].extra == {"bus": "Ethernet"}


def test_csv_and_aliases():
    text = "Node,Category,Software,Hardware\nADAS_ECU,ADAS ECU,ADAS_SW_1.0,HW_C2\nFRONT_RADAR,Radar,RAD_SW_3.8,HW_A3\n"
    assert _names(parse_inventory(text)) == EXPECTED


def test_space_aligned_table():
    text = (
        "ECU           Type       SW Version     HW Version\n"
        "ADAS_ECU      ADAS ECU   ADAS_SW_1.0    HW_C2\n"
        "FRONT_RADAR   Radar      RAD_SW_3.8     HW_A3\n"
    )
    assert _names(parse_inventory(text)) == EXPECTED


def test_ini_sections():
    text = """
[ADAS_ECU]
type = ADAS ECU
sw_version = ADAS_SW_1.0
hw_version = HW_C2

[FRONT_RADAR]
Type: Radar
Software Version: RAD_SW_3.8
Hardware Version: HW_A3
"""
    assert _names(parse_inventory(text)) == EXPECTED


def test_key_value_blocks():
    text = """
ECU: ADAS_ECU
Type: ADAS ECU
SW: ADAS_SW_1.0
HW: HW_C2

ECU: FRONT_RADAR
Type: Radar
SW: RAD_SW_3.8
HW: HW_A3
"""
    assert _names(parse_inventory(text)) == EXPECTED


def test_duplicate_ecu_is_rejected_with_lines():
    text = "ECU|SW\nRADAR|1\nradar|2\n"
    with pytest.raises(InventoryError, match="line 3.*already listed on line 2"):
        parse_inventory(text)


def test_too_many_columns_and_empty_file():
    with pytest.raises(InventoryError, match="line 2"):
        parse_inventory("ECU|SW\nRADAR|1|extra\n")
    with pytest.raises(InventoryError, match="empty"):
        parse_inventory("# only a comment\n")
