"""
hallucination.py -- persists the hallucination claims judge.py already
extracted as part of its single graded call to Claude (see
model_client.call_judge / JUDGE_SYSTEM_PROMPT). There is no model call in
this module -- auditing every claim against that same test's own
action/tool outputs happens inside the judge call, at no extra API cost.
Only unsupported claims are written to results/hallucinations_<run_id>.csv
(the flag list); a summary row per (test_id, repetition) (claims checked /
unsupported) goes to results/hallucination_summary_<run_id>.csv so a clean
transcript still shows up in the report.
"""
from __future__ import annotations

import csv
import os

import config

CLAIMS_CSV_FIELDS = ["run_id", "test_id", "repetition", "claim", "evidence_status", "severity", "detail"]
SUMMARY_CSV_FIELDS = ["run_id", "test_id", "repetition", "claims_checked", "claims_unsupported"]


def write_claims(all_claims: list[dict], execution_results: list[dict], run_id: str) -> list[dict]:
    """all_claims: every claim judge.grade_all() pulled out of the merged
    judge/hallucination responses (both supported and unsupported), already
    stamped with test_id/run_id/repetition. execution_results: this run's
    executions, used only so every execution -- even one with zero claims
    -- gets a summary row."""
    claims_by_execution: dict[tuple, list[dict]] = {}
    for c in all_claims:
        claims_by_execution.setdefault((c["test_id"], c["repetition"]), []).append(c)

    summary_rows = []
    for r in execution_results:
        claims = claims_by_execution.get((r["test_id"], r["repetition"]), [])
        unsupported = [c for c in claims if c["evidence_status"] == "unsupported"]
        summary_rows.append({
            "run_id": run_id,
            "test_id": r["test_id"],
            "repetition": r["repetition"],
            "claims_checked": len(claims),
            "claims_unsupported": len(unsupported),
        })

    os.makedirs(config.RESULTS_DIR, exist_ok=True)

    flagged = [c for c in all_claims if c["evidence_status"] == "unsupported"]
    with open(os.path.join(config.RESULTS_DIR, f"hallucinations_{run_id}.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CLAIMS_CSV_FIELDS)
        writer.writeheader()
        for c in flagged:
            writer.writerow({k: c[k] for k in CLAIMS_CSV_FIELDS})

    with open(os.path.join(config.RESULTS_DIR, f"hallucination_summary_{run_id}.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(summary_rows)

    return flagged
