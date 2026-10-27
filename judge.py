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

import assertions
import config
import logging_setup
import model_client
from resilience import ModelCallError
from schemas import HALLUCINATION_CLAIM_SCHEMA, JUDGE_VERDICT_SCHEMA, validate

log = logging_setup.get_logger("judge")

JUDGED_CSV_FIELDS = ["run_id", "test_id", "repetition", "verdict", "topic_match", "action_match", "rationale", "assertion_detail"]


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
            "assertion_detail": "",
        }
        return verdict, []

    # Deterministic trace assertions first. If the case declares expected_actions
    # / expected_action_path and they don't hold, that's an unambiguous FAIL -- no
    # model call needed (and no hallucination audit, since the behavior is already
    # wrong). Otherwise fall through to the LLM judge, recording the assertion result.
    checked = assertions.evaluate(test_case, execution_result)
    if checked["has_hard_assertions"] and not checked["passed"]:
        verdict = {
            "test_id": test_case["test_id"],
            "run_id": execution_result["run_id"],
            "repetition": execution_result["repetition"],
            "verdict": "FAIL",
            "topic_match": checked["topic_match"],
            "action_match": checked["action_match"],
            "rationale": f"Deterministic assertion failed: {checked['detail']}",
            "assertion_detail": checked["detail"],
        }
        return verdict, []

    try:
        raw = model_client.call_judge(test_case, execution_result)
    except ModelCallError as exc:
        # The judge model itself failed (after retries) -- that's an infra
        # failure, not the agent's fault. Exclude it rather than crash grade_all,
        # same treatment as a Gate-0 execution error.
        log.warning("judge model call failed for %s: %s -- excluding", test_case["test_id"], exc)
        verdict = {
            "test_id": test_case["test_id"],
            "run_id": execution_result["run_id"],
            "repetition": execution_result["repetition"],
            "verdict": "EXCLUDED",
            "topic_match": checked["topic_match"],
            "action_match": checked["action_match"],
            "rationale": f"Judge model call failed, not graded: {exc}",
            "assertion_detail": checked["detail"] if checked["has_hard_assertions"] else "",
        }
        return verdict, []

    claims = raw.pop("hallucinations", [])
    raw["test_id"] = test_case["test_id"]
    raw["run_id"] = execution_result["run_id"]
    raw["repetition"] = execution_result["repetition"]
    raw["assertion_detail"] = checked["detail"] if checked["has_hard_assertions"] else ""
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
