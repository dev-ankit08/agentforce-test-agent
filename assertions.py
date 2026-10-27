"""
assertions.py -- deterministic, no-LLM checks on an execution result, plus
topic/action name normalization.

Borrowed (and slimmed to pure Python) from the agent-test-framework's
trace-assertion idea: cheap structural checks that can run *before* the LLM
judge so grading doesn't depend entirely on the model. Two kinds:

  - normalize()/names_match(): strip Salesforce id suffixes, underscores, and
    case, and treat configured alias sets as equivalent -- so a correct answer
    routed to a renamed/aliased topic isn't a false failure.
  - evaluate(): when a test case declares `expected_actions` and/or
    `expected_action_path`, check them against the actions the agent actually
    invoked. These are a hard gate: a mismatch is a deterministic FAIL with no
    model call.

All assertion inputs are optional strings on the test case, so cases that don't
declare them behave exactly as before (assertion "did not run", fall through to
the LLM judge).
"""
from __future__ import annotations

import re

import config

# Salesforce often appends an opaque id suffix to topic/action developer names
# (e.g. "Order_Status_16j0x000000XyZa"). Strip a trailing _<8+ alnum> chunk.
_ID_SUFFIX = re.compile(r"_[0-9A-Za-z]{8,}$")

# expected_actions / expected_action_path authoring format: "A -> B -> C".
# Commas are also accepted as separators for the order-independent
# expected_actions list. "..." is a path wildcard (see _match_sequence).
_SEP = re.compile(r"->|,")


def normalize(name: str | None) -> str:
    """Canonicalize a topic/action name for comparison: drop a trailing id
    suffix, turn underscores into spaces, collapse whitespace, lowercase."""
    if not name:
        return ""
    name = _ID_SUFFIX.sub("", str(name))
    name = name.replace("_", " ")
    return " ".join(name.lower().split())


def names_match(expected: str | None, actual: str | None, aliases: list[list[str]] | None = None) -> bool:
    """True if expected and actual normalize to the same name, or both belong to
    the same configured alias group. `aliases` is a list of groups, each a list
    of equivalent names."""
    ne, na = normalize(expected), normalize(actual)
    if ne == na:
        return True
    for group in aliases or []:
        normed = {normalize(m) for m in group}
        if ne in normed and na in normed:
            return True
    return False


def action_names(actions_taken: list[dict] | None) -> list[str]:
    """Pull the invoked action/tool names out of an execution result's
    actions_taken list (messages whose type isn't Text/Inform), in order.
    Defensive about the exact field the Agent API uses for the name."""
    names: list[str] = []
    for a in actions_taken or []:
        if not isinstance(a, dict):
            continue
        name = a.get("actionName") or a.get("name") or a.get("type") or ""
        if name:
            names.append(name)
    return names


def parse_list(spec: str | None) -> list[str]:
    """Parse an authored '->'/','-separated action spec into tokens, preserving
    the '...' wildcard token. Empty/blank -> []."""
    if not spec:
        return []
    return [t.strip() for t in _SEP.split(str(spec)) if t.strip()]


def _match_sequence(pattern: list[str], actual: list[str], aliases: list[list[str]] | None) -> bool:
    """Ordered match of `actual` against `pattern`, where a '...' token in the
    pattern matches any run (zero or more) of actual items. Without any '...',
    this requires an exact in-order, same-length match."""
    if not pattern:
        return not actual
    head, rest = pattern[0], pattern[1:]
    if head == "...":
        # '...' consumes 0..len(actual) leading items.
        return any(_match_sequence(rest, actual[i:], aliases) for i in range(len(actual) + 1))
    if actual and names_match(head, actual[0], aliases):
        return _match_sequence(rest, actual[1:], aliases)
    return False


def check_actions_present(expected: list[str], actual_names: list[str], aliases: list[list[str]] | None) -> list[str]:
    """Return the subset of `expected` action names that are NOT present anywhere
    in actual_names (order-independent). Empty list == all present."""
    return [e for e in expected if not any(names_match(e, a, aliases) for a in actual_names)]


def evaluate(test_case: dict, execution_result: dict) -> dict:
    """Run the deterministic assertions declared on a test case.

    Returns a dict:
      has_hard_assertions: bool  -- whether expected_actions/expected_action_path were declared
      passed: bool               -- meaningful only if has_hard_assertions
      detail: str                -- human-readable summary
      topic_match: bool          -- normalized expected_topic vs actual_topic (advisory)
      action_match: bool         -- normalized expected_action vs actual_action/actions (advisory)
    """
    actual_names = action_names(execution_result.get("actions_taken"))

    topic_match = names_match(
        test_case.get("expected_topic"), execution_result.get("actual_topic"), config.TOPIC_ALIASES
    )

    expected_action = (test_case.get("expected_action") or "").strip()
    if expected_action and expected_action.lower() != "none":
        action_match = names_match(expected_action, execution_result.get("actual_action"), config.ACTION_ALIASES) or any(
            names_match(expected_action, a, config.ACTION_ALIASES) for a in actual_names
        )
    else:
        # "none" expected: a match means the agent invoked no actions.
        action_match = len(actual_names) == 0

    expected_actions = parse_list(test_case.get("expected_actions"))
    expected_path = parse_list(test_case.get("expected_action_path"))
    has_hard = bool(expected_actions or expected_path)

    failures: list[str] = []
    if expected_actions:
        missing = check_actions_present(expected_actions, actual_names, config.ACTION_ALIASES)
        if missing:
            failures.append(f"missing required action(s): {', '.join(missing)}")
    if expected_path:
        if not _match_sequence(expected_path, actual_names, config.ACTION_ALIASES):
            failures.append(
                f"action path {' -> '.join(expected_path)} did not match actual {' -> '.join(actual_names) or '(none)'}"
            )

    if has_hard:
        passed = not failures
        detail = "deterministic assertions passed" if passed else "; ".join(failures)
    else:
        passed = True
        detail = ""

    return {
        "has_hard_assertions": has_hard,
        "passed": passed,
        "detail": detail,
        "topic_match": topic_match,
        "action_match": action_match,
    }
