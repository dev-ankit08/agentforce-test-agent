"""
report.py -- reads every results/*_<run_id>.csv file for a run, applies the
gates, prints a terminal summary, and writes report_<run_id>.md (human) +
report_<run_id>.csv (machine) + hallucination_worksheet_<run_id>.csv (a
sample for a human to spot-check by hand).

Every test case can run more than once per bulk run (see executor.py's
`repetitions`), so most gates operate at the (test_id, repetition)
granularity -- "how many executions passed/failed", not just "how many
test cases". `gate_consistency` collapses repetitions back down per test_id
to answer a different question: does this test case pass *every* time, or
is the agent flaky on it?
"""
from __future__ import annotations

import csv
import os

import config
from schemas import BULK_CATEGORIES

SEVERITY_ORDER = {"critical": 0, "medium": 1, "low": 2, "none": 3}


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_run(run_id: str, cases_file: str = "test_cases.csv") -> dict:
    cases = {c["test_id"]: c for c in _read_csv(cases_file)}
    execution = _read_csv(os.path.join(config.RESULTS_DIR, f"run_{run_id}.csv"))
    judged = _read_csv(os.path.join(config.RESULTS_DIR, f"judged_{run_id}.csv"))
    hallucinations = _read_csv(os.path.join(config.RESULTS_DIR, f"hallucinations_{run_id}.csv"))
    hallucination_summary = _read_csv(os.path.join(config.RESULTS_DIR, f"hallucination_summary_{run_id}.csv"))
    regression = _read_csv(os.path.join(config.RESULTS_DIR, f"regression_{run_id}.csv"))
    return {
        "cases": cases,
        "execution": execution,
        "judged": judged,
        "hallucinations": hallucinations,
        "hallucination_summary": hallucination_summary,
        "regression": regression,
    }


def gate0_execution_integrity(data: dict) -> dict:
    errors = [r for r in data["execution"] if r["execution_status"] != "OK"]
    return {"total": len(data["execution"]), "infra_failures": errors}


def gate1_pass_rate(data: dict) -> dict:
    """Pass/fail % across every execution (test case x repetition) -- this
    is "how much % is failing" across the whole bulk run, repeats included.

    `by_ac` is restricted to happy_path/negative rows only (schemas.
    BULK_CATEGORIES) -- the per-acceptance-criterion "bulk pair" -- since
    edge_case/adversarial cases run once and aren't part of the bulk
    consistency measurement for that AC."""
    valid = [v for v in data["judged"] if v["verdict"] != "EXCLUDED"]
    passed = [v for v in valid if v["verdict"] == "PASS"]
    failed = [v for v in valid if v["verdict"] == "FAIL"]
    pass_pct = (len(passed) / len(valid) * 100) if valid else 0.0
    fail_pct = (len(failed) / len(valid) * 100) if valid else 0.0

    by_category: dict[str, dict] = {}
    by_topic: dict[str, dict] = {}
    by_ac: dict[str, dict] = {}
    for v in valid:
        case = data["cases"].get(v["test_id"], {})
        buckets = [(case.get("category", "unknown"), by_category), (case.get("expected_topic", "unknown"), by_topic)]
        if case.get("category") in BULK_CATEGORIES:
            buckets.append((case.get("ac_id", "unassigned"), by_ac))
        for key, bucket in buckets:
            b = bucket.setdefault(key, {"pass": 0, "fail": 0, "total": 0})
            b["total"] += 1
            if v["verdict"] == "PASS":
                b["pass"] += 1
            else:
                b["fail"] += 1

    return {
        "valid_count": len(valid),
        "pass_count": len(passed),
        "fail_count": len(failed),
        "pass_pct": pass_pct,
        "fail_pct": fail_pct,
        "failed_tests": failed,
        "by_category": by_category,
        "by_topic": by_topic,
        "by_ac": by_ac,
    }


