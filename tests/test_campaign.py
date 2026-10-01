from pathlib import Path

import pytest

from hil_status.campaign import (
    CampaignError, Requirement, load_requirements, load_results, signature_key, summarize,
)
from hil_status.campaign import TestResult as Result
from hil_status.model import EcuState, EcuStatus, Overall, RigStatus

DATA = Path(__file__).resolve().parents[1] / "data" / "campaign"


def _bench(**states):
    ecus = [EcuStatus(name=n, ecu_type="", transport="can", address="", state=s,
                      mismatches=["sw_version: inventory 'A', ECU reports 'B'"] if s is EcuState.VERSION_MISMATCH else [])
            for n, s in states.items()]
    return RigStatus("rig", "sim", "t", "", Overall.PASS, ecus=ecus)


def test_signature_masks_numbers():
    assert signature_key("Radar target dropout beyond 140 m") == signature_key("radar target dropout beyond 150 m")
    assert signature_key("Lane confidence below 0.4") == signature_key("Lane confidence below 0.35")


def test_status_coverage_and_clusters_on_sample_data():
    reqs, results = load_requirements(DATA / "requirements.csv"), load_results(DATA / "results.csv")
    bench = _bench(ADAS_ECU=EcuState.OK, FRONT_CAMERA=EcuState.VERSION_MISMATCH,
                   FRONT_RADAR=EcuState.OK, LIDAR=EcuState.NOT_POWERED)
    c = summarize({"name": "x"}, reqs, results, bench)

    s = c["status"]
    assert (s["total"], s["PASS"], s["FAIL"], s["BLOCKED"], s["NOT_RUN"]) == (34, 18, 11, 3, 2)
    assert s["pass_rate"] == round(18 / 29 * 100, 1)

    o = c["coverage"]["overall"]
    assert (o["total"], o["planned"], o["verified"], o["failed"]) == (24, 21, 10, 6)
    states = {r["id"]: r["state"] for r in c["coverage"]["requirements"]}
    assert states["REQ-ACC-006"] == "NO_TEST"
    assert states["REQ-AEB-005"] == "NOT_EXECUTED"  # only blocked tests
    assert states["REQ-ACC-001"] == "PARTIAL"  # two passes + one not run

    clusters = {c_["defect_id"] or c_["signature"]: c_ for c_ in c["defects"]["clusters"]}
    assert clusters["ADAS-1051"]["size"] == 2  # same ticket, different numbers in the signature
    assert not clusters["ADAS-1042"]["suspect_bench"]
    assert clusters["LIDAR not reachable"]["suspect_bench"] and clusters["LIDAR not reachable"]["size"] == 3
    cam = clusters["Lane marking confidence below 0.4"]
    assert cam["size"] == 2 and cam["suspect_bench"] and "inventory 'A'" in cam["bench_reasons"][0]
    assert c["defects"]["matrix"]["fails"]["AEB"]["LIDAR"] == 2


def test_healthy_bench_turns_untracked_failures_into_needs_ticket():
    reqs = [Requirement("R1", feature="F")]
    results = [Result("T1", requirements=["R1"], feature="F", ecu="CAM", status="FAIL", failure_signature="x 1"),
               Result("T2", requirements=["R9"], feature="F", ecu="CAM", status="FAIL", failure_signature="x 2")]
    c = summarize({}, reqs, results, _bench(CAM=EcuState.OK))
    (cl,) = c["defects"]["clusters"]
    assert cl["size"] == 2 and cl["needs_ticket"] and not cl["suspect_bench"]
    assert "R9" in c["notes"][0]  # traceability gap reported


def test_bad_status_is_rejected(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("test_id,status\nT1,DONE\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="line 2"):
        load_results(p)
