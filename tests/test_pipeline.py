"""
Mock-based integration tests: exercise executor -> judge -> aggregate without
any network (no Anthropic, no Salesforce). Proves the production-hardening
guarantees: one bad execution never aborts the batch, a judge-model failure is
isolated as EXCLUDED, and multi-turn conversations run end to end.
"""
from __future__ import annotations

import csv
import os

import pytest

import config
import executor
import judge
import model_client
import report
from agent_api_client import AgentApiError
from resilience import ModelCallError


class FakeClient:
    """Stand-in for AgentApiClient. Raises on an utterance containing 'BOOM' to
    simulate an Agent API failure; otherwise returns a canned response."""

    def __init__(self, *a, **k):
        pass

    def start_session(self):
        return "sess-1"

    def send_message(self, session_id, text, sequence_id):
        if "BOOM" in text:
            raise AgentApiError("simulated Agent API 500")
        return {
            "topicName": "Order_Status",
            "actionName": "GetOrder",
            "messages": [
                {"type": "Action", "actionName": "GetOrder", "output": "order #42 shipped"},
                {"type": "Text", "message": f"reply to: {text}"},
            ],
        }

    def end_session(self, session_id):
        pass


@pytest.fixture
def tmp_io(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(config, "TRANSCRIPTS_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setattr(config, "RUN_HISTORY_CSV", str(tmp_path / "results" / "run_history.csv"))
    monkeypatch.setattr(executor, "AgentApiClient", FakeClient)
    return tmp_path


def _case(test_id, utterance, **extra):
    base = {
        "test_id": test_id, "utterance": utterance, "expected_topic": "Order_Status",
        "expected_action": "GetOrder", "expected_outcome": "shows the order", "category": "happy_path", "ac_id": "AC1",
    }
    base.update(extra)
    return base


def test_bad_execution_is_isolated(tmp_io, monkeypatch):
    monkeypatch.setattr(model_client, "call_judge", lambda tc, er: {
        "verdict": "PASS", "topic_match": True, "action_match": True, "rationale": "ok", "hallucinations": []})

    cases = [_case("TC-1", "where is my order"), _case("TC-2", "BOOM")]
    results = executor.run_bulk(cases, "run1", concurrency=2, repetitions=1)

    assert len(results) == 2  # batch completed despite the failure
    by_id = {r["test_id"]: r for r in results}
    assert by_id["TC-1"]["execution_status"] == "OK"
    assert by_id["TC-2"]["execution_status"] == "ERROR"

    verdicts, _ = judge.grade_all({c["test_id"]: c for c in cases}, results, "run1")
    v = {x["test_id"]: x for x in verdicts}
    assert v["TC-1"]["verdict"] == "PASS"
    assert v["TC-2"]["verdict"] == "EXCLUDED"  # infra error excluded, not graded


def test_judge_model_failure_is_excluded(tmp_io, monkeypatch):
    def boom(tc, er):
        raise ModelCallError("judge model unavailable")

    monkeypatch.setattr(model_client, "call_judge", boom)
    cases = [_case("TC-1", "where is my order")]
    results = executor.run_bulk(cases, "run2", concurrency=1, repetitions=1)
    verdicts, claims = judge.grade_all({c["test_id"]: c for c in cases}, results, "run2")

    assert verdicts[0]["verdict"] == "EXCLUDED"  # model failure isolated
    assert claims == []
    # grade_all still wrote its CSV -- the run didn't crash
    assert os.path.exists(os.path.join(config.RESULTS_DIR, "judged_run2.csv"))


def test_multi_turn_runs_turns(tmp_io, monkeypatch):
    turns_seen = {"n": 0}

    def fake_customer(scenario, history):
        turns_seen["n"] += 1
        if turns_seen["n"] >= 2:
            return {"message": "", "status": "stop"}
        return {"message": "and when will it arrive?", "status": "continue"}

    monkeypatch.setattr(model_client, "call_customer", fake_customer)
    case = _case("TC-MT", "where is my order", conversation_mode="multi_turn", scenario="customer chasing an order")
    results = executor.run_bulk([case], "run3", concurrency=1, repetitions=1)

    r = results[0]
    assert r["execution_status"] == "OK"
    assert len(r["turns"]) >= 2  # seeded turn + at least one customer-driven turn
    assert r["turns"][0]["customer"] == "where is my order"


def test_deterministic_assertion_fail_skips_llm(tmp_io, monkeypatch):
    # If the LLM judge were called this would raise and fail the test; the hard
    # assertion must short-circuit to a deterministic FAIL with no model call.
    monkeypatch.setattr(model_client, "call_judge", lambda tc, er: (_ for _ in ()).throw(AssertionError("judge should not be called")))
    case = _case("TC-A", "where is my order", expected_actions="GetOrder -> SendEmail")
    results = executor.run_bulk([case], "run4", concurrency=1, repetitions=1)
    verdicts, _ = judge.grade_all({c["test_id"]: c for c in case and [case]}, results, "run4")
    assert verdicts[0]["verdict"] == "FAIL"
    assert "SendEmail" in verdicts[0]["assertion_detail"]


def test_report_summarize_counts(tmp_io, monkeypatch):
    monkeypatch.setattr(model_client, "call_judge", lambda tc, er: {
        "verdict": "PASS", "topic_match": True, "action_match": True, "rationale": "ok", "hallucinations": []})
    cases = [_case("TC-1", "a"), _case("TC-2", "b")]

    cases_path = os.path.join(tmp_io, "cases.csv")
    with open(cases_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cases[0].keys()))
        w.writeheader()
        w.writerows(cases)

    results = executor.run_bulk(cases, "run5", concurrency=2, repetitions=1)
    verdicts, claims = judge.grade_all({c["test_id"]: c for c in cases}, results, "run5")
    import hallucination
    import regression
    hallucination.write_claims(claims, results, "run5")
    agg = report.aggregate_verdicts_by_test(verdicts)
    regression.append_to_history("run5", cases_path, agg, "2026-01-01T00:00:00Z")
    regression.diff_against_previous(cases_path, "run5", agg)
    report.generate("run5", cases_path)

    summary = report.summarize("run5", cases_path)
    assert summary["passed"] == 2
    assert summary["failed"] == 0
    assert summary["pass_pct"] == 100.0
    assert summary["infra_failures"] == 0
