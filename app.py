"""
app.py -- Streamlit UI for the Agentforce bulk/regression test agent.

Launch with:  streamlit run app.py   (or double-click launch_app.bat)

This is a thin UI layer only -- every button here calls the same functions
`run.py` calls on the command line (test_designer, executor, judge,
hallucination, regression, report). There is no separate "UI logic"; the
UI just gives a non-terminal user a form instead of flags.
"""
from __future__ import annotations

import glob
import os
import time

import pandas as pd
import streamlit as st

import config
import executor
import hallucination
import judge
import regression
import report
import test_designer

st.set_page_config(page_title="Agentforce Test Agent", layout="wide")

SEVERITY_COLOR = {"critical": "#b91c1c", "medium": "#b45309", "low": "#a16207", "none": "#6b7280"}


# --- helpers -----------------------------------------------------------------

def _new_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _available_test_case_files() -> list[str]:
    files = [f for f in glob.glob("*.csv") if not f.startswith("results")]
    return sorted(files) or ["test_cases.csv"]


def _available_run_ids() -> list[str]:
    paths = glob.glob(os.path.join(config.RESULTS_DIR, "run_*.csv"))
    ids = []
    for p in paths:
        name = os.path.basename(p)
        if name == "run_history.csv":
            continue
        ids.append(name[len("run_"):-len(".csv")])
    return sorted(ids, reverse=True)


def _config_status() -> dict:
    return {
        "ANTHROPIC_API_KEY": bool(config.ANTHROPIC_API_KEY),
        "AGENTFORCE_SF_ORG_ALIAS": config.SF_ORG_ALIAS,
        "AGENTFORCE_AGENT_API_NAME": config.AGENT_API_NAME,
    }


# --- sidebar: config status ---------------------------------------------------

with st.sidebar:
    st.header("Configuration")
    status = _config_status()
    st.write("Anthropic API key:", "configured" if status["ANTHROPIC_API_KEY"] else ":red[missing]")
    st.write("Salesforce org alias:", status["AGENTFORCE_SF_ORG_ALIAS"] or ":red[missing]")
    st.write("Agent API name:", status["AGENTFORCE_AGENT_API_NAME"] or ":red[missing]")
    if not all([status["ANTHROPIC_API_KEY"], status["AGENTFORCE_SF_ORG_ALIAS"], status["AGENTFORCE_AGENT_API_NAME"]]):
        st.caption("Set these in `.env` (copy from `.env.example`), then restart the app. "
                   "You can still browse any existing report below.")
    st.divider()
    st.caption("See USER_GUIDE.md for a full walkthrough and IMPLEMENTATION.md for how this is built.")

st.title("Agentforce Bulk & Regression Test Agent")
st.caption("Turn a user story into test cases, run them against your live Agentforce agent, "
           "and get a pass/fail + hallucination + regression report.")

tab_design, tab_execute, tab_report = st.tabs(["1. Design test cases", "2. Run tests", "3. Report"])


# --- Tab 1: Design ------------------------------------------------------------

with tab_design:
    st.subheader("Generate test cases from a user story")
    story_text = st.text_area(
        "User story / acceptance criteria",
        value=open("story.txt", encoding="utf-8").read() if os.path.exists("story.txt") else "",
        height=260,
    )
    out_name = st.text_input("Save test cases as", value="test_cases.csv")

    if st.button("Generate test cases with Claude", type="primary"):
        if not config.ANTHROPIC_API_KEY:
            st.error("ANTHROPIC_API_KEY is not set in .env -- cannot call Claude. See USER_GUIDE.md.")
        elif not story_text.strip():
            st.warning("Paste a user story first.")
        else:
            with st.spinner("Designing test cases..."):
                try:
                    cases = test_designer.generate_test_cases(story_text)
                    st.session_state["designed_cases"] = cases
                    st.session_state["designed_out_name"] = out_name
                except Exception as exc:  # surfaced to the user, not swallowed
                    st.error(f"Test design failed: {exc}")

    if "designed_cases" in st.session_state:
        df = pd.DataFrame(st.session_state["designed_cases"])
        st.write(f"{len(df)} test case(s) generated:")
        st.dataframe(df, width='stretch')
        for warning in test_designer.check_ac_coverage(st.session_state["designed_cases"]):
            st.warning(warning)
        if st.button(f"Save to {st.session_state['designed_out_name']}"):
            test_designer.write_test_cases_csv(st.session_state["designed_cases"], st.session_state["designed_out_name"])
            st.success(f"Saved to {st.session_state['designed_out_name']}")

    if os.path.exists("test_cases.csv"):
        with st.expander("Current test_cases.csv on disk"):
            st.dataframe(pd.read_csv("test_cases.csv"), width='stretch')


# --- Tab 2: Execute ------------------------------------------------------------

