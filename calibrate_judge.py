#!/usr/bin/env python3
"""
calibrate_judge.py -- a trust signal for the LLM judge.

Runs the real judge (model_client.call_judge via judge.grade) against a small
set of hand-labelled fixtures whose correct PASS/FAIL verdict we already know,
and reports the agreement rate. Low agreement means the judge prompt/model
drifted and verdicts shouldn't be trusted blindly.

Needs ANTHROPIC_API_KEY (it makes real model calls). Run occasionally / after
changing the judge prompt or model:

    python3 calibrate_judge.py
"""
from __future__ import annotations

import judge
import logging_setup

# Each fixture: (label, test_case, execution_result, expected_verdict)
FIXTURES = [
    (
        "clean order status -> PASS",
        {"test_id": "F1", "expected_topic": "Order_Status", "expected_action": "Get_Order_Status",
         "expected_outcome": "States the order status and a date, from the lookup action."},
        {"test_id": "F1", "run_id": "cal", "repetition": 1, "execution_status": "OK",
         "actual_topic": "Order_Status", "actual_action": "Get_Order_Status",
         "final_response": "Your order #ORD-48213 shipped on June 3 and is out for delivery today.",
         "actions_taken": [{"type": "Action", "actionName": "Get_Order_Status", "output": "status=shipped; ship_date=2026-06-03"}],
         "transcript_path": "n/a"},
        "PASS",
    ),
    (
        "fabricated discount code -> FAIL",
        {"test_id": "F2", "expected_topic": "Order_Status", "expected_action": "none",
         "expected_outcome": "Does not invent a discount code; declines or escalates."},
        {"test_id": "F2", "run_id": "cal", "repetition": 1, "execution_status": "OK",
         "actual_topic": "Order_Status", "actual_action": "",
         "final_response": "Sorry about the delay! Here's 20% off: code SAVE20NOW.",
         "actions_taken": [],
         "transcript_path": "n/a"},
        "FAIL",
    ),
    (
        "evasive non-answer -> FAIL",
        {"test_id": "F3", "expected_topic": "Returns", "expected_action": "Check_Return_Eligibility",
         "expected_outcome": "States whether the item is within the 30-day return window, citing the delivery date."},
        {"test_id": "F3", "run_id": "cal", "repetition": 1, "execution_status": "OK",
         "actual_topic": "Returns", "actual_action": "Check_Return_Eligibility",
         "final_response": "Returns can sometimes be possible depending on various factors.",
         "actions_taken": [{"type": "Action", "actionName": "Check_Return_Eligibility", "output": "delivered=2026-05-01; eligible=false"}],
         "transcript_path": "n/a"},
        "FAIL",
    ),
]


def main() -> int:
    logging_setup.configure()
    agree = 0
    for label, tc, er, expected in FIXTURES:
        verdict, _ = judge.grade(tc, er)
        got = verdict["verdict"]
        ok = got == expected
        agree += ok
        print(f"  [{'OK ' if ok else 'XX '}] {label}: expected {expected}, got {got}")
    pct = agree / len(FIXTURES) * 100
    print(f"\nJudge agreement: {agree}/{len(FIXTURES)} = {pct:.0f}%")
    return 0 if agree == len(FIXTURES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
