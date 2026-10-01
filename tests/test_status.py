import json
import tomllib
from pathlib import Path

from hil_status.cli import main
from hil_status.config import parse_config
from hil_status.inventory import load_inventory
from hil_status.model import EcuState, Overall
from hil_status.report import to_console, to_html
from hil_status.sim import SimRig
from hil_status.status import collect

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "rig.toml"
INVENTORY = ROOT / "data" / "ecu_inventory.txt"


def _run(sim: dict, inventory=None):
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    data["sim"] = sim
    cfg = parse_config(data, CONFIG.parent)
    inv = inventory or load_inventory(INVENTORY)
    return collect(cfg, inv, SimRig(cfg, inv), str(INVENTORY))


def test_healthy_bench_passes():
    s = _run({})
    assert s.overall is Overall.PASS
    states = {e.name: e.state for e in s.ecus}
    assert set(states.values()) == {EcuState.OK}
    radar = next(e for e in s.ecus if e.name == "FRONT_RADAR")
    assert radar.actual["sw_version"] == "RAD_SW_03.08.02"
    assert radar.actual["serial_number"].startswith("SN")
    assert s.bus["responders"] == ["0x7EA", "0x7EB"]


def test_faults_are_classified():
    s = _run({
        "unknown_responders": [0x7EE],
        "ecus": {
            "FRONT_CAMERA": {"sw_version": "CAM_SW_04.11.00"},
            "FRONT_RADAR": {"responsive": False},
            "ADAS_ECU": {"responsive": False},
            "LIDAR": {"powered": False},
        },
    })
    states = {e.name: e.state for e in s.ecus}
    assert states == {
        "ADAS_ECU": EcuState.NO_RESPONSE,
        "FRONT_CAMERA": EcuState.VERSION_MISMATCH,
        "FRONT_RADAR": EcuState.NO_RESPONSE,
        "LIDAR": EcuState.NOT_POWERED,
    }
    assert s.overall is Overall.FAIL
    assert s.unknown_responders == ["0x7EE"]
    cam = next(e for e in s.ecus if e.name == "FRONT_CAMERA")
    assert cam.mismatches == ["sw_version: inventory 'CAM_SW_04.12.00', ECU reports 'CAM_SW_04.11.00'"]


def test_version_mismatch_only_is_warn():
    s = _run({"ecus": {"FRONT_RADAR": {"hw_version": "HW_A4"}}})
    assert s.overall is Overall.WARN


def test_inventory_ecu_without_config_is_not_probed():
    inv = load_inventory(INVENTORY)
    from hil_status.model import InventoryEntry

    inv.append(InventoryEntry(name="REAR_RADAR", ecu_type="Corner radar", sw_version="X"))
    s = _run({}, inv)
    rear = next(e for e in s.ecus if e.name == "REAR_RADAR")
    assert rear.state is EcuState.NOT_PROBED and s.overall is Overall.WARN


def test_reports_escape_and_render():
    s = _run({"ecus": {"FRONT_CAMERA": {"sw_version": "</script><b>x"}}})
    html = to_html(s)
    assert "</script><b>x" not in html  # cannot break out of the embedded JSON block
    assert html.count("</script>") == 2 and "__DATA__" not in html
    assert "VERSION_MISMATCH" in to_console(s)


def test_live_server_endpoints():
    import urllib.request

    from hil_status.server import Monitor, make_handler
    from http.server import ThreadingHTTPServer
    import threading

    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    cfg = parse_config(data, CONFIG.parent)
    inv = load_inventory(INVENTORY)
    mon = Monitor(cfg, INVENTORY, SimRig(cfg, inv), interval_s=60)
    mon.check()
    mon.check()  # backend stays open between checks
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(mon))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert '"live": true' in page
        req = urllib.request.Request(base + "/api/refresh", method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        payload = json.loads(urllib.request.urlopen(base + "/api/status").read())
        assert len(payload["history"]) == 3 and payload["status"]["overall"] == "FAIL"
    finally:
        httpd.shutdown()
        mon.stop()


def test_cli_writes_outputs_and_exit_code(tmp_path, capsys):
    code = main(["--config", str(CONFIG), "--json", str(tmp_path / "s.json"), "--html", str(tmp_path / "s.html")])
    assert code == 2  # demo config: LIDAR switched off -> FAIL
    data = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert {e["name"]: e["connected"] for e in data["ecus"]}["LIDAR"] is False
    assert (tmp_path / "s.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "3/4 inventory ECUs connected" in capsys.readouterr().out


def test_cli_bad_inventory_is_exit_3(tmp_path):
    bad = tmp_path / "inv.txt"
    bad.write_text("ECU|SW\nA|1\na|2\n", encoding="utf-8")
    assert main(["--config", str(CONFIG), "--inventory", str(bad), "--quiet"]) == 3
