"""
model_client.py -- the only place this project talks to Claude. No
stub/mock mode: every function here makes a real Anthropic API call and
raises if ANTHROPIC_API_KEY isn't set. Each call asks for a JSON object back
and parses it; a malformed response is a hard error, not silently swallowed.
"""
from __future__ import annotations

import json

import anthropic

import config

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

JUDGE_SYSTEM_PROMPT = """You are grading a single Agentforce test case and auditing it for hallucinations \
in one pass. You will be given the expected topic, expected action, expected outcome, the actual \
transcript (which topic/action the agent used and its final response), and the full list of \
actions/tools the agent actually invoked during the conversation, including what each one returned.

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


def call_test_designer(story_text: str) -> list[dict]:
    resp = _client().messages.create(
        model=config.DESIGNER_MODEL,
        max_tokens=4096,
        system=DESIGNER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"User story:\n\n{story_text}"}],
    )
    return _extract_json(resp.content[0].text)


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
        "final_response": execution_result["final_response"],
        "actions_taken": execution_result["actions_taken"],
    }
    resp = _client().messages.create(
        model=config.JUDGE_MODEL,
        max_tokens=3072,
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
    )
    return _extract_json(resp.content[0].text)
