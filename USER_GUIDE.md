# User guide

How to set up and use the Agentforce Bulk & Regression Test Agent. For how
it's built internally, see `IMPLEMENTATION.md`.

## 1. One-time setup

You need three things before you can run a real test:

1. **Python 3.10+** installed.
2. **The Salesforce CLI (`sf`)**, already logged in to the org where your
   AgentScript agent is deployed:
   ```
   sf org login web --alias myOrgAlias
   sf org list          # confirm myOrgAlias shows "Connected"
   ```
3. **An Anthropic API key.**

Then, in this folder:

```
pip install -e .          # or: pip install -r requirements.txt
copy .env.example .env
```

(`pip install -e .` also installs the `agentforce-test` command used in
section 5. For a locked, reproducible environment, use
`pip install -r requirements.lock`.)

Open `.env` in a text editor and fill in:

```
ANTHROPIC_API_KEY=sk-ant-...
AGENTFORCE_SF_ORG_ALIAS=myOrgAlias
AGENTFORCE_AGENT_API_NAME=<your agent's Agent API name>
```

Find `AGENTFORCE_AGENT_API_NAME` in Salesforce Setup under the deployed
agent's details page (its API name / developer name).

## 2. Launch the app

Double-click **`launch_app.bat`** in this folder. A terminal window opens,
and after a few seconds your browser opens to the app (usually
`http://localhost:8501`). Leave that terminal window open while you use the
app; closing it stops the app.

(If you'd rather use a terminal yourself: `streamlit run app.py`.)

The **sidebar** always shows whether your `.env` is fully configured. If
something's missing, you can still browse any existing report -- you just
can't generate new test cases or run a new test until it's filled in.

## 3. The three tabs

### Tab 1 -- Design test cases

Paste (or edit) a user story with numbered acceptance criteria, then click
**Generate test cases with Claude**. For every acceptance criterion, Claude
proposes a "bulk pair": one **happy_path** case (the criterion's condition
is met) and one **negative** case (the condition is *not* met, so the
described behavior must not happen) -- these two are what get repeated in
Tab 2's bulk run. It also adds **edge_case** (boundary/missing-data) and
**adversarial** (deliberately tricky, meant to tempt the agent into making
something up) cases, which only ever run once. If an acceptance criterion
ends up missing one half of its bulk pair, you'll see a warning under the
table -- worth fixing before you run a bulk pass against it.

Review the table -- edit it directly if a case looks wrong -- then click
**Save to test_cases.csv** (or whatever filename you chose). You can also
open `test_cases.csv` in Excel and edit it by hand; the app just reads
whatever's on disk.

### Tab 2 -- Run tests

Pick a test-cases file, review the preview table, choose how many tests to
run in parallel (4 is a reasonable default), and set **Repetitions for
bulk (happy_path / negative) test cases** -- this only affects the bulk
pair per acceptance criterion; edge_case/adversarial cases always run
once, so you don't pay for repeating a one-shot check. Click **Run bulk
test**. This actually talks to your live agent -- one Agent API
conversation per execution -- then grades every response and checks every
factual claim it made against what it actually looked up, in the same
pass (one round of grading, not two). This takes longer than Tab 1 (real
conversations + grading), so expect it to run for a while on a large test
set.

When it finishes you'll see a `run_id` -- note it, or just go straight to
the Report tab, which remembers your most recent run.

### Tab 3 -- Report

Pick a run (or use the one just completed) and you'll see:

- **Four headline numbers**: pass rate, how many hallucinations were
  flagged, how many tests newly regressed since the last run, and how many
  tests failed to execute at all (infrastructure problems, not agent
  problems).
- **Three bar charts**: pass rate broken down by test category, by topic,
  and by acceptance criteria -- useful for spotting "this one topic (or
  this one acceptance criterion) is where it keeps failing."
- **Per-acceptance-criteria bulk summary**: a table with one row per
  acceptance criterion -- pass %, fail %, and how many hallucinations were
  flagged in its bulk (happy_path/negative) executions. This is the
  number to report per AC after a bulk run.
- **Failed tests**: what was expected vs. what actually happened, in plain
  language.
- **Hallucination flags**: every claim the agent made that wasn't backed
  up by anything it actually looked up, with a severity rating -- flagged
  as part of the same grading pass, not a separate scan.
- **Regression**: tests that used to pass and now fail (or vice versa) --
  this is the number to watch right after deploying an AgentScript change.
- **Execution integrity**: any test that failed to even run (timeout,
  API error), separate from the agent's own accuracy.

Download buttons at the bottom let you save the report as Markdown or CSV,
the per-acceptance-criteria summary as CSV, and a hallucination worksheet
CSV for someone to manually verify a sample of the flagged claims directly
in Salesforce.

## 4. The regression workflow

After every AgentScript deploy:

1. Open Tab 2, pick the same `test_cases.csv` you used last time, click
   **Run bulk test**.
2. Open Tab 3. The Regression section automatically compares this run to
   the most recent previous run of that same file -- no extra setup needed.

## 5. If you'd rather use the terminal

Everything the app does is also a CLI command. After `pip install -e .` you
can use the `agentforce-test` command (or `python run.py` with the same
arguments):

```
agentforce-test design  --story story.txt --out test_cases.csv
agentforce-test execute --cases test_cases.csv
agentforce-test report  --run <run_id>
agentforce-test run     --cases test_cases.csv     # one-shot: execute + report
agentforce-test doctor                             # check your setup is valid
```

Run **`agentforce-test doctor`** any time to confirm your API key, org
alias, agent API name, and `sf` org connection are all good -- handy before
a scheduled run.

### Unattended / scheduled runs

For a nightly job (or any script that needs to know pass/fail), `execute`
and `run` can **exit non-zero** so the scheduler can alert you:

- `--fail-on regression` (the default) -- fail the run if any test that used
  to pass now fails, compared to the previous run of the same file.
- `--fail-on failure` -- also fail on any failed or infra-errored execution.
- `--fail-under 90` -- fail if the overall pass rate drops below 90%.
- `--json` -- print a compact machine-readable summary instead of the full
  report (easy to grep in logs).

Example nightly cron line:

```
0 2 * * *  cd /path/to/agentforce-test-agent && \
  agentforce-test run --cases example_cases.csv --fail-on regression --json \
  >> logs/nightly.jsonl 2>&1 || notify-me "agentforce regression"
```

Each run also drops a `results/run_meta_<run_id>.json` recording the tool
version, git commit, timings, and a result summary -- useful when reviewing
what a scheduled run did days later. See `README.md` for a launchd (macOS)
variant and the full flag list.

### Advanced test-case fields (optional)

Beyond the basic columns, a test case can carry:

- `conversation_mode = multi_turn` + a `scenario` -- Claude plays a customer
  with that persona/goal and holds a short back-and-forth with the agent,
  instead of a single message.
- `expected_actions` -- a `->`-separated list of actions that must all have
  been invoked.
- `expected_action_path` -- an ordered sequence the agent's actions must
  match, where `...` means "any actions may come in between". If the path
  doesn't match, the case fails immediately without spending a grading call.

`example_cases.csv` shows one of each. You can also keep an optional
`aliases.json` to treat renamed topics/actions as equivalent when matching
(see `README.md`).

## 6. There's no data here until you run something

`results/` and `transcripts/` are gitignored and don't ship with the repo
-- a fresh clone's Report tab will say "No runs found yet" until you run
Tab 2 (or `run.py execute`) at least once. Everything you run locally
stays local; it's never committed.
