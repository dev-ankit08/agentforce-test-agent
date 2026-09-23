# Implementation notes

How this project is built, and why. For "how do I use it," see `USER_GUIDE.md`.

## Problem this solves

An AgentScript agent deployed in Salesforce needs bulk, repeatable testing:
turn a user story into test cases, run them all against the live agent,
grade pass/fail, catch hallucinated (fabricated) responses, and catch
regressions between runs -- without hand-running conversations one at a
time.

## Architecture

```
USER STORY (text)
   |
   v
TEST DESIGNER (Claude)  ->  test_cases.csv
   |
   v
EXECUTOR (Agent API, no LLM; bulk pair only repeats happy_path/negative)
   ->  transcripts/<run_id>/*.json + results/run_<run_id>.csv
   |
   v
JUDGE (Claude, one call: verdict + hallucination audit)
   ->  results/judged_<run_id>.csv + results/hallucinations_<run_id>.csv
   |
   v
REGRESSION DIFF (pure Python)  ->  results/regression_<run_id>.csv
   |
   v
REPORT (pure Python)  ->  terminal / Streamlit UI
   ->  results/report_<run_id>.md/.csv + results/ac_summary_<run_id>.csv
```

Every stage is a separate module with one job, so each can be tested,
re-run, or replaced independently. `app.py` (the Streamlit UI) and `run.py`
(the CLI) are both thin wrappers that call the exact same functions in the
exact same order -- there is no logic that exists only in one or the other.

## Module responsibilities

| File | Responsibility |
|---|---|
| `config.py` | Reads `.env` (via `python-dotenv`): models per role, Salesforce org alias, Agent API agent id. No stub/mock switches -- if a value is missing, the module that needs it fails loudly, not silently. |
| `agent_api_client.py` | The one Salesforce integration. Gets an access token + instance URL for the already-`sf`-authenticated org (`sf org display --target-org <alias> --json`) and drives the Agentforce Agent API session lifecycle over `requests`: `start_session()`, `send_message()`, `end_session()`. |
| `model_client.py` | The one Anthropic integration. Two functions, one per role: `call_test_designer()` and `call_judge()` (grading + hallucination audit in one call). Each sends a role-specific system prompt and parses a strict JSON response; a malformed response raises rather than being silently coerced. |
| `schemas.py` | Structural validation (required fields + types + enums) for every data shape passed between stages: `TestCase`, `ExecutionResult`, `JudgeVerdict`, `HallucinationClaim`. No external dependency -- a small built-in validator, since these shapes are simple. |
| `test_designer.py` | `generate_test_cases(story_text)` calls the model, validates each case against `TEST_CASE_SCHEMA`, and `write_test_cases_csv()` persists them. For every acceptance criterion in the story, the model is prompted to produce a `happy_path`/`negative` "bulk pair" tagged with that criterion's `ac_id`, plus any `edge_case`/`adversarial` cases needed. `check_ac_coverage()` is a soft, non-blocking check that warns (doesn't fail) if an `ac_id` is missing one half of its bulk pair. |
| `executor.py` | `run_bulk(cases, run_id, concurrency, repetitions)` runs a thread pool over independent Agent API sessions (`run_test_case()` per case). Only `happy_path`/`negative` cases (`schemas.BULK_CATEGORIES`) are repeated `repetitions` times -- that's the per-acceptance-criterion bulk pair being checked for consistency; `edge_case`/`adversarial` cases always run once, since they're one-shot boundary/hallucination-bait checks, not part of the bulk measurement. Any Agent API failure is caught per-test and recorded as `execution_status=ERROR` rather than raised, so one bad session doesn't kill the whole batch. Every test's full transcript (test case + execution result, including every action/tool the agent invoked) is written to its own JSON file -- that's the evidence the judge's hallucination audit cross-checks against. |
| `judge.py` | `grade_all()` calls the model once per test with `{expected_topic, expected_action, expected_outcome, actual_topic, actual_action, final_response, actions_taken}` and gets back `{verdict, topic_match, action_match, rationale, hallucinations}` -- grading and hallucination auditing in the same call (see "Why grading and hallucination auditing are one call" below). Tests that errored at execution are recorded as `EXCLUDED`, not graded or audited -- an infra failure isn't the agent's fault and shouldn't count against its pass rate. |
| `hallucination.py` | No model call. `write_claims()` takes the hallucination claims `judge.grade_all()` already extracted from its single model response and writes them out: unsupported claims to the flagged-claims file, and a per-test summary (claims checked vs. unsupported) separately so a clean transcript still shows up in the report. |
| `regression.py` | Pure Python, no LLM call. Appends every run's verdicts to `results/run_history.csv` (one row per test per run, forever), finds the most recent prior run of the *same* `test_cases.csv`, and diffs verdicts: `newly_failing`, `newly_passing`, `unchanged`, `new_test`. |
| `report.py` | Pure Python. Reads all of a run's result CSVs and applies four gates (see below), then renders a terminal summary, `report_<run_id>.md`, `report_<run_id>.csv`, `ac_summary_<run_id>.csv` (pass/fail % and hallucination count per acceptance criterion, bulk pairs only), and a `hallucination_worksheet_<run_id>.csv` sample for a human to spot-check in Salesforce by hand. Its gate functions (`gate0_execution_integrity`, `gate1_pass_rate`, `gate2_hallucination_scan`, `gate_regression`) are the same functions both `run.py report` and the Streamlit report tab call -- the UI adds no separate aggregation logic. |
| `run.py` | CLI: `design`, `execute`, `report` subcommands. `execute` runs Executor -> Judge (grading + hallucination audit) -> Regression in sequence and prints the `run_id` to feed into `report`. |
| `app.py` | Streamlit UI with the same three stages as tabs. Every button calls the same functions `run.py` calls -- see the module docstring in `app.py`. |

