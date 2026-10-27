"""
config.py -- models, org alias, agent id. Configure via a .env file in the
repo root (copy .env.example -> .env) or real environment variables -- both
work; .env is loaded first via python-dotenv if present, but real env vars
always take precedence over it.
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()  # no-op if .env doesn't exist; does not override existing os.environ

# --- Model selection ---------------------------------------------------------
DESIGNER_MODEL = "claude-sonnet-5"  # user story -> test cases
JUDGE_MODEL = "claude-sonnet-5"     # transcript vs expected_outcome -> verdict + hallucination audit, one call
CUSTOMER_MODEL = "claude-sonnet-5"  # role-plays the customer in multi_turn conversation tests

# --- Required configuration ----------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Salesforce org alias, already authenticated via the `sf` CLI on this machine.
# No token/URL configured here -- the Agent API access token is read from this
# `sf` session at run time. See agent_api_client.py.
SF_ORG_ALIAS = os.environ.get("AGENTFORCE_SF_ORG_ALIAS", "")

# Agent API agent id / developer name for the deployed AgentScript agent.
AGENT_API_NAME = os.environ.get("AGENTFORCE_AGENT_API_NAME", "")

# --- sf CLI / Agent API timeouts -----------------------------------------------
SF_CLI_TIMEOUT_SECONDS = 60
AGENT_API_TIMEOUT_SECONDS = 30
AGENT_API_VERSION = "v1"

# --- Retry / backoff (transient Anthropic + Agent API failures) -----------------
# A single transient hiccup should be retried, not abort a whole bulk run.
MODEL_RETRY_ATTEMPTS = int(os.environ.get("MODEL_RETRY_ATTEMPTS", "3"))
MODEL_RETRY_BASE_DELAY = float(os.environ.get("MODEL_RETRY_BASE_DELAY", "1.0"))
AGENT_API_RETRY_ATTEMPTS = int(os.environ.get("AGENT_API_RETRY_ATTEMPTS", "3"))
AGENT_API_RETRY_BASE_DELAY = float(os.environ.get("AGENT_API_RETRY_BASE_DELAY", "1.0"))

# --- Logging --------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()  # "text" or "json"

# --- Execution ------------------------------------------------------------------
DEFAULT_CONCURRENCY = 4

# --- Output paths ----------------------------------------------------------------
RESULTS_DIR = "results"
TRANSCRIPTS_DIR = "transcripts"
RUN_HISTORY_CSV = os.path.join(RESULTS_DIR, "run_history.csv")

# --- Topic / action aliases ------------------------------------------------------
# Optional aliases.json lets you declare sets of topic/action names that should
# be treated as equivalent during matching (see assertions.names_match), so a
# renamed/aliased topic isn't a false failure. Fail-soft: absent or malformed
# file => no aliases. Format:
#   {"topic_aliases":  [["Order Status", "Check Order"]],
#    "action_aliases": [["GetOrderStatus", "LookupOrder"]]}
ALIASES_FILE = os.environ.get("AGENTFORCE_ALIASES_FILE", "aliases.json")


def _load_aliases() -> tuple[list[list[str]], list[list[str]]]:
    try:
        with open(ALIASES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return [], []
    return data.get("topic_aliases", []) or [], data.get("action_aliases", []) or []


TOPIC_ALIASES, ACTION_ALIASES = _load_aliases()