with tab_execute:
    st.subheader("Run the bulk test against the live agent")
    cases_file = st.selectbox("Test cases file", _available_test_case_files())
    if cases_file and os.path.exists(cases_file):
        cases_preview = pd.read_csv(cases_file).to_dict("records")
        st.dataframe(pd.DataFrame(cases_preview), width='stretch')
        n_cases = len(cases_preview)
    else:
        cases_preview = []
        n_cases = 0
    concurrency = st.slider("Concurrency (parallel Agent API sessions)", 1, 8, config.DEFAULT_CONCURRENCY)
    repetitions = st.number_input(
        "Repetitions for bulk (happy_path / negative) test cases",
        min_value=1, max_value=50, value=3, step=1,
        help="Runs each happy_path/negative test case -- the bulk pair per acceptance criterion -- this many "
             "times, since the agent is LLM-backed and can answer the same prompt differently from run to run. "
             "edge_case/adversarial test cases always run once. The report's Consistency section flags any "
             "bulk test case that doesn't pass every repetition as flaky.",
    )
    total_executions = executor.planned_execution_count(cases_preview, int(repetitions))
    st.caption(f"This will run {n_cases} test case(s) -> **{total_executions} total executions** "
               f"({repetitions}x for happy_path/negative, 1x for edge_case/adversarial).")

    if st.button("Run bulk test", type="primary"):
        missing = [k for k, v in _config_status().items() if not v]
        if missing:
            st.error(f"Missing configuration: {', '.join(missing)}. Set these in .env and restart.")
        else:
            run_id = _new_run_id()
            progress = st.empty()
            try:
                with st.spinner(f"Running {cases_file} against the live agent (run_id={run_id})..."):
                    cases = pd.read_csv(cases_file).to_dict("records")
                    cases_by_id = {c["test_id"]: c for c in cases}
                    progress.info(f"Calling Agent API for {n_cases} test case(s) ({total_executions} total executions)...")
                    execution_results = executor.run_bulk(cases, run_id, concurrency=concurrency, repetitions=int(repetitions))
                    progress.info("Grading responses and auditing for hallucinations...")
                    verdicts, hallucination_claims = judge.grade_all(cases_by_id, execution_results, run_id)
                    hallucination.write_claims(hallucination_claims, execution_results, run_id)
                    progress.info("Diffing against the previous run (aggregated per test case across repetitions)...")
                    aggregated = report.aggregate_verdicts_by_test(verdicts)
                    regression.append_to_history(run_id, cases_file, aggregated, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                    regression.diff_against_previous(cases_file, run_id, aggregated)
                st.success(f"Run complete: run_id = {run_id}")
                st.session_state["last_run_id"] = run_id
                st.session_state["last_cases_file"] = cases_file
                st.info("Open the **3. Report** tab to view results.")
            except Exception as exc:
                st.error(f"Run failed: {exc}")


# --- Tab 3: Report ---------------------------------------------------------------

with tab_report:
    st.subheader("Test report")
    run_ids = _available_run_ids()
    if not run_ids:
        st.info("No runs found yet. Use the **2. Run tests** tab first.")
    else:
        default_idx = run_ids.index(st.session_state["last_run_id"]) if st.session_state.get("last_run_id") in run_ids else 0
        run_id = st.selectbox("Run", run_ids, index=default_idx)
        cases_file = st.text_input("Test cases file used for this run", value=st.session_state.get("last_cases_file", "test_cases.csv"))

        data = report.load_run(run_id, cases_file)
        g0 = report.gate0_execution_integrity(data)
        g1 = report.gate1_pass_rate(data)
        gc = report.gate_consistency(data)
        g2 = report.gate2_hallucination_scan(data)
        greg = report.gate_regression(data)

        st.caption(
            f"{len(gc['aggregated'])} test case(s), {g0['total']} total executions "
            f"(each test case run {g0['total'] // len(gc['aggregated']) if gc['aggregated'] else 0}x on average)."
        )
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        col1.metric("Pass rate", f"{g1['pass_pct']:.1f}%", f"{g1['pass_count']}/{g1['valid_count']} executions")
        col2.metric("Fail rate", f"{g1['fail_pct']:.1f}%", f"{g1['fail_count']}/{g1['valid_count']} executions", delta_color="inverse")
        col3.metric("Flaky test cases", len(gc["flaky"]), f"of {len(gc['aggregated'])} test cases", delta_color="inverse")
        col4.metric("Hallucinations flagged", len(g2["flagged"]), f"{g2['hallucination_induced_rate']:.0f}% of adversarial executions")
        col5.metric("Regressions", len(greg["newly_failing"]), "newly failing vs previous run", delta_color="inverse")
        col6.metric("Infra failures", len(g0["infra_failures"]), f"of {g0['total']} executions")

        def _pass_fail_df(buckets: dict) -> pd.DataFrame:
            rows = []
            for key, b in buckets.items():
                total = b["total"] or 1
                rows.append({"name": key, "pass_pct": b["pass"] / total * 100, "fail_pct": b["fail"] / total * 100})
            return pd.DataFrame(rows).set_index("name")

        chart_col1, chart_col2, chart_col3 = st.columns(3)
        with chart_col1:
            st.write("Pass % / fail % by category")
            st.bar_chart(_pass_fail_df(g1["by_category"]))
        with chart_col2:
            st.write("Pass % / fail % by expected topic")
            st.bar_chart(_pass_fail_df(g1["by_topic"]))
        with chart_col3:
            st.write("Pass % / fail % by acceptance criteria (bulk)")
            st.bar_chart(_pass_fail_df(g1["by_ac"]))

        with st.expander("Per-acceptance-criteria bulk summary", expanded=True):
            if g1["by_ac"]:
                ac_rows = []
                for ac_id, b in sorted(g1["by_ac"].items()):
                    total = b["total"] or 1
                    ac_rows.append({
                        "ac_id": ac_id,
                        "pass_pct": round(b["pass"] / total * 100, 1),
                        "fail_pct": round(b["fail"] / total * 100, 1),
                        "pass_count": b["pass"],
                        "total": b["total"],
                        "hallucinations_flagged": g2["by_ac"].get(ac_id, 0),
                    })
                st.dataframe(pd.DataFrame(ac_rows), width='stretch')
                st.caption("happy_path + negative executions only -- the bulk pair per acceptance criterion. "
                           "edge_case/adversarial cases run once and show up in the category breakdown instead.")
            else:
                st.write("No bulk (happy_path/negative) executions yet -- generate test cases tagged with ac_id first.")

        with st.expander(f"Failed executions ({len(g1['failed_tests'])})", expanded=bool(g1["failed_tests"])):
            if g1["failed_tests"]:
                st.dataframe(pd.DataFrame(g1["failed_tests"]), width='stretch')
            else:
                st.write("None.")

        with st.expander(f"Consistency across repetitions ({len(gc['flaky'])} flaky test case(s))", expanded=bool(gc["flaky"])):
            if gc["aggregated"]:
                cdf = pd.DataFrame(gc["aggregated"])[["test_id", "verdict", "pass_count", "graded_count", "consistency_pct", "repetitions"]]
                st.dataframe(cdf, width='stretch')
                st.caption("A test case is flaky when it doesn't pass every repetition -- same prompt, inconsistent agent behavior.")
            else:
                st.write("No graded executions yet.")

        with st.expander(f"Hallucination flags ({len(g2['flagged'])})", expanded=bool(g2["flagged"])):
            if g2["flagged"]:
                hdf = pd.DataFrame(g2["flagged"])[["test_id", "repetition", "severity", "claim", "detail"]]
                st.dataframe(hdf, width='stretch')
            else:
                st.write("None -- every claim checked resolved to a real action output.")

        with st.expander(f"Regression ({len(greg['newly_failing'])} newly failing, {len(greg['newly_passing'])} newly passing)", expanded=bool(greg["newly_failing"])):
            changed = [r for r in greg["all"] if r["delta_type"] != "unchanged"]
            if changed:
                st.dataframe(pd.DataFrame(changed), width='stretch')
            else:
                st.write("No change from the previous run.")

        with st.expander(f"Execution integrity ({len(g0['infra_failures'])} infra failure(s))"):
            if g0["infra_failures"]:
                st.dataframe(pd.DataFrame(g0["infra_failures"]), width='stretch')
            else:
                st.write("Every test executed without an Agent API error.")

        st.divider()
        report.generate(run_id, cases_file)  # (re)writes report_<run_id>.md/.csv + ac_summary_<run_id>.csv + hallucination_worksheet_<run_id>.csv
        dl_col1, dl_col2, dl_col3, dl_col4 = st.columns(4)
        report_md = os.path.join(config.RESULTS_DIR, f"report_{run_id}.md")
        report_csv = os.path.join(config.RESULTS_DIR, f"report_{run_id}.csv")
        ac_summary_csv = os.path.join(config.RESULTS_DIR, f"ac_summary_{run_id}.csv")
        worksheet_csv = os.path.join(config.RESULTS_DIR, f"hallucination_worksheet_{run_id}.csv")
        if os.path.exists(report_md):
            dl_col1.download_button("Download report (.md)", open(report_md, "rb").read(), file_name=os.path.basename(report_md))
        if os.path.exists(report_csv):
            dl_col2.download_button("Download report (.csv)", open(report_csv, "rb").read(), file_name=os.path.basename(report_csv))
        if os.path.exists(ac_summary_csv):
            dl_col3.download_button("Download AC summary (.csv)", open(ac_summary_csv, "rb").read(), file_name=os.path.basename(ac_summary_csv))
        if os.path.exists(worksheet_csv):
            dl_col4.download_button("Download hallucination worksheet (.csv)", open(worksheet_csv, "rb").read(), file_name=os.path.basename(worksheet_csv))
