#!/usr/bin/env python3
"""
run.py -- CLI entry point.

  python3 run.py design  --story story.txt --out test_cases.csv
  python3 run.py execute --cases test_cases.csv [--concurrency 4] [--run-id ID]
                         [--repetitions N] [--fail-on {none,regression,failure}]
                         [--fail-under PCT] [--json]
  python3 run.py report  --run <run_id> [--cases test_cases.csv] [--json]
  python3 run.py run     --cases test_cases.csv [--story story.txt] [...]   # design?+execute+report
  python3 run.py doctor                                                     # preflight checks

`execute`/`run` return a non-zero exit code per the --fail-on/--fail-under
policy, so a scheduled job (cron/launchd) or CI step can gate on the result.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time

# Windows terminals often default to a non-UTF-8 codepage; without this,
# the report's em dashes/arrows get silently mangled on print (they're
# written to files as UTF-8 regardless, via explicit encoding= everywhere).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import config
import executor
import hallucination
import judge
import logging_setup
import regression
import report
import test_designer
from version import __version__

log = logging_setup.get_logger("cli")


def _new_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, shell=False,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _write_run_meta(run_id: str, cases_file: str, planned: int, actual: int, timings: dict, summary: dict) -> str:
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    meta = {
        "run_id": run_id,
        "tool_version": __version__,
        "git_sha": _git_sha(),
        "started_at": timings.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases_file": cases_file,
        "planned_executions": planned,
        "actual_executions": actual,
        "models": {"designer": config.DESIGNER_MODEL, "judge": config.JUDGE_MODEL, "customer": config.CUSTOMER_MODEL},
        "stage_seconds": {k: round(v, 2) for k, v in timings.get("stages", {}).items()},
        "summary": summary,
    }
    path = os.path.join(config.RESULTS_DIR, f"run_meta_{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return path


def _exit_policy(summary: dict, fail_on: str, fail_under: float | None) -> tuple[int, list[str]]:
    """Returns (exit_code, reasons). 0 = success."""
    reasons: list[str] = []
    if fail_under is not None and summary["pass_pct"] < fail_under:
        reasons.append(f"pass rate {summary['pass_pct']}% < --fail-under {fail_under}%")
    if fail_on in ("regression", "failure") and summary["regressions"] > 0:
        reasons.append(f"{summary['regressions']} regression(s)")
    if fail_on == "failure":
        if summary["failed"] > 0:
            reasons.append(f"{summary['failed']} failed execution(s)")
        if summary["infra_failures"] > 0:
            reasons.append(f"{summary['infra_failures']} infra failure(s)")
    return (1 if reasons else 0), reasons


def _run_pipeline(cases_file: str, run_id: str, concurrency: int, repetitions: int) -> tuple[int, int, dict]:
    """Shared execute->grade->audit->regress->report pipeline. Returns
    (planned, actual, timings). Same call order as before, now timed."""
    with open(cases_file, newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))
    cases_by_id = {c["test_id"]: c for c in cases}

    timings: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stages": {}}
    planned = executor.planned_execution_count(cases, repetitions)
    log.info("running %d case(s) = %d execution(s), run_id=%s, concurrency=%d", len(cases), planned, run_id, concurrency)

    t = time.monotonic()
    execution_results = executor.run_bulk(cases, run_id, concurrency=concurrency, repetitions=repetitions)
    timings["stages"]["execute"] = time.monotonic() - t

    log.info("grading responses and auditing for hallucinations")
    t = time.monotonic()
    verdicts, hallucination_claims = judge.grade_all(cases_by_id, execution_results, run_id)
    hallucination.write_claims(hallucination_claims, execution_results, run_id)
    timings["stages"]["grade"] = time.monotonic() - t

    log.info("diffing against previous run")
    t = time.monotonic()
    aggregated = report.aggregate_verdicts_by_test(verdicts)
    regression.append_to_history(run_id, cases_file, aggregated, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    regression.diff_against_previous(cases_file, run_id, aggregated)
    timings["stages"]["regress"] = time.monotonic() - t

    return planned, len(execution_results), timings


def _finish(cases_file: str, run_id: str, planned: int, actual: int, timings: dict, fail_on: str, fail_under, as_json: bool) -> int:
    """Generate the report, write run_meta, apply the exit policy."""
    text = report.generate(run_id, cases_file)
    summary = report.summarize(run_id, cases_file)
    meta_path = _write_run_meta(run_id, cases_file, planned, actual, timings, summary)
    code, reasons = _exit_policy(summary, fail_on, fail_under)

    if as_json:
        print(json.dumps({"exit_code": code, "reasons": reasons, "meta": meta_path, **summary}, indent=2))
    else:
        print(text)
        print(f"\nrun_id = {run_id}  |  run_meta: {meta_path}")
        if reasons:
            print("FAILED exit policy: " + "; ".join(reasons))
        else:
            print("Exit policy: OK")
    return code


def cmd_design(args: argparse.Namespace) -> int:
    cases = test_designer.design(args.story, args.out)
    print(f"Wrote {len(cases)} test case(s) to {args.out}")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    run_id = args.run_id or _new_run_id()
    planned, actual, timings = _run_pipeline(args.cases, run_id, args.concurrency, args.repetitions)
    return _finish(args.cases, run_id, planned, actual, timings, args.fail_on, args.fail_under, args.json)


def cmd_report(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(report.summarize(args.run, args.cases), indent=2))
    else:
        print(report.generate(args.run, args.cases))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.story:
        log.info("designing test cases from %s", args.story)
        test_designer.design(args.story, args.cases)
    run_id = args.run_id or _new_run_id()
    planned, actual, timings = _run_pipeline(args.cases, run_id, args.concurrency, args.repetitions)
    return _finish(args.cases, run_id, planned, actual, timings, args.fail_on, args.fail_under, args.json)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Preflight: fail fast and loud if a scheduled run would be misconfigured."""
    from agent_api_client import AgentApiError, _org_credentials

    problems: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    ok = bool(config.ANTHROPIC_API_KEY)
    checks.append(("ANTHROPIC_API_KEY set", ok, "" if ok else "export ANTHROPIC_API_KEY or add to .env"))
    if not ok:
        problems.append("ANTHROPIC_API_KEY")

    ok = bool(config.SF_ORG_ALIAS)
    checks.append(("AGENTFORCE_SF_ORG_ALIAS set", ok, "" if ok else "set AGENTFORCE_SF_ORG_ALIAS"))
    ok2 = bool(config.AGENT_API_NAME)
    checks.append(("AGENTFORCE_AGENT_API_NAME set", ok2, "" if ok2 else "set AGENTFORCE_AGENT_API_NAME"))
    if not ok:
        problems.append("AGENTFORCE_SF_ORG_ALIAS")
    if not ok2:
        problems.append("AGENTFORCE_AGENT_API_NAME")

    if config.SF_ORG_ALIAS:
        try:
            _org_credentials(config.SF_ORG_ALIAS)
            checks.append((f"sf org '{config.SF_ORG_ALIAS}' reachable", True, ""))
        except AgentApiError as exc:
            checks.append((f"sf org '{config.SF_ORG_ALIAS}' reachable", False, str(exc)))
            problems.append("sf org auth")

    print(f"agentforce-test v{__version__} doctor\n")
    for name, passed, hint in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {name}" + (f"  -- {hint}" if hint else ""))
    print("\n" + ("All checks passed." if not problems else f"{len(problems)} problem(s): {', '.join(problems)}"))
    return 0 if not problems else 1