def aggregate_verdicts_by_test(verdicts: list[dict]) -> list[dict]:
    """Collapses per-repetition verdicts into one row per test_id: PASS only
    if every repetition of that test passed, otherwise FAIL (a test that's
    flaky -- right 4 times out of 5 -- is not a reliably-passing test). This
    is what regression comparisons and the consistency gate are built on;
    a test whose consistency drops from 100% to partial between two runs
    surfaces as a regression (PASS -> FAIL) automatically, with no separate
    mechanism needed."""
    groups: dict[str, list[dict]] = {}
    for v in verdicts:
        groups.setdefault(v["test_id"], []).append(v)

    aggregated = []
    for test_id, vs in groups.items():
        valid = [v for v in vs if v["verdict"] != "EXCLUDED"]
        graded_count = len(valid)
        pass_count = sum(1 for v in valid if v["verdict"] == "PASS")
        if graded_count == 0:
            agg_verdict = "EXCLUDED"
        elif pass_count == graded_count:
            agg_verdict = "PASS"
        else:
            agg_verdict = "FAIL"
        aggregated.append({
            "test_id": test_id,
            "run_id": vs[0]["run_id"],
            "verdict": agg_verdict,
            "repetitions": len(vs),
            "pass_count": pass_count,
            "graded_count": graded_count,
            "consistency_pct": (pass_count / graded_count * 100) if graded_count else 0.0,
        })
    return aggregated


def gate_consistency(data: dict) -> dict:
    aggregated = aggregate_verdicts_by_test(data["judged"])
    flaky = [a for a in aggregated if a["graded_count"] > 0 and 0 < a["consistency_pct"] < 100]
    return {"aggregated": sorted(aggregated, key=lambda a: a["consistency_pct"]), "flaky": flaky}


def gate2_hallucination_scan(data: dict) -> dict:
    flagged = sorted(data["hallucinations"], key=lambda c: SEVERITY_ORDER.get(c["severity"], 9))
    severity_counts: dict[str, int] = {}
    for c in flagged:
        severity_counts[c["severity"]] = severity_counts.get(c["severity"], 0) + 1

    adversarial_ids = {tid for tid, case in data["cases"].items() if case.get("category") == "adversarial"}
    adversarial_valid = {
        (v["test_id"], v.get("repetition")) for v in data["judged"]
        if v["test_id"] in adversarial_ids and v["verdict"] != "EXCLUDED"
    }
    adversarial_flagged = {(c["test_id"], c.get("repetition")) for c in flagged if c["test_id"] in adversarial_ids}
    hallucination_induced_rate = (
        len(adversarial_flagged) / len(adversarial_valid) * 100 if adversarial_valid else 0.0
    )

    by_ac: dict[str, int] = {}
    for c in flagged:
        ac_id = data["cases"].get(c["test_id"], {}).get("ac_id", "unassigned")
        by_ac[ac_id] = by_ac.get(ac_id, 0) + 1

    return {
        "flagged": flagged,
        "severity_counts": severity_counts,
        "adversarial_total": len(adversarial_valid),
        "adversarial_flagged": len(adversarial_flagged),
        "hallucination_induced_rate": hallucination_induced_rate,
        "by_ac": by_ac,
    }


def gate_regression(data: dict) -> dict:
    newly_failing = [r for r in data["regression"] if r["delta_type"] == "newly_failing"]
    newly_passing = [r for r in data["regression"] if r["delta_type"] == "newly_passing"]
    return {"newly_failing": newly_failing, "newly_passing": newly_passing, "all": data["regression"]}


