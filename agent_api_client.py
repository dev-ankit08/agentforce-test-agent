"""
agent_api_client.py -- the one Salesforce integration in this project.

No separate OAuth client / token / URL to configure: the access token and
instance URL are read from the `sf` CLI's already-authenticated session for
config.SF_ORG_ALIAS (`sf org display --json`), the same way `sf` itself
authenticates any other command against that org.

Drives the Agentforce Agent API session lifecycle over plain HTTP:
  POST {instanceUrl}/einstein/ai-agent/v1/agents/{agentId}/sessions
  POST {instanceUrl}/einstein/ai-agent/v1/sessions/{sessionId}/messages
  DELETE {instanceUrl}/einstein/ai-agent/v1/sessions/{sessionId}

Reference: https://developer.salesforce.com/docs/ai/agentforce/references/agent-api
Endpoint paths should be reconfirmed against that page for your API version
before the first real run -- Salesforce has iterated this surface across
2025-2026 releases.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid

import requests

import config


class AgentApiError(Exception):
    """Raised for any failure talking to the Agent API or resolving `sf` auth."""


def _resolve_sf_executable() -> str:
    path = shutil.which("sf")
    if not path:
        raise AgentApiError(
            "The `sf` (Salesforce CLI) executable was not found on PATH. Install it from "
            "https://developer.salesforce.com/tools/salesforcecli and confirm `sf org list` "
            "shows your target org as Connected."
        )
    return path


def _org_credentials(org_alias: str) -> tuple[str, str]:
    """Returns (access_token, instance_url) for the given `sf`-authenticated org alias."""
    sf_path = _resolve_sf_executable()
    try:
        proc = subprocess.run(
            [sf_path, "org", "display", "--target-org", org_alias, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=config.SF_CLI_TIMEOUT_SECONDS,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AgentApiError(f"Could not run `sf`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentApiError(f"`sf org display` timed out after {config.SF_CLI_TIMEOUT_SECONDS}s") from exc

    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentApiError(f"`sf org display` returned malformed JSON: {proc.stdout!r}") from exc

    if parsed.get("status") != 0:
        raise AgentApiError(parsed.get("message") or f"`sf org display --target-org {org_alias}` failed")

    result = parsed.get("result", {})
    access_token = result.get("accessToken")
    instance_url = result.get("instanceUrl")
    if not access_token or not instance_url:
        raise AgentApiError(
            f"`sf org display --target-org {org_alias}` did not return an access token / instance URL. "
            f"Run `sf org login web --alias {org_alias}` and retry."
        )
    return access_token, instance_url


class AgentApiClient:
    """One client instance per test run. Not thread-safe by itself -- executor.py
    creates one client per worker thread, since each holds its own HTTP session
    but credentials are cheap to re-resolve."""

    def __init__(self, org_alias: str | None = None, agent_api_name: str | None = None):
        self.org_alias = org_alias or config.SF_ORG_ALIAS
        self.agent_api_name = agent_api_name or config.AGENT_API_NAME
        if not self.org_alias:
            raise AgentApiError("AGENTFORCE_SF_ORG_ALIAS is not set in .env")
        if not self.agent_api_name:
            raise AgentApiError("AGENTFORCE_AGENT_API_NAME is not set in .env")
        self.access_token, self.instance_url = _org_credentials(self.org_alias)
        self._base = f"{self.instance_url}/einstein/ai-agent/{config.AGENT_API_VERSION}"
        self._headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def start_session(self) -> str:
        resp = requests.post(
            f"{self._base}/agents/{self.agent_api_name}/sessions",
            headers=self._headers,
            json={
                "externalSessionKey": str(uuid.uuid4()),
                "instanceConfig": {"endpoint": self.instance_url},
                "streamingCapabilities": {"chunkTypes": ["Text"]},
                "bypassUser": True,
            },
            timeout=config.AGENT_API_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 300:
            raise AgentApiError(f"start_session failed ({resp.status_code}): {resp.text}")
        return resp.json()["sessionId"]

    def send_message(self, session_id: str, text: str, sequence_id: int) -> dict:
        resp = requests.post(
            f"{self._base}/sessions/{session_id}/messages",
            headers=self._headers,
            json={"message": {"sequenceId": sequence_id, "type": "Text", "text": text}},
            timeout=config.AGENT_API_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 300:
            raise AgentApiError(f"send_message failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def end_session(self, session_id: str) -> None:
        requests.delete(
            f"{self._base}/sessions/{session_id}",
            headers={**self._headers, "x-session-end-reason": "UserRequest"},
            timeout=config.AGENT_API_TIMEOUT_SECONDS,
        )