def _add_pipeline_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--concurrency", type=int, default=config.DEFAULT_CONCURRENCY)
    p.add_argument("--repetitions", type=int, default=1, help="Run each happy_path/negative case this many times to catch flaky responses. edge_case/adversarial always run once.")
    p.add_argument("--run-id", default=None)
    p.add_argument("--fail-on", choices=["none", "regression", "failure"], default="regression", help="Exit non-zero on: nothing / any regression / any failure+regression+infra error.")
    p.add_argument("--fail-under", type=float, default=None, help="Exit non-zero if pass rate (%%) is below this threshold.")
    p.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary instead of the text report.")


def main() -> int:
    logging_setup.configure()
    parser = argparse.ArgumentParser(description="Agentforce bulk/regression test runner.")
    parser.add_argument("--version", action="version", version=f"agentforce-test {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_design = sub.add_parser("design", help="Generate test_cases.csv from a user story.")
    p_design.add_argument("--story", required=True)
    p_design.add_argument("--out", default="test_cases.csv")
    p_design.set_defaults(func=cmd_design)

    p_execute = sub.add_parser("execute", help="Run all test cases against the live agent and report.")
    p_execute.add_argument("--cases", default="test_cases.csv")
    _add_pipeline_flags(p_execute)
    p_execute.set_defaults(func=cmd_execute)

    p_run = sub.add_parser("run", help="One-shot: (optional design) + execute + report. Scheduler entry point.")
    p_run.add_argument("--cases", default="test_cases.csv")
    p_run.add_argument("--story", default=None, help="If given, (re)generate --cases from this story first.")
    _add_pipeline_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="Generate the report for a run.")
    p_report.add_argument("--run", required=True)
    p_report.add_argument("--cases", default="test_cases.csv")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    p_doctor = sub.add_parser("doctor", help="Preflight: validate env/config and org connectivity.")
    p_doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
