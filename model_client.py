"""
model_client.py -- the only place this project talks to Claude. No
stub/mock mode: every function here makes a real Anthropic API call and
raises if ANTHROPIC_API_KEY isn't set. Each call asks for a JSON object back
and parses it; a malformed response is a hard error, not silently swallowed.
"""
from __future__ import annotations

import json

import anthropic

import assertions
import config
from resilience import ModelCallError, with_retries  # noqa: F401  (ModelCallError re-exported for callers)


def _transient_errors() -> tuple[type[BaseException], ...]:
    """Anthropic SDK error classes worth retrying (rate limits, timeouts,
    connection drops, 5xx), resolved defensively so an SDK rename can't break
    import. A malformed-JSON response (json.JSONDecodeError) is also retried --
    asking the model once more usually yields clean JSON."""
    names = ["RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError", "APIStatusError"]
    types = tuple(getattr(anthropic, n) for n in names if hasattr(anthropic, n))
    return (types or (Exception,)) + (json.JSONDecodeError,)


_RETRY_ON = _transient_errors()

DESIGNER_SYSTEM_PROMPT = """You are a test designer for an Agentforce (Salesforce AI agent) regression \
test suite. Given a user story with numbered acceptance criteria, produce a JSON array of test cases.

For EVERY acceptance criterion, produce exactly one "bulk pair" tagged with that criterion's id:
  - one "happy_path" case: the condition(s) in that criterion ARE met, and the agent should produce \
the behavior the criterion describes.
  - one "negative" case: the condition(s) in that criterion are NOT met (negate the precondition), and \
the agent must NOT produce that behavior -- e.g. if the criterion is "if support tier is Premier, show \
X", the negative case asks the same thing for a non-Premier tier and expects X to NOT be shown.
Both cases in a pair share the same ac_id (e.g. "AC1") so they can be grouped and bulk-repeated together.

In addition to the per-AC bulk pairs, add "edge_case" cases for boundary/missing-data situations and \
"adversarial" cases designed to tempt the agent into fabricating information it doesn't actually have. \
Always include at least one adversarial case per story. Tag each with the ac_id it's most relevant to, \
or "general" if it doesn't map to one specific criterion.

Each test case object must have exactly these fields:
  test_id (short unique string, e.g. "TC-001")
  utterance (what the customer/user actually types to the agent)
  expected_topic (the Agentforce topic this should route to)
  expected_action (the action/tool the agent should invoke, or "none")
  expected_outcome (one or two sentences describing what a correct response contains)
  category ("happy_path", "negative", "edge_case", or "adversarial")
  ac_id (the acceptance criterion this case validates, e.g. "AC1", or "general")

Return ONLY a JSON array of these objects, no prose, no markdown fences."""

CUSTOMER_SYSTEM_PROMPT = """You are role-playing a customer talking to a Salesforce Agentforce agent, \
so that the agent can be tested. You will be given a SCENARIO describing your goal/persona and the \
CONVERSATION SO FAR (the customer and agent messages exchanged). Produce the customer's next message to \
drive the scenario forward realistically.

Stay in character as the customer -- never act as the agent or the tester. Keep each message to what a \
real customer would actually type. Stop as soon as the scenario's goal has been reached or it is clearly \
stalled/refused; do not pad the conversation with filler turns.

Return ONLY a JSON object with exactly these fields:
  message (the customer's next message to the agent; empty string if you are stopping)
  status ("continue" to keep going, or "stop" when the goal is reached or cannot be reached)"""