def render_terminal(run_id: str, g0: dict, g1: dict, gc: dict, g2: dict, greg: dict) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append(f" AGENTFORCE TEST REPORT — run {run_id}")
    lines.append("=" * 72)

    lines.append("")
    lines.append(f"Gate 0 — Execution integrity: {len(g0['infra_failures'])} infra failure(s) / {g0['total']} executions")
    for r in g0["infra_failures"]:
        lines.append(f"    ! {r['test_id']} (rep {r.get('repetition', '?')}): execution_status=ERROR")

    lines.append("")
    lines.append(
        f"Gate 1 — Pass rate across all executions: {g1['pass_pct']:.1f}% PASS / {g1['fail_pct']:.1f}% FAIL  "
        f"({g1['pass_count']} pass / {g1['fail_count']} fail / {g1['valid_count']} graded executions)"
    )
    lines.append("  By category (pass% / fail%):")
    for cat, b in sorted(g1["by_category"].items()):
        pass_pct = (b["pass"] / b["total"] * 100) if b["total"] else 0.0
        fail_pct = (b["fail"] / b["total"] * 100) if b["total"] else 0.0
        lines.append(f"    {cat:<14} pass {pass_pct:5.1f}%  fail {fail_pct:5.1f}%  ({b['pass']}/{b['total']})")
    lines.append("  By expected topic (pass% / fail%):")
    for topic, b in sorted(g1["by_topic"].items()):
        pass_pct = (b["pass"] / b["total"] * 100) if b["total"] else 0.0
        fail_pct = (b["fail"] / b["total"] * 100) if b["total"] else 0.0
        lines.append(f"    {topic:<24} pass {pass_pct:5.1f}%  fail {fail_pct:5.1f}%  ({b['pass']}/{b['total']})")
    lines.append("  By acceptance criteria (bulk: happy_path + negative only):")
    for ac_id, b in sorted(g1["by_ac"].items()):
        pass_pct = (b["pass"] / b["total"] * 100) if b["total"] else 0.0
        fail_pct = (b["fail"] / b["total"] * 100) if b["total"] else 0.0
        halluc = g2["by_ac"].get(ac_id, 0)
        lines.append(
            f"    {ac_id:<10} pass {pass_pct:5.1f}%  fail {fail_pct:5.1f}%  ({b['pass']}/{b['total']})"
            f"  | {halluc} hallucination(s) flagged"
        )
    if g1["failed_tests"]:
        lines.append("  Failed executions:")
        for v in g1["failed_tests"]:
            lines.append(f"    x {v['test_id']} (rep {v.get('repetition', '?')}): {v['rationale']}")

    lines.append("")
    lines.append(f"Consistency — {len(gc['flaky'])} flaky test case(s) out of {len(gc['aggregated'])} (same test, different verdict across repetitions)")
    for a in gc["aggregated"]:
        if a["verdict"] == "EXCLUDED":
            marker = "EXCL  "
        elif a in gc["flaky"]:
            marker = "FLAKY "
        elif a["verdict"] == "PASS":
            marker = "PASS  "
        else:
            marker = "FAIL  "
        if a["graded_count"] == 0:
            lines.append(f"    {marker}{a['test_id']:<10} no graded executions ({a['repetitions']} repetition(s), all excluded by Gate 0)")
        else:
            lines.append(f"    {marker}{a['test_id']:<10} {a['pass_count']}/{a['graded_count']} passed ({a['consistency_pct']:.0f}% consistent, {a['repetitions']} repetition(s))")

    lines.append("")
    lines.append(
        f"Gate 2 — Hallucination scan: {len(g2['flagged'])} unsupported claim(s) flagged across all executions "
        f"| adversarial hallucination-induced rate: {g2['hallucination_induced_rate']:.1f}% "
        f"({g2['adversarial_flagged']}/{g2['adversarial_total']} executions)"
    )
    for c in g2["flagged"]:
        lines.append(f"    [{c['severity'].upper():<8}] {c['test_id']} (rep {c.get('repetition', '?')}): {c['claim']}")
        lines.append(f"               -> {c['detail']}")

    lines.append("")
    lines.append(f"Regression — {len(greg['newly_failing'])} newly failing, {len(greg['newly_passing'])} newly passing (vs previous run, aggregated per test case)")
    for r in greg["newly_failing"]:
        lines.append(f"    REGRESSION  {r['test_id']}: PASS -> FAIL (was {r['previous_run_id']})")
    for r in greg["newly_passing"]:
        lines.append(f"    FIXED       {r['test_id']}: FAIL -> PASS (was {r['previous_run_id']})")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def write_report_csv(run_id: str, g0: dict, g1: dict, gc: dict, g2: dict, greg: dict) -> str:
    path = os.path.join(config.RESULTS_DIR, f"report_{run_id}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        writer.writerow(["run_id", run_id])
        writer.writerow(["total_executions", g0["total"]])
        writer.writerow(["infra_failures", len(g0["infra_failures"])])
        writer.writerow(["graded_executions", g1["valid_count"]])
        writer.writerow(["pass_count", g1["pass_count"]])
        writer.writerow(["fail_count", g1["fail_count"]])
        writer.writerow(["pass_pct", f"{g1['pass_pct']:.1f}"])
        writer.writerow(["fail_pct", f"{g1['fail_pct']:.1f}"])
        writer.writerow(["test_cases", len(gc["aggregated"])])
        writer.writerow(["flaky_test_cases", len(gc["flaky"])])
        writer.writerow(["hallucinations_flagged", len(g2["flagged"])])
        writer.writerow(["hallucination_induced_rate_adversarial_pct", f"{g2['hallucination_induced_rate']:.1f}"])
        writer.writerow(["regressions_newly_failing", len(greg["newly_failing"])])
        writer.writerow(["regressions_newly_passing", len(greg["newly_passing"])])
    return path


def write_consistency_csv(run_id: str, gc: dict) -> str:
    path = os.path.join(config.RESULTS_DIR, f"consistency_{run_id}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["test_id", "verdict", "repetitions", "pass_count", "graded_count", "consistency_pct"])
        writer.writeheader()
        for a in gc["aggregated"]:
            writer.writerow({k: a[k] for k in ["test_id", "verdict", "repetitions", "pass_count", "graded_count", "consistency_pct"]})
    return path


def write_ac_summary_csv(run_id: str, g1: dict, g2: dict) -> str:
    """Per-acceptance-criteria bulk (happy_path + negative) pass/fail % --
    the number the report exists to answer per AC, plus how many
    hallucinations were flagged for that AC's executions."""
    path = os.path.join(config.RESULTS_DIR, f"ac_summary_{run_id}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ac_id", "pass_count", "fail_count", "total", "pass_pct", "fail_pct", "hallucinations_flagged"])
        writer.writeheader()
        for ac_id, b in sorted(g1["by_ac"].items()):
            total = b["total"] or 1
            writer.writerow({
                "ac_id": ac_id,
                "pass_count": b["pass"],
                "fail_count": b["fail"],
                "total": b["total"],
                "pass_pct": f"{b['pass'] / total * 100:.1f}",
                "fail_pct": f"{b['fail'] / total * 100:.1f}",
                "hallucinations_flagged": g2["by_ac"].get(ac_id, 0),
            })
    return path


def write_hallucination_worksheet(run_id: str, g2: dict, sample_size: int = 25) -> str:
    path = os.path.join(config.RESULTS_DIR, f"hallucination_worksheet_{run_id}.csv")
    sample = g2["flagged"][:sample_size]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["test_id", "repetition", "claim", "severity", "detail", "verified_in_salesforce", "notes"])
        writer.writeheader()
        for c in sample:
            writer.writerow({
                "test_id": c["test_id"],
                "repetition": c.get("repetition", ""),
                "claim": c["claim"],
                "severity": c["severity"],
                "detail": c["detail"],
                "verified_in_salesforce": "",
                "notes": "",
            })
    return path


