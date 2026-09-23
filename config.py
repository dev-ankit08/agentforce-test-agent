"""
config.py -- models, org alias, agent id. Configure via a .env file in the
repo root (copy .env.example -> .env) or real environment variables -- both
work; .env is loaded first via python-dotenv if present, but real env vars
always take precedence over it.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # no-op if .env doesn't exist; does not override existing os.environ

# --- Model selection ---------------------------------------------------------
DESIGNER_MODEL = "claude-sonnet-5"  # user story -> test cases
JUDGE_MODEL = "claude-sonnet-5"     # transcript vs expected_outcome -> verdict + hallucination audit, one call

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

# --- Execution ------------------------------------------------------------------
DEFAULT_CONCURRENCY = 4

# --- Output paths ----------------------------------------------------------------
RESULTS_DIR = "results"
TRANSCRIPTS_DIR = "transcripts"
RUN_HISTORY_CSV = os.path.join(RESULTS_DIR, "run_history.csv")