JUDGE_SYSTEM_PROMPT = """You are grading a single Agentforce test case and auditing it for hallucinations \
in one pass. You will be given the expected topic, expected action, expected outcome, the actual \
transcript (which topic/action the agent used and its final response), and the full list of \
actions/tools the agent actually invoked during the conversation, including what each one returned. \
The transcript may be a single turn or a multi-turn conversation (a `turns` list, oldest first); when \
present, grade against the whole conversation and the agent's final response.

Grading: decide PASS or FAIL based on whether the actual behavior satisfies the expected outcome -- not \
just whether the wording matches. Be strict: a response that is vague, evasive, or contradicts the \
expected outcome is a FAIL even if the topic/action matched.

Hallucination audit: extract every factual claim in the final response (record data, numbers, dates, \
order/case statuses, policy statements, promised follow-up actions) and decide, for each one, whether it \
is directly supported by something an action actually returned in this transcript. Do not use \
outside/general knowledge to decide support -- only this transcript's own action outputs count as \
evidence. A claim with no supporting action output is unsupported, i.e. a hallucination candidate.

Return ONLY a JSON object with exactly these fields:
  verdict ("PASS" or "FAIL")
  topic_match (boolean)
  action_match (boolean)
  rationale (one or two sentences explaining the verdict)
  hallucinations (a JSON array of objects, one per factual claim found in the final response, empty \
array if it made no factual claims; each object has exactly these fields:
    claim (the specific factual statement, quoted or closely paraphrased from the response)
    evidence_status ("supported" or "unsupported")
    severity ("none" for supported claims; for unsupported claims use "low" for vague/inconsequential, \
"medium" for a specific but non-critical fact, "critical" for a fabricated record identifier, number, \
status, or promise the customer could act on)
    detail (one sentence: what evidence was found, or why none was found))"""


def _client() -> anthropic.Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env or export it in your shell -- "
            "there is no stub/mock mode for model calls in this project."
        )
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _call(model: str, max_tokens: int, system: str, user_content: str):
    """One model call + JSON parse, wrapped in retry/backoff. Raises
    ModelCallError if it can't succeed within config.MODEL_RETRY_ATTEMPTS so the
    caller can isolate the failure instead of crashing the run."""
    def _do():
        resp = _client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return _extract_json(resp.content[0].text)

    return with_retries(
        _do,
        attempts=config.MODEL_RETRY_ATTEMPTS,
        base_delay=config.MODEL_RETRY_BASE_DELAY,
        retry_on=_RETRY_ON,
    )


def call_customer(scenario: str, history: list[dict]) -> dict:
    """Role-plays the next customer message in a multi_turn conversation test.
    `history` is the conversation so far (list of {customer, agent} turns).
    Returns {message, status}."""
    return _call(
        config.CUSTOMER_MODEL, 1024, CUSTOMER_SYSTEM_PROMPT,
        json.dumps({"scenario": scenario, "conversation_so_far": history}, indent=2),
    )


def call_test_designer(story_text: str) -> list[dict]:
    return _call(config.DESIGNER_MODEL, 4096, DESIGNER_SYSTEM_PROMPT, f"User story:\n\n{story_text}")


def call_judge(test_case: dict, execution_result: dict) -> dict:
    """One call does both grading and hallucination auditing -- see
    JUDGE_SYSTEM_PROMPT. Returns {verdict, topic_match, action_match,
    rationale, hallucinations: [...]}."""
    payload = {
        "expected_topic": test_case["expected_topic"],
        "expected_action": test_case["expected_action"],
        "expected_outcome": test_case["expected_outcome"],
        "actual_topic": execution_result["actual_topic"],
        "actual_action": execution_result["actual_action"],
        # Normalized (id-suffix/underscore/case-folded) names so the judge isn't
        # tripped up by Salesforce developer-name formatting -- see assertions.py.
        "expected_topic_normalized": assertions.normalize(test_case["expected_topic"]),
        "actual_topic_normalized": assertions.normalize(execution_result["actual_topic"]),
        "expected_action_normalized": assertions.normalize(test_case["expected_action"]),
        "actual_action_normalized": assertions.normalize(execution_result["actual_action"]),
        "final_response": execution_result["final_response"],
        "actions_taken": execution_result["actions_taken"],
    }
    if execution_result.get("turns"):
        payload["turns"] = execution_result["turns"]
    return _call(config.JUDGE_MODEL, 3072, JUDGE_SYSTEM_PROMPT, json.dumps(payload, indent=2))
