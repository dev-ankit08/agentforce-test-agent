"""
test_assertions.py -- offline unit tests for the pure-Python assertion and
normalization logic (no Anthropic / Salesforce / network needed). Run with:

    python3 -m pytest test_assertions.py      # if pytest is installed
    python3 test_assertions.py                # plain-stdlib fallback runner
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assertions


def test_normalize_strips_id_suffix_underscores_case():
    assert assertions.normalize("Order_Status_16j0x000000XyZa") == "order status"
    assert assertions.normalize("CheckOrder") == "checkorder"
    assert assertions.normalize(None) == ""


def test_names_match_direct_and_aliased():
    assert assertions.names_match("Order_Status", "order status")
    assert not assertions.names_match("Order Status", "Billing")
    aliases = [["Order Status", "Check Order"]]
    assert assertions.names_match("Order_Status_16j0x000000XyZa", "Check_Order", aliases)
    assert not assertions.names_match("Order Status", "Check Order")  # no aliases passed


def test_action_names_extraction():
    actions = [
        {"type": "Action", "actionName": "GetOrder"},
        {"type": "Action", "name": "Notify"},
        {"type": "Apex"},  # falls back to type
        "garbage",         # ignored
    ]
    assert assertions.action_names(actions) == ["GetOrder", "Notify", "Apex"]


def test_match_sequence_wildcards():
    m = assertions._match_sequence
    assert m(["A", "B", "C"], ["A", "B", "C"], None)
    assert not m(["A", "B"], ["A", "B", "C"], None)       # exact length required w/o '...'
    assert m(["A", "...", "C"], ["A", "X", "Y", "C"], None)
    assert m(["...", "C"], ["X", "C"], None)              # leading wildcard
    assert m(["A", "..."], ["A", "X", "Y"], None)         # trailing wildcard
    assert m(["..."], [], None)                           # wildcard matches empty
    assert not m(["A", "C"], ["A", "B"], None)


def test_evaluate_passes_when_no_hard_assertions():
    case = {"expected_topic": "Order_Status", "expected_action": "none"}
    exec_result = {"actual_topic": "order status", "actual_action": "", "actions_taken": []}
    out = assertions.evaluate(case, exec_result)
    assert out["has_hard_assertions"] is False
    assert out["passed"] is True
    assert out["topic_match"] is True
    assert out["action_match"] is True  # "none" expected and no actions invoked


def test_evaluate_hard_fail_on_missing_action():
    case = {"expected_topic": "Order_Status", "expected_action": "none", "expected_actions": "GetOrder -> SendEmail"}
    exec_result = {"actual_topic": "Order_Status", "actual_action": "", "actions_taken": [{"type": "Action", "actionName": "GetOrder"}]}
    out = assertions.evaluate(case, exec_result)
    assert out["has_hard_assertions"] is True
    assert out["passed"] is False
    assert "SendEmail" in out["detail"]


def test_evaluate_hard_pass_on_action_path():
    case = {"expected_topic": "T", "expected_action": "none", "expected_action_path": "GetOrder -> ... -> SendEmail"}
    exec_result = {
        "actual_topic": "T",
        "actual_action": "",
        "actions_taken": [
            {"type": "Action", "actionName": "GetOrder"},
            {"type": "Action", "actionName": "Lookup"},
            {"type": "Action", "actionName": "SendEmail"},
        ],
    }
    out = assertions.evaluate(case, exec_result)
    assert out["has_hard_assertions"] is True
    assert out["passed"] is True


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
