"""Phase 8b — turn control + lifecycle commands via commands_api + HTTP.

stop/skip/spawn/kill-agent/restart-agent now share one frontend-agnostic core; Discord
handlers wrap it (keeping Discord-only bits: futures/reactions/background spawn), and http_api
exposes POST endpoints. Verifies the validation/error paths deterministically (happy paths that
create real CLI sessions are exercised by the live smoke).
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from agenthub import AgentSession
from axi import agents, commands_api, config


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    saved = dict(agents.agents)
    agents.agents.clear()
    yield
    agents.agents.clear()
    agents.agents.update(saved)


# --- stop / skip ---


@pytest.mark.asyncio
async def test_stop_not_found() -> None:
    r = await commands_api.stop("ghost")
    assert r.ok is False and "not found" in r.message


@pytest.mark.asyncio
async def test_stop_not_busy() -> None:
    agents.agents["idle"] = AgentSession(name="idle")  # client is None -> not busy
    r = await commands_api.stop("idle")
    assert r.ok is False and "not busy" in r.message


@pytest.mark.asyncio
async def test_skip_not_busy() -> None:
    agents.agents["idle"] = AgentSession(name="idle")
    r = await commands_api.skip("idle")
    assert r.ok is False and "not busy" in r.message


# --- spawn validation ---


def test_validate_spawn_empty_name() -> None:
    r = commands_api.validate_spawn("", None, None)
    assert r.ok is False and "empty" in r.message


def test_validate_spawn_reserved_master() -> None:
    r = commands_api.validate_spawn(config.MASTER_AGENT_NAME, None, None)
    assert r.ok is False and "reserved" in r.message


def test_validate_spawn_disallowed_cwd() -> None:
    r = commands_api.validate_spawn("ok", "/etc/definitely-not-allowed", None)
    assert r.ok is False and "allowed directories" in r.message


@pytest.mark.asyncio
async def test_spawn_existing_without_resume_refused() -> None:
    agents.agents["dup"] = AgentSession(name="dup")
    r = await commands_api.spawn("dup", "hello")
    assert r.ok is False and "already exists" in r.message


# --- kill / restart ---


@pytest.mark.asyncio
async def test_kill_not_found() -> None:
    r = await commands_api.kill_agent("ghost")
    assert r.ok is False and "not found" in r.message


@pytest.mark.asyncio
async def test_kill_master_refused() -> None:
    agents.agents[config.MASTER_AGENT_NAME] = AgentSession(name=config.MASTER_AGENT_NAME)
    r = await commands_api.kill_agent(config.MASTER_AGENT_NAME)
    assert r.ok is False and "master" in r.message.lower()


@pytest.mark.asyncio
async def test_restart_not_found() -> None:
    r = await commands_api.restart_agent("ghost")
    assert r.ok is False and "not found" in r.message


@pytest.mark.asyncio
async def test_restart_master_refused() -> None:
    agents.agents[config.MASTER_AGENT_NAME] = AgentSession(name=config.MASTER_AGENT_NAME)
    r = await commands_api.restart_agent(config.MASTER_AGENT_NAME)
    assert r.ok is False


# --- HTTP endpoints ---


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from axi import config as cfg
    from axi import http_api

    monkeypatch.setattr(cfg, "HTTP_API_TOKEN", "")
    return TestClient(http_api.app)


def test_http_lifecycle_endpoints_reject_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    for path in (
        "/v1/agents/ghost/stop",
        "/v1/agents/ghost/skip",
        "/v1/agents/ghost/kill",
        "/v1/agents/ghost/restart",
    ):
        resp = client.post(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.json()["ok"] is False, path


def test_http_spawn_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    resp = client.post("/v1/spawn", json={"name": "", "prompt": "x"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    # reserved master name
    resp2 = client.post("/v1/spawn", json={"name": config.MASTER_AGENT_NAME, "prompt": "x"})
    assert resp2.json()["ok"] is False
