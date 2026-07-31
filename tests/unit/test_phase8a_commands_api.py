"""Phase 8a — frontend-agnostic read/info commands (commands_api) + HTTP endpoints.

Verifies the shared command functions return structured CommandResults with NO Discord, and
that http_api exposes them as GET /v1 endpoints returning {ok, message, data}. The Discord
slash handlers are thin wrappers over the same functions.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from agenthub import AgentSession
from axi import agents, commands_api


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    saved = dict(agents.agents)
    agents.agents.clear()
    yield
    agents.agents.clear()
    agents.agents.update(saved)


# ---------------------------------------------------------------------------
# commands_api functions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_without_start_time_is_initializing() -> None:
    r = await commands_api.ping()
    assert r.ok
    assert "Bot uptime: initializing" in r.message
    assert r.data["bot_uptime_seconds"] is None
    assert "bridge_connected" in r.data
    assert r.data["latency_ms"] is None  # no Discord latency over the shared path


@pytest.mark.asyncio
async def test_ping_with_latency_and_start_time() -> None:
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC) - timedelta(seconds=65)
    r = await commands_api.ping(latency_ms=42, bot_start_time=start)
    assert r.message.startswith("Pong! Latency: 42ms")
    assert "Bot uptime: 0h 1m" in r.message
    assert r.data["latency_ms"] == 42
    assert r.data["bot_uptime_seconds"] >= 65


def test_list_agents_empty() -> None:
    r = commands_api.list_agents()
    assert r.message == "No active agents."
    assert r.data["agents"] == []


def test_list_agents_reports_sessions() -> None:
    s = AgentSession(name="worker", cwd="/tmp/worker")
    agents.agents["worker"] = s
    r = commands_api.list_agents()
    assert r.data["awake"] == 0  # no client -> sleeping
    row = r.data["agents"][0]
    assert row["name"] == "worker"
    assert row["state"] == "sleeping"
    assert row["cwd"] == "/tmp/worker"
    assert "**worker**" in r.message


def test_agent_status_all_empty() -> None:
    r = commands_api.agent_status(None)
    assert "No active agents" in r.message


def test_agent_status_missing_is_not_ok() -> None:
    r = commands_api.agent_status("ghost")
    assert r.ok is False
    assert "not found" in r.message
    assert r.ephemeral


def test_agent_status_single_session() -> None:
    s = AgentSession(name="a1", cwd="/tmp/a1")
    agents.agents["a1"] = s
    r = commands_api.agent_status("a1")
    assert r.ok
    assert "**a1**" in r.message
    assert "State: sleeping" in r.message
    assert r.data["name"] == "a1"


@pytest.mark.asyncio
async def test_claude_usage_no_data() -> None:
    r = await commands_api.claude_usage()
    assert "Claude Usage" in r.message
    assert r.data["total_queries"] == 0
    assert r.data["rate_limits"] == []


def test_flowchart_list_returns_structured_commands() -> None:
    r = commands_api.flowchart_list()
    assert isinstance(r.data["commands"], list)
    # message either lists commands or reports none — always a str
    assert isinstance(r.message, str) and r.message


# ---------------------------------------------------------------------------
# HTTP endpoints (same functions, over REST)
# ---------------------------------------------------------------------------


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from axi import config, http_api

    monkeypatch.setattr(config, "HTTP_API_TOKEN", "")  # disable bearer for the test
    return TestClient(http_api.app)


def test_http_read_endpoints_return_ok_message_data(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    for path in ("/v1/ping", "/v1/agents", "/v1/status", "/v1/usage", "/v1/flowcharts"):
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        body = resp.json()
        assert set(body) >= {"ok", "message", "data"}, (path, body)


def test_http_agent_status_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    s = AgentSession(name="httpa", cwd="/tmp/httpa")
    agents.agents["httpa"] = s
    client = _client(monkeypatch)
    resp = client.get("/v1/agents/httpa/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["name"] == "httpa"
    # missing agent -> ok False
    resp2 = client.get("/v1/agents/nope/status")
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is False