## Data model: CSV + per-test JSON, not a database

Test cases and run history are CSV by design (human-editable, diffable in
git, no database to stand up). Raw transcripts and action traces are
nested/variable-shaped, so each test's full transcript lives in its own
JSON file under `transcripts/<run_id>/`, referenced by path from the CSV
rows that summarize it.

## The four gates (`report.py`)

1. **Gate 0 -- Execution integrity.** Any test whose Agent API call itself
   errored or timed out is pulled out of the pass/fail denominator and
   reported separately. This is an infrastructure problem, not an agent
   correctness problem, and mixing the two would make the pass rate
   meaningless.
2. **Gate 1 -- Pass rate.** `judge verdict == PASS` / total graded tests, as
   a %, broken down by `category` (happy_path / negative / edge_case /
   adversarial), by `expected_topic`, and by `ac_id` (acceptance
   criterion) -- the `ac_id` breakdown only counts `happy_path`/`negative`
   executions, i.e. the bulk pair repeated per criterion, and is what
   answers "for this acceptance criterion, what % passed across the bulk
   run" directly.
3. **Gate 2 -- Hallucination scan.** Every claim the Hallucination Detector
   marked `unsupported`, grouped by severity. Adversarial-category tests
   additionally get their own *hallucination-induced rate* -- the fraction
   of adversarial prompts that produced at least one fabricated claim --
   tracked separately from the overall pass rate, since a test can pass
   topic/action matching while still hallucinating in its response text.
4. **Regression.** Tests that passed in the immediately-prior run of the
   same `test_cases.csv` but fail now. This is the single most actionable
   number after an AgentScript deploy: it isolates *new* breakage from
   pre-existing, already-known failures.

## Why grading and hallucination auditing are one call

A response can be topically correct and still fabricate a detail (see the
demo's TC-007: correct topic, but an invented discount code), or it can be
fully grounded and still fail to satisfy the expected outcome (a correct,
truthful, but unhelpful answer). These used to be two separate Claude
calls (Judge, then a dedicated Hallucination Detector) so the report could
show "75% pass, but 2 of those failures were hallucinations" as two
distinct numbers instead of one undifferentiated one -- but a second
full model call per execution roughly doubles grading cost for no gain:
both calls need the same evidence (the transcript's `actions_taken`) and
operate at the same per-execution granularity, so `judge.py`'s single
call now asks the model for both the verdict *and* the list of factual
claims with their support status in one JSON response
(`model_client.JUDGE_SYSTEM_PROMPT`). The report still shows both numbers
separately -- `judge.grade()` splits the response into the verdict row
(`judged_<run_id>.csv`) and the hallucination claims
(`hallucinations_<run_id>.csv` / `hallucination_summary_<run_id>.csv`,
written by `hallucination.write_claims()`) -- only the number of model
round-trips changed, not what the report distinguishes.

## Concurrency

`executor.run_bulk()` uses a `ThreadPoolExecutor` (default concurrency 4,
configurable) because each test case is an independent Agent API session --
there's no shared state to race on, and the bottleneck is network latency
per session, not CPU.

## Scope cut: Agent API only, not Testing Center metadata

Salesforce's own Agentforce Testing Center/Testing API can also grade
topic/action match authoritatively, via `AiEvaluationDefinition` metadata
deployed through Agentforce DX. That's a heavier, deploy-pipeline-shaped
integration that doesn't fit a terminal/UI-driven workflow run by hand. This
project grades everything directly from the Agent API transcript instead
(the Judge does topic/outcome grading from the conversation itself). Wiring
in Testing Center metadata is a clean, additive follow-up if this ever
needs to gate a CI pipeline instead of being run on demand.

## `results/` and `transcripts/` are gitignored

Both directories are created on demand (`os.makedirs(..., exist_ok=True)`
in `executor.py`/`judge.py`/`hallucination.py`/`report.py`) and hold only
generated run output -- CSVs, transcripts, and reports from `test_cases.csv`
runs against your own Agent API agent. They're gitignored rather than
shipped, since a real transcript can carry customer-adjacent data that
shouldn't end up in version control. A fresh clone has no report until you
run the pipeline once (see `USER_GUIDE.md`).
