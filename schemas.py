"""
schemas.py -- the contracts every stage reads or writes. A small built-in
validator is used instead of requiring the `jsonschema` package, since these
shapes are simple and this project has few dependencies by design.
"""
from __future__ import annotations

TEST_CASE_SCHEMA = {
    "type": "object",
    "required": ["test_id", "utterance", "expected_topic", "expected_action", "expected_outcome", "category", "ac_id"],
    "properties": {
        "test_id": {"type": "string"},
        "utterance": {"type": "string"},
        "expected_topic": {"type": "string"},
        "expected_action": {"type": "string"},
        "expected_outcome": {"type": "string"},
        "category": {"type": "string", "enum": ["happy_path", "negative", "edge_case", "adversarial"]},
        "ac_id": {"type": "string"},
    },
}

# happy_path/negative form the "bulk pair" for each acceptance criterion --
# these are the only categories repeated `repetitions` times by
# executor.run_bulk(); edge_case/adversarial always run once. Every module
# that needs this split (executor, report, run, app) imports it from here
# so the rule lives in exactly one place.
BULK_CATEGORIES = frozenset({"happy_path", "negative"})

EXECUTION_RESULT_SCHEMA = {
    "type": "object",
    "required": ["test_id", "run_id", "repetition", "execution_status", "actual_topic", "actual_action", "final_response", "actions_taken", "transcript_path"],
    "properties": {
        "test_id": {"type": "string"},
        "run_id": {"type": "string"},
        "repetition": {"type": "number"},
        "execution_status": {"type": "string", "enum": ["OK", "ERROR"]},
        "actual_topic": {"type": "string"},
        "actual_action": {"type": "string"},
        "final_response": {"type": "string"},
        "actions_taken": {"type": "array", "items": {"type": "object"}},
        "transcript_path": {"type": "string"},
        "error_detail": {"type": "string"},
    },
}

JUDGE_VERDICT_SCHEMA = {
    "type": "object",
    "required": ["test_id", "run_id", "repetition", "verdict", "topic_match", "action_match", "rationale"],
    "properties": {
        "test_id": {"type": "string"},
        "run_id": {"type": "string"},
        "repetition": {"type": "number"},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "EXCLUDED"]},
        "topic_match": {"type": "boolean"},
        "action_match": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}

HALLUCINATION_CLAIM_SCHEMA = {
    "type": "object",
    "required": ["test_id", "run_id", "repetition", "claim", "evidence_status", "severity", "detail"],
    "properties": {
        "test_id": {"type": "string"},
        "run_id": {"type": "string"},
        "repetition": {"type": "number"},
        "claim": {"type": "string"},
        "evidence_status": {"type": "string", "enum": ["supported", "unsupported"]},
        "severity": {"type": "string", "enum": ["none", "low", "medium", "critical"]},
        "detail": {"type": "string"},
    },
}


class SchemaValidationError(Exception):
    pass


def _check_type(value, expected, path):
    types = {"object": dict, "array": list, "string": str, "number": (int, float), "boolean": bool}
    py_type = types.get(expected)
    if py_type is not None and not isinstance(value, py_type):
        raise SchemaValidationError(f"{path}: expected {expected}, got {type(value).__name__}")


def _validate_node(value, schema, path="$"):
    expected_type = schema.get("type")
    if expected_type:
        _check_type(value, expected_type, path)

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: {value!r} not in allowed values {schema['enum']}")

    if expected_type == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaValidationError(f"{path}: missing required field {key!r}")
        for key, subschema in schema.get("properties", {}).items():
            if key in value:
                _validate_node(value[key], subschema, f"{path}.{key}")

    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema:
            for idx, item in enumerate(value):
                _validate_node(item, item_schema, f"{path}[{idx}]")


def validate(obj, schema) -> None:
    _validate_node(obj, schema)


def is_valid(obj, schema) -> bool:
    try:
        validate(obj, schema)
        return True
    except SchemaValidationError:
        return False
