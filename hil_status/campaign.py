"""Campaign view: test-run status, requirement coverage and defect clustering.

Inputs are two CSV files, the shape most test tools can export (ECU-TEST, PROVEtec,
IBM ETM, a Jenkins job):

  requirements.csv  id, title, feature, asil
  results.csv       test_id, title, requirements (";"-separated), feature, ecu, status,
                    defect_id, failure_signature, duration_s, executed_at

status is PASS / FAIL / BLOCKED / NOT_RUN. This is visibility for the test manager:
nothing here signs a release off automatically.

Defect clustering groups FAIL and BLOCKED results by defect ID, or by failure signature
with numbers masked ("dropout beyond 140 m" ~ "beyond 150 m") when no ticket exists yet.
A cluster whose ECUs are all unhealthy on the bench right now is flagged "suspect bench":
check the environment before raising a product defect.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .model import EcuState, RigStatus

STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT_RUN")
EXECUTED = {"PASS", "FAIL"}
BENCH_BAD = {EcuState.VERSION_MISMATCH, EcuState.NOT_POWERED, EcuState.NO_RESPONSE, EcuState.ERROR}


class CampaignError(ValueError):
    pass


@dataclass
class Requirement:
    id: str
    title: str = ""
    feature: str = ""
    asil: str = ""


@dataclass
class TestResult:
    test_id: str
    title: str = ""
    requirements: list[str] = field(default_factory=list)
    feature: str = ""
    ecu: str = ""
    status: str = "NOT_RUN"
    defect_id: str = ""
    failure_signature: str = ""
    duration_s: float = 0.0
    executed_at: str = ""


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise CampaignError(f"{path.name}: empty file")
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


def load_requirements(path: Path) -> list[Requirement]:
    reqs = []
    for n, r in enumerate(_rows(path), start=2):
        if not r.get("id"):
            raise CampaignError(f"{path.name} line {n}: requirement without id")
        reqs.append(Requirement(r["id"], r.get("title", ""), r.get("feature", ""), r.get("asil", "").upper()))
    return reqs


def load_results(path: Path) -> list[TestResult]:
    out = []
    for n, r in enumerate(_rows(path), start=2):
        status = r.get("status", "").upper().replace(" ", "_") or "NOT_RUN"
        if status not in STATUSES:
            raise CampaignError(f"{path.name} line {n}: status {status!r} not in {STATUSES}")
        if not r.get("test_id"):
            raise CampaignError(f"{path.name} line {n}: result without test_id")
        out.append(TestResult(
            test_id=r["test_id"], title=r.get("title", ""),
            requirements=[x.strip() for x in r.get("requirements", "").split(";") if x.strip()],
            feature=r.get("feature", ""), ecu=r.get("ecu", ""), status=status,
            defect_id=r.get("defect_id", ""), failure_signature=r.get("failure_signature", ""),
            duration_s=float(r.get("duration_s") or 0), executed_at=r.get("executed_at", ""),
        ))
    return out


def signature_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+(\.\d+)?", "#", text.lower())).strip()


def _counts(results: list[TestResult]) -> dict[str, int]:
    c = Counter(r.status for r in results)
    return {s: c.get(s, 0) for s in STATUSES}


def summarize(meta: dict, reqs: list[Requirement], results: list[TestResult], bench: RigStatus | None) -> dict:
    notes: list[str] = []

    # --- campaign status -------------------------------------------------------
    counts = _counts(results)
    executed = counts["PASS"] + counts["FAIL"]
    by_feature = []
    for feat in sorted({r.feature for r in results}):
        rs = [r for r in results if r.feature == feat]
        by_feature.append({"feature": feat, "total": len(rs), **_counts(rs)})
    times = sorted(r.executed_at for r in results if r.executed_at)

    # --- requirement coverage --------------------------------------------------
    tests_by_req: dict[str, list[TestResult]] = defaultdict(list)
    for r in results:
        for rid in r.requirements:
            tests_by_req[rid].append(r)
    known = {q.id for q in reqs}
    unknown = sorted(set(tests_by_req) - known)
    if unknown:
        notes.append(f"results reference requirements missing from the requirements file: {', '.join(unknown)}")

    def req_state(q: Requirement) -> str:
        ts = tests_by_req.get(q.id, [])
        if not ts:
            return "NO_TEST"
        if any(t.status == "FAIL" for t in ts):
            return "FAILED"
        if all(t.status == "PASS" for t in ts):
            return "VERIFIED"
        if any(t.status in EXECUTED for t in ts):
            return "PARTIAL"
        return "NOT_EXECUTED"

    req_rows = []
    for q in reqs:
        ts = tests_by_req.get(q.id, [])
        req_rows.append({
            "id": q.id, "title": q.title, "feature": q.feature, "asil": q.asil, "state": req_state(q),
            "tests": [t.test_id for t in ts],
        })

    def cov(rows: list[dict]) -> dict:
        n = len(rows)
        planned = sum(r["state"] != "NO_TEST" for r in rows)
        executed_ = sum(r["state"] in ("VERIFIED", "FAILED", "PARTIAL") for r in rows)
        verified = sum(r["state"] == "VERIFIED" for r in rows)
        return {"total": n, "planned": planned, "executed": executed_, "verified": verified,
                "failed": sum(r["state"] == "FAILED" for r in rows)}

    group = lambda key: [{"name": k or "-", **cov([r for r in req_rows if r[key] == k])}
                         for k in sorted({r[key] for r in req_rows})]
    coverage = {
        "overall": cov(req_rows), "by_feature": group("feature"), "by_asil": group("asil"),
        "requirements": req_rows,
    }

    # --- defect clustering -----------------------------------------------------
    bench_state = {e.name.casefold(): e for e in (bench.ecus if bench else [])}
    buckets: dict[str, list[TestResult]] = defaultdict(list)
    for r in results:
        if r.status in ("FAIL", "BLOCKED"):
            key = f"defect:{r.defect_id}" if r.defect_id else f"sig:{signature_key(r.failure_signature) or 'no signature'}"
            buckets[key].append(r)

    clusters = []
    for key, rs in buckets.items():
        ecus = sorted({r.ecu for r in rs if r.ecu})
        sick = [bench_state[e.casefold()] for e in ecus if e.casefold() in bench_state
                and bench_state[e.casefold()].state in BENCH_BAD]
        suspect = bool(ecus) and len(sick) == len(ecus)
        reasons = []
        for s in sick:
            if s.mismatches:
                reasons += [f"{s.name}: {m}" for m in s.mismatches]
            else:
                reasons.append(f"{s.name}: {s.state.value.replace('_', ' ').lower()}")
        clusters.append({
            "key": key,
            "defect_id": rs[0].defect_id,
            "signature": rs[0].failure_signature or "(no signature)",
            "size": len(rs),
            "statuses": dict(Counter(r.status for r in rs)),
            "ecus": ecus,
            "features": sorted({r.feature for r in rs if r.feature}),
            "requirements": sorted({q for r in rs for q in r.requirements}),
            "tests": [{"id": r.test_id, "title": r.title, "status": r.status} for r in rs],
            "suspect_bench": suspect,
            "bench_reasons": reasons if suspect else [],
            "needs_ticket": not rs[0].defect_id and not suspect,
        })
    clusters.sort(key=lambda c: (-c["size"], c["key"]))

    features = sorted({r.feature for r in results if r.feature})
    ecus_all = sorted({r.ecu for r in results if r.ecu})
    matrix = {f: {e: sum(1 for r in results if r.feature == f and r.ecu == e and r.status in ("FAIL", "BLOCKED"))
                  for e in ecus_all} for f in features}

    return {
        "meta": {**meta, "first_execution": times[0] if times else "", "last_execution": times[-1] if times else ""},
        "status": {"total": len(results), "executed": executed, **counts,
                   "pass_rate": round(counts["PASS"] / executed * 100, 1) if executed else None,
                   "by_feature": by_feature},
        "coverage": coverage,
        "defects": {"clusters": clusters, "matrix": {"features": features, "ecus": ecus_all, "fails": matrix}},
        "notes": notes,
    }


def attach_campaign(cfg, status: RigStatus) -> None:
    """Load [campaign] files (if configured) and put the summary on the status. Never raises."""
    c = cfg.campaign
    if not c:
        return
    try:
        reqs = load_requirements(cfg.base_dir / c["requirements"])
        results = load_results(cfg.base_dir / c["results"])
        meta = {k: v for k, v in c.items() if k not in ("requirements", "results")}
        status.campaign = summarize(meta, reqs, results, status)
    except (OSError, KeyError, CampaignError, ValueError) as exc:
        status.campaign = {"error": str(exc)}
        status.notes.append(f"campaign data unreadable: {exc}")


def campaign_console(camp: dict) -> list[str]:
    if not camp:
        return []
    if "error" in camp:
        return [f"Campaign: {camp['error']}"]
    s, cov = camp["status"], camp["coverage"]["overall"]
    out = [
        f"Campaign {camp['meta'].get('name', '')}: {s['executed']}/{s['total']} executed, "
        f"{s['PASS']} pass, {s['FAIL']} fail, {s['BLOCKED']} blocked, {s['NOT_RUN']} not run"
        + (f", pass rate {s['pass_rate']}%" if s["pass_rate"] is not None else ""),
        f"Coverage: {cov['executed']}/{cov['total']} requirements executed, {cov['verified']} verified, "
        f"{cov['total'] - cov['planned']} without a test",
    ]
    for c in camp["defects"]["clusters"][:5]:
        tag = " [suspect bench]" if c["suspect_bench"] else (" [needs ticket]" if c["needs_ticket"] else "")
        out.append(f"  cluster x{c['size']}: {c['defect_id'] or c['signature']} ({', '.join(c['ecus'])}){tag}")
    return out
