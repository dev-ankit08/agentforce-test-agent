"""
test_designer.py -- turns a plain-text user story into test_cases.csv via
Claude. See model_client.call_test_designer for the prompt.
"""
from __future__ import annotations

import csv
from pathlib import Path

import model_client
from schemas import TEST_CASE_SCHEMA, validate

FIELDNAMES = ["test_id", "utterance", "expected_topic", "expected_action", "expected_outcome", "category", "ac_id"]


def generate_test_cases(story_text: str) -> list[dict]:
    cases = model_client.call_test_designer(story_text)
    for case in cases:
        validate(case, TEST_CASE_SCHEMA)
    return cases


def check_ac_coverage(cases: list[dict]) -> list[str]:
    """Soft, non-blocking check: every acceptance criterion should have both
    a happy_path and a negative case (the bulk pair). Returns human-readable
    warnings for any ac_id missing one; never raises -- a story can
    legitimately have an AC where one side doesn't apply."""
    by_ac: dict[str, set] = {}
    for case in cases:
        by_ac.setdefault(case.get("ac_id", "general"), set()).add(case.get("category"))

    warnings = []
    for ac_id, categories in sorted(by_ac.items()):
        if ac_id == "general":
            continue
        if "happy_path" not in categories:
            warnings.append(f"{ac_id} is missing a happy_path case for bulk testing.")
        if "negative" not in categories:
            warnings.append(f"{ac_id} is missing a negative-scenario case for bulk testing.")
    return warnings


def write_test_cases_csv(cases: list[dict], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for case in cases:
            writer.writerow({k: case[k] for k in FIELDNAMES})


def design(story_path: str, out_path: str) -> list[dict]:
    story_text = Path(story_path).read_text(encoding="utf-8")
    cases = generate_test_cases(story_text)
    write_test_cases_csv(cases, out_path)
    for warning in check_ac_coverage(cases):
        print(f"Warning: {warning}")
    return cases
