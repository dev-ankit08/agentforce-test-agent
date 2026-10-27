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
import logging_setup
import model_client
from agent_api_client import AgentApiClient, AgentApiError
from resilience import ModelCallError
from schemas import BULK_CATEGORIES, EXECUTION_RESULT_SCHEMA, validate

log = logging_setup.get_logger("executor")

RUN_CSV_FIELDS = ["run_id", "test_id", "repetition", "timestamp", "actual_topic", "actual_action", "actual_response_summary", "execution_status", "transcript_path"]

# Hard cap on turns in a multi_turn conversation test, so a looping or evasive
# agent (or a customer LLM that won't stop) can't run forever. Mirrors the
# agent-test-framework's turn-limit loop protection.
MAX_CONVERSATION_TURNS = 8


def _non_text_actions(messages: list[dict]) -> list[dict]:
    """The actions/tools the agent invoked -- every message that isn't plain
    conversational text."""
    return [m for m in messages if m.get("type") not in ("Text", "Inform")]


def _transcript_path(run_id: str, test_id: str, repetition: int) -> str:
    path = os.path.join(config.TRANSCRIPTS_DIR, run_id, f"{test_id}_rep{repetition}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _error_result(case: dict, run_id: str, repetition: int, transcript_path: str, exc: Exception) -> dict:
    return {
        "test_id": case["test_id"],
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


def _finalize(case: dict, result: dict, transcript_path: str) -> dict:
    validate(result, EXECUTION_RESULT_SCHEMA)
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump({"test_case": case, "result": result}, f, indent=2, default=str)
    return result


def run_test_case(case: dict, run_id: str, client: AgentApiClient, repetition: int = 1) -> dict:
    """Runs one (test case, repetition) pair end to end against the live agent
    and returns an ExecutionResult-shaped dict. Dispatches to the multi-turn
    conversation runner when the case declares conversation_mode=multi_turn.
    Any Agent API failure is captured as execution_status=ERROR rather than
    raised, so one bad execution doesn't kill the whole bulk run."""
    if (case.get("conversation_mode") or "").strip() == "multi_turn":
        return run_conversation(case, run_id, client, repetition)

    test_id = case["test_id"]
    transcript_path = _transcript_path(run_id, test_id, repetition)

    try:
        session_id = client.start_session()
        try:
            response = client.send_message(session_id, case["utterance"], sequence_id=1)
        finally:
            client.end_session(session_id)

        messages = response.get("messages", [])
        final_message = messages[-1] if messages else {}
        actions_taken = _non_text_actions(messages)

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
    except Exception as exc:  # isolate ANY failure as an ERROR row, never abort the batch
        log.warning("execution error for %s rep%d: %s", test_id, repetition, exc)
        result = _error_result(case, run_id, repetition, transcript_path, exc)

    return _finalize(case, result, transcript_path)


def run_conversation(case: dict, run_id: str, client: AgentApiClient, repetition: int = 1) -> dict:
    """Runs one multi_turn conversation: the `utterance` seeds the first turn,
    then model_client.call_customer role-plays the customer for each subsequent
    turn following the case's `scenario`, until the customer stops or
    MAX_CONVERSATION_TURNS is reached. One Agent API session spans all turns,
    with an incrementing sequenceId. Produces the same ExecutionResult shape as
    a single-turn run, plus a `turns` list; final_response is the agent's last
    message and actions_taken is the union of actions across all turns."""
    test_id = case["test_id"]
    transcript_path = _transcript_path(run_id, test_id, repetition)
    scenario = case.get("scenario") or case.get("utterance", "")

    try:
        turns: list[dict] = []
        history: list[dict] = []
        all_actions: list[dict] = []
        actual_topic = ""
        actual_action = ""
        final_response = ""

        session_id = client.start_session()
        try:
            for turn_no in range(1, MAX_CONVERSATION_TURNS + 1):
                if turn_no == 1 and (case.get("utterance") or "").strip():
                    user_msg = case["utterance"].strip()
                else:
                    try:
                        customer = model_client.call_customer(scenario, history)
                    except ModelCallError as exc:
                        # The customer-simulator model failed mid-conversation.
                        # If we already have turns, grade what we gathered; if not,
                        # re-raise so this becomes an ERROR row (nothing to grade).
                        log.warning("customer model failed for %s at turn %d: %s", test_id, turn_no, exc)
                        if turns:
                            break
                        raise
                    if (customer.get("status") or "").strip() == "stop":
                        break
                    user_msg = (customer.get("message") or "").strip()
                    if not user_msg:
                        break

                response = client.send_message(session_id, user_msg, sequence_id=turn_no)
                messages = response.get("messages", [])
                final_message = messages[-1] if messages else {}
                turn_actions = _non_text_actions(messages)
                all_actions.extend(turn_actions)

                agent_text = final_message.get("message", "")
                actual_topic = response.get("topicName") or final_message.get("topicName", "") or actual_topic
                actual_action = response.get("actionName") or final_message.get("actionName", "") or actual_action
                final_response = agent_text

                turns.append({
                    "sequence_id": turn_no,
                    "customer": user_msg,
                    "agent": agent_text,
                    "actions": turn_actions,
                    "topic": actual_topic,
                })
                history.append({"customer": user_msg, "agent": agent_text})
        finally:
            client.end_session(session_id)

        result = {
            "test_id": test_id,
            "run_id": run_id,
            "repetition": repetition,
            "execution_status": "OK",
            "actual_topic": actual_topic,
            "actual_action": actual_action,
            "final_response": final_response,
            "actions_taken": all_actions,
            "transcript_path": transcript_path,
            "turns": turns,
        }
    except Exception as exc:  # isolate ANY failure as an ERROR row, never abort the batch
        log.warning("conversation error for %s rep%d: %s", test_id, repetition, exc)
        result = _error_result(case, run_id, repetition, transcript_path, exc)

    return _finalize(case, result, transcript_path)


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
        # Belt-and-suspenders: run_test_case already isolates its own failures,
        # but creating the client (which resolves `sf` auth) can also raise, so
        # the whole worker is guarded -- one bad job must never abort the batch.
        try:
            client = AgentApiClient()
            return idx, run_test_case(case, run_id, client, repetition=rep)
        except Exception as exc:
            log.warning("worker failed for %s rep%d: %s", case.get("test_id"), rep, exc)
            path = _transcript_path(run_id, case["test_id"], rep)
            return idx, _finalize(case, _error_result(case, run_id, rep, path, exc), path)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_worker, i, case, rep) for i, (case, rep) in enumerate(jobs)]
        for future in as_completed(futures):
            try:
                idx, result = future.result()
                results[idx] = result
            except Exception as exc:  # should not happen (worker is guarded), but never crash the run
                log.error("unexpected worker crash, continuing: %s", exc)

    results = [r for r in results if r is not None]

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
