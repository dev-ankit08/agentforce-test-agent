"""
judge.py -- grades each execution result against its test case's
expected_outcome via Claude, and pulls the hallucination audit out of that
same model response (see model_client.call_judge / JUDGE_SYSTEM_PROMPT) --
one call does both jobs, so there's no separate hallucination-detector
model call anywhere in this project. Tests that errored at execution
(Gate 0) are recorded as EXCLUDED, not graded, and not audited -- an infra
failure isn't the agent's fault. Grades one row per (test_id, repetition)
-- see executor.py.
"""
from __future__ import annotations

import csv
import os

import config
import model_client
from schemas import HALLUCINATION_CLAIM_SCHEMA, JUDGE_VERDICT_SCHEMA, validate

JUDGED_CSV_FIELDS = ["run_id", "test_id", "repetition", "verdict", "topic_match", "action_match", "rationale"]


def grade(test_case: dict, execution_result: dict) -> tuple[dict, list[dict]]:
    """Returns (verdict, hallucination_claims) for one execution."""
    if execution_result["execution_status"] != "OK":
        verdict = {
            "test_id": test_case["test_id"],
            "run_id": execution_result["run_id"],
            "repetition": execution_result["repetition"],
            "verdict": "EXCLUDED",
            "topic_match": False,
            "action_match": False,
            "rationale": f"Execution error, not graded: {execution_result.get('error_detail', '')}",
        }
        return verdict, []

    raw = model_client.call_judge(test_case, execution_result)
    claims = raw.pop("hallucinations", [])
    raw["test_id"] = test_case["test_id"]
    raw["run_id"] = execution_result["run_id"]
    raw["repetition"] = execution_result["repetition"]
    validate(raw, JUDGE_VERDICT_SCHEMA)

    for claim in claims:
        claim["test_id"] = execution_result["test_id"]
        claim["run_id"] = execution_result["run_id"]
        claim["repetition"] = execution_result["repetition"]
        validate(claim, HALLUCINATION_CLAIM_SCHEMA)

    return raw, claims


def grade_all(cases_by_id: dict[str, dict], execution_results: list[dict], run_id: str) -> tuple[list[dict], list[dict]]:
    """Returns (verdicts, hallucination_claims) for the whole run and writes
    judged_<run_id>.csv. Hallucination claims still need
    hallucination.write_claims() to be persisted -- see run.py/app.py."""
    verdicts: list[dict] = []
    all_claims: list[dict] = []
    for r in execution_results:
        verdict, claims = grade(cases_by_id[r["test_id"]], r)
        verdicts.append(verdict)
        all_claims.extend(claims)

    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    path = os.path.join(config.RESULTS_DIR, f"judged_{run_id}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JUDGED_CSV_FIELDS)
        writer.writeheader()
        for v in verdicts:
            writer.writerow({k: v[k] for k in JUDGED_CSV_FIELDS})
    return verdicts, all_claims
