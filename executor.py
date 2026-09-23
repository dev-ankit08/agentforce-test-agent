"""
executor.py -- drives the Agentforce Agent API for every test case in bulk.
No LLM calls here. Writes one transcript JSON per (test case, repetition)
and a structured results/run_<run_id>.csv summary row per execution.

"Repetitions" is how many times a test case is run in this bulk pass -- the
same utterance can get a different response from run to run since the
agent is LLM-backed, so repeating a case is how flakiness gets caught (see
report.gate_consistency). This only applies to the happy_path/negative
"bulk pair" per acceptance criterion (schemas.BULK_CATEGORIES); edge_case
and adversarial cases always run exactly once -- they're one-shot checks,
not part of the bulk consistency measurement.
"""
from __future__ import annotations

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from agent_api_client import AgentApiClient, AgentApiError
from schemas import BULK_CATEGORIES, EXECUTION_RESULT_SCHEMA, validate

RUN_CSV_FIELDS = ["run_id", "test_id", "repetition", "timestamp", "actual_topic", "actual_action", "actual_response_summary", "execution_status", "transcript_path"]


def run_test_case(case: dict, run_id: str, client: AgentApiClient, repetition: int = 1) -> dict:
    """Runs one (test case, repetition) pair end to end against the live agent
    and returns an ExecutionResult-shaped dict. Any Agent API failure is
    captured as execution_status=ERROR rather than raised, so one bad
    execution doesn't kill the whole bulk run."""
    test_id = case["test_id"]
    transcript_path = os.path.join(config.TRANSCRIPTS_DIR, run_id, f"{test_id}_rep{repetition}.json")
    os.makedirs(os.path.dirname(transcript_path), exist_ok=True)

    try:
        session_id = client.start_session()
        try:
            response = client.send_message(session_id, case["utterance"], sequence_id=1)
        finally:
            client.end_session(session_id)

        messages = response.get("messages", [])
        final_message = messages[-1] if messages else {}
        actions_taken = [m for m in messages if m.get("type") not in ("Text", "Inform")]

        result = {
            "test_id": test_id,
            "run_id": run_id,
            "repetition": repetition,
            "execution_status": "OK",
            "actual_topic": response.get("topicName") or final_message.get("topicName", ""),
            "actual_action": response.get("actionName") or final_message.get("actionName", ""),
            "final_response": final_message.get("message", ""),
            "actions_taken": actions_taken,
            "transcript_path": transcript_path,
        }
    except AgentApiError as exc:
        result = {
            "test_id": test_id,
            "run_id": run_id,
            "repetition": repetition,
            "execution_status": "ERROR",
            "actual_topic": "",
            "actual_action": "",
            "final_response": "",
            "actions_taken": [],
            "transcript_path": transcript_path,
            "error_detail": str(exc),
        }

    validate(result, EXECUTION_RESULT_SCHEMA)
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump({"test_case": case, "result": result}, f, indent=2, default=str)
    return result


def _repetitions_for(case: dict, repetitions: int) -> int:
    """happy_path/negative cases run `repetitions` times; everything else
    (edge_case, adversarial) runs exactly once."""
    return repetitions if case.get("category") in BULK_CATEGORIES else 1


def planned_execution_count(cases: list[dict], repetitions: int) -> int:
    """Total Agent API executions a run_bulk() call with these cases/
    repetitions will make -- for progress/total display, since it's no
    longer a flat `len(cases) * repetitions` now that only bulk categories
    repeat."""
    repetitions = max(1, repetitions)
    return sum(_repetitions_for(case, repetitions) for case in cases)


def run_bulk(cases: list[dict], run_id: str, concurrency: int = None, repetitions: int = 1) -> list[dict]:
    """Runs every happy_path/negative case `repetitions` times and every
    edge_case/adversarial case once, spread across a thread pool since each
    execution is an independent Agent API session."""
    concurrency = concurrency or config.DEFAULT_CONCURRENCY
    repetitions = max(1, repetitions)
    jobs = [(case, rep) for case in cases for rep in range(1, _repetitions_for(case, repetitions) + 1)]
    results = [None] * len(jobs)

    def _worker(idx: int, case: dict, rep: int) -> tuple[int, dict]:
        client = AgentApiClient()
        return idx, run_test_case(case, run_id, client, repetition=rep)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_worker, i, case, rep) for i, (case, rep) in enumerate(jobs)]
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result

    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    run_csv_path = os.path.join(config.RESULTS_DIR, f"run_{run_id}.csv")
    with open(run_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RUN_CSV_FIELDS)
        writer.writeheader()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for r in results:
            writer.writerow({
                "run_id": r["run_id"],
                "test_id": r["test_id"],
                "repetition": r["repetition"],
                "timestamp": ts,
                "actual_topic": r["actual_topic"],
                "actual_action": r["actual_action"],
                "actual_response_summary": (r["final_response"] or "")[:200],
                "execution_status": r["execution_status"],
                "transcript_path": r["transcript_path"],
            })
    return results
