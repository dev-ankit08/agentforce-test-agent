# Agentforce Bulk & Regression Test Agent

Turns a user story into bulk test cases, runs them against a live
Agentforce (AgentScript) agent, grades pass/fail, flags hallucinated
responses, and catches regressions between runs -- via a Streamlit UI or
the CLI.

- **New here?** Start with `USER_GUIDE.md` (setup + how to use the app).
- **Want to know how it's built?** See `IMPLEMENTATION.md`.

Quick start (UI):

```
pip install -e .              # or: pip install -r requirements.txt
copy .env.example .env        # fill in ANTHROPIC_API_KEY, org alias, agent api name
launch_app.bat                # or: streamlit run app.py
```

Quick start (CLI, e.g. for scheduled runs):

```
pip install -e .
agentforce-test doctor                                   # preflight checks
agentforce-test run --cases example_cases.csv --fail-on regression
```

## Installing

`pip install -e .` installs the console entry point **`agentforce-test`**
(equivalent to `python run.py`) plus all runtime deps from `pyproject.toml`.
For a byte-for-byte reproducible environment (scheduled jobs, shared CI
runners), install the pinned set instead:

```
pip install -r requirements.lock
```

Dev extras (tests + linter): `pip install -e ".[dev]"`, then `pytest -q`
and `ruff check .`. The test suite is fully offline -- no org or API key
needed.

## CLI

```
agentforce-test design  --story story.txt --out test_cases.csv
agentforce-test execute --cases test_cases.csv [--concurrency 4] [--repetitions N]
                        [--run-id ID] [--fail-on {none,regression,failure}]
                        [--fail-under PCT] [--json]
agentforce-test run     --cases test_cases.csv [--story story.txt] [...]   # design?+execute+report
agentforce-test report  --run <run_id> [--cases test_cases.csv] [--json]
agentforce-test doctor                                                     # preflight checks
agentforce-test --version
```

- **`run`** is the one-shot scheduler entry point: optional design (if
  `--story` is given), then execute, then report -- in a single command.
- **`doctor`** validates `ANTHROPIC_API_KEY`, the org alias / agent API name,
  and `sf` org reachability, and exits non-zero on any problem so a scheduled
  job fails fast and loud.

### Exit codes (for scheduled runs / CI gating)

`execute` and `run` exit non-zero per a policy you choose, so cron/launchd
or a CI step can alert on the result:

- `--fail-on none` -- always exit 0 (report only).
- `--fail-on regression` *(default)* -- exit non-zero if any test newly
  regressed vs. the previous run of the same cases file.
- `--fail-on failure` -- exit non-zero on any regression **or** any failed /
  infra-errored execution.
- `--fail-under PCT` -- exit non-zero if the overall pass rate drops below
  `PCT` (combine with any `--fail-on`).
- `--json` -- print a machine-readable summary (counts, pass %, regressions,
  infra errors, exit reason) instead of the text report, for log scraping.

Every run also writes `results/run_meta_<run_id>.json` (tool version, git
SHA, planned vs. actual executions, per-stage timings, model ids, summary)
for auditability.

### Scheduled-run recipe

cron (Linux/macOS) -- nightly at 02:00, alert on non-zero exit:

```
0 2 * * *  cd /path/to/agentforce-test-agent && \
  agentforce-test run --cases example_cases.csv --fail-on regression --json \
  >> logs/nightly.jsonl 2>&1 || /usr/local/bin/alert "agentforce regression"
```

launchd (macOS) -- a `~/Library/LaunchAgents/com.example.agentforce-test.plist`
with `ProgramArguments` of `["/path/to/venv/bin/agentforce-test", "run",
"--cases", "example_cases.csv", "--fail-on", "regression", "--json"]` and a
`StartCalendarInterval` block. Set `EnvironmentVariables` (or an `.env` in
`WorkingDirectory`) for `ANTHROPIC_API_KEY`, `AGENTFORCE_SF_ORG_ALIAS`, and
`AGENTFORCE_AGENT_API_NAME`. Run `agentforce-test doctor` from the same
environment first to confirm config.

## Configuration

Env vars (or `.env`): `ANTHROPIC_API_KEY`, `AGENTFORCE_SF_ORG_ALIAS`,
`AGENTFORCE_AGENT_API_NAME`. Optional knobs (see `config.py`): `LOG_LEVEL`,
`LOG_FORMAT` (`text`|`json`), retry attempts/delays, and
`AGENTFORCE_ALIASES_FILE`.

### Test-case fields

Required CSV columns: `test_id`, `utterance`, `expected_topic`,
`expected_action`, `expected_outcome`, `category`. Optional:

- `ac_id` -- acceptance-criterion tag (e.g. `AC1`) for per-AC reporting.
- `conversation_mode` -- `single_turn` (default) or `multi_turn`.
- `scenario` -- for `multi_turn`: the persona/goal an LLM "customer" plays.
- `expected_actions` -- `->`-separated actions that must all be present.
- `expected_action_path` -- an ordered action sequence; `...` matches any
  gap (deterministic trace assertion). Fails the case before any model
  call if unmet.

See `example_cases.csv` for one case of each shape.

### aliases.json (optional)

Treat different topic/action API names as equivalent when matching. Place an
`aliases.json` (or point `AGENTFORCE_ALIASES_FILE` at one) with:

```json
{
  "topic_aliases":  [["Order_Status", "Order_Tracking"]],
  "action_aliases": [["Get_Order_Status", "Lookup_Order"]]
}
```

Names are normalized (id suffixes stripped, underscores -> spaces,
lowercased) before comparison; aliases cover genuine renames beyond that.
