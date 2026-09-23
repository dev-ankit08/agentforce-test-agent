#!/usr/bin/env python3
"""
run.py -- CLI entry point.

  python3 run.py design  --story story.txt --out test_cases.csv
  python3 run.py execute --cases test_cases.csv [--concurrency 4] [--run-id ID]
  python3 run.py report  --run <run_id> [--cases test_cases.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time

# Windows terminals often default to a non-UTF-8 codepage; without this,
# the report's em dashes/arrows get silently mangled on print (they're
# written to files as UTF-8 regardless, via explicit encoding= everywhere).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import executor
import hallucination
import judge
import regression
import report
import test_designer


def _new_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def cmd_design(args: argparse.Namespace) -> int:
    cases = test_designer.design(args.story, args.out)
    print(f"Wrote {len(cases)} test case(s) to {args.out}")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    with open(args.cases, newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))
    cases_by_id = {c["test_id"]: c for c in cases}

    run_id = args.run_id or _new_run_id()
    total_executions = executor.planned_execution_count(cases, args.repetitions)
    print(
        f"Running {len(cases)} test case(s) ({args.repetitions} repetition(s) for happy_path/negative, "
        f"1 for edge_case/adversarial) = {total_executions} executions "
        f"against the live agent (run_id={run_id}, concurrency={args.concurrency})..."
    )
    execution_results = executor.run_bulk(cases, run_id, concurrency=args.concurrency, repetitions=args.repetitions)

    print("Grading responses and auditing for hallucinations...")
    verdicts, hallucination_claims = judge.grade_all(cases_by_id, execution_results, run_id)
    hallucination.write_claims(hallucination_claims, execution_results, run_id)

    print("Diffing against previous run (aggregated per test case across repetitions)...")
    aggregated = report.aggregate_verdicts_by_test(verdicts)
    regression.append_to_history(run_id, args.cases, aggregated, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    regression.diff_against_previous(args.cases, run_id, aggregated)

    print(f"\nDone. run_id = {run_id}")
    print(f"Next: python3 run.py report --run {run_id} --cases {args.cases}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    text = report.generate(args.run, args.cases)
    print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentforce bulk/regression test runner.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_design = sub.add_parser("design", help="Generate test_cases.csv from a user story.")
    p_design.add_argument("--story", required=True)
    p_design.add_argument("--out", default="test_cases.csv")
    p_design.set_defaults(func=cmd_design)

    p_execute = sub.add_parser("execute", help="Run all test cases against the live agent.")
    p_execute.add_argument("--cases", default="test_cases.csv")
    p_execute.add_argument("--concurrency", type=int, default=4)
    p_execute.add_argument("--repetitions", type=int, default=1, help="Run each happy_path/negative test case this many times, to catch flaky/inconsistent responses. edge_case/adversarial cases always run once.")
    p_execute.add_argument("--run-id", default=None)
    p_execute.set_defaults(func=cmd_execute)

    p_report = sub.add_parser("report", help="Generate the report for a run.")
    p_report.add_argument("--run", required=True)
    p_report.add_argument("--cases", default="test_cases.csv")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