def write_report_md(run_id: str, terminal_text: str) -> str:
    path = os.path.join(config.RESULTS_DIR, f"report_{run_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Agentforce Test Report — run `{run_id}`\n\n```\n{terminal_text}\n```\n")
    return path


def generate(run_id: str, cases_file: str = "test_cases.csv") -> str:
    data = load_run(run_id, cases_file)
    g0 = gate0_execution_integrity(data)
    g1 = gate1_pass_rate(data)
    gc = gate_consistency(data)
    g2 = gate2_hallucination_scan(data)
    greg = gate_regression(data)

    terminal_text = render_terminal(run_id, g0, g1, gc, g2, greg)
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    write_report_csv(run_id, g0, g1, gc, g2, greg)
    write_consistency_csv(run_id, gc)
    write_ac_summary_csv(run_id, g1, g2)
    write_hallucination_worksheet(run_id, g2)
    write_report_md(run_id, terminal_text)
    return terminal_text


def summarize(run_id: str, cases_file: str = "test_cases.csv") -> dict:
    """Machine-readable headline numbers for a run -- used for CLI exit-code
    policy (`--fail-on` / `--fail-under`) and the `--json` summary. Reads the
    same CSVs as generate(); does not write anything."""
    data = load_run(run_id, cases_file)
    g0 = gate0_execution_integrity(data)
    g1 = gate1_pass_rate(data)
    gc = gate_consistency(data)
    g2 = gate2_hallucination_scan(data)
    greg = gate_regression(data)
    return {
        "run_id": run_id,
        "cases_file": cases_file,
        "executions_total": g0["total"],
        "infra_failures": len(g0["infra_failures"]),
        "graded": g1["valid_count"],
        "passed": g1["pass_count"],
        "failed": g1["fail_count"],
        "pass_pct": round(g1["pass_pct"], 2),
        "flaky_tests": len(gc["flaky"]),
        "hallucinations_flagged": len(g2["flagged"]),
        "regressions": len(greg["newly_failing"]),
        "newly_passing": len(greg["newly_passing"]),
    }
