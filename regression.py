"""
regression.py -- pure Python, no LLM. Maintains results/run_history.csv (one
row per test per run, ever) and diffs the current run's verdicts against the
most recent prior run of the same test_cases.csv file.
"""
from __future__ import annotations

import csv
import os

import config

HISTORY_FIELDS = ["run_id", "timestamp", "cases_file", "test_id", "verdict"]
REGRESSION_FIELDS = ["test_id", "previous_run_id", "previous_verdict", "current_verdict", "delta_type"]


def _read_history() -> list[dict]:
    if not os.path.exists(config.RUN_HISTORY_CSV):
        return []
    with open(config.RUN_HISTORY_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_to_history(run_id: str, cases_file: str, verdicts: list[dict], timestamp: str) -> None:
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    file_exists = os.path.exists(config.RUN_HISTORY_CSV)
    with open(config.RUN_HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        for v in verdicts:
            writer.writerow({
                "run_id": run_id,
                "timestamp": timestamp,
                "cases_file": cases_file,
                "test_id": v["test_id"],
                "verdict": v["verdict"],
            })


def find_previous_run_id(cases_file: str, current_run_id: str) -> str | None:
    history = _read_history()
    prior_runs = sorted(
        {row["run_id"] for row in history if row["cases_file"] == cases_file and row["run_id"] != current_run_id}
    )
    return prior_runs[-1] if prior_runs else None


def diff_against_previous(cases_file: str, run_id: str, verdicts: list[dict]) -> list[dict]:
    previous_run_id = find_previous_run_id(cases_file, run_id)
    diffs: list[dict] = []

    if previous_run_id is None:
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        path = os.path.join(config.RESULTS_DIR, f"regression_{run_id}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=REGRESSION_FIELDS)
            writer.writeheader()
        return diffs

    history = _read_history()
    previous_verdicts = {row["test_id"]: row["verdict"] for row in history if row["run_id"] == previous_run_id}

    for v in verdicts:
        prev = previous_verdicts.get(v["test_id"])
        current = v["verdict"]
        if prev is None:
            delta_type = "new_test"
        elif prev == "PASS" and current == "FAIL":
            delta_type = "newly_failing"
        elif prev == "FAIL" and current == "PASS":
            delta_type = "newly_passing"
        else:
            delta_type = "unchanged"
        diffs.append({
            "test_id": v["test_id"],
            "previous_run_id": previous_run_id,
            "previous_verdict": prev or "",
            "current_verdict": current,
            "delta_type": delta_type,
        })

    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    path = os.path.join(config.RESULTS_DIR, f"regression_{run_id}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REGRESSION_FIELDS)
        writer.writeheader()
        writer.writerows(diffs)
    return diffs
