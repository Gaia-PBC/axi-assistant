"""Phase 8c — context + scope commands via commands_api + HTTP.

reset-context, model, verbose, debug, debug-all, plan share one core. Scope commands take an
explicit agent target: model is agent|None (None = global default); verbose/debug/plan are
per-agent; debug-all is the all-agents variant. (compact/clear are handled separately — they
send raw CLI slash commands and stream, which needs its own approach.)
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from agenthub import AgentSession
from axi import agents, commands_api, config
from axi.axi_types import discord_state


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    saved = dict(agents.agents)
    agents.agents.clear()
    yield
    agents.agents.clear()
    agents.agents.update(saved)


# --- verbose / debug (per-agent Discord-render flags) ---


def test_set_verbose_not_found() -> None:
    r = commands_api.set_verbose("ghost")
    assert r.ok is False and "not found" in r.message


def test_set_verbose_toggle_and_explicit() -> None:
    s = AgentSession(name="v1")
    agents.agents["v1"] = s
    discord_state(s).verbose = False  # normalize starting state (env can default it on)
    r = commands_api.set_verbose("v1")  # toggle -> on
    assert r.ok and discord_state(s).verbose is True and "**on**" in r.message
    r2 = commands_api.set_verbose("v1", "off")
    assert discord_state(s).verbose is False and "**off**" in r2.message
    r3 = commands_api.set_verbose("v1", "bogus")
    assert r3.ok is False and "Usage" in r3.message


def test_set_debug_toggle() -> None:
    s = AgentSession(name="d1")
    agents.agents["d1"] = s
    r = commands_api.set_debug("d1", "on")
    assert r.ok and discord_state(s).debug is True


def test_set_debug_all_sets_every_agent() -> None:
    for n in ("a", "b", "c"):
        agents.agents[n] = AgentSession(name=n)
    r = commands_api.set_debug_all("on")
    assert r.ok and r.data["count"] == 3
    assert all(discord_state(s).debug for s in agents.agents.values())
    r2 = commands_api.set_debug_all("off")
    assert all(not discord_state(s).debug for s in agents.agents.values())


# --- plan ---


@pytest.mark.asyncio
async def test_set_plan_not_found() -> None:
    r = await commands_api.set_plan("ghost")
    assert r.ok is False


@pytest.mark.asyncio
async def test_set_plan_toggles() -> None:
    s = AgentSession(name="p1")  # client None -> no set_permission_mode
    agents.agents["p1"] = s
    r = await commands_api.set_plan("p1")
    assert r.ok and s.plan_mode is True and "Plan mode ON" in r.message
    r2 = await commands_api.set_plan("p1")
    assert s.plan_mode is False and "Plan mode OFF" in r2.message


# --- model (agent|None target) ---


@pytest.mark.asyncio
async def test_model_view_global() -> None:
    r = await commands_api.set_model(None, None)
    assert r.ok and "Current default model" in r.message
    assert r.data["agent"] is None


@pytest.mark.asyncio
async def test_model_view_per_agent() -> None:
    s = AgentSession(name="m1")
    s.model = "sonnet"
    agents.agents["m1"] = s
    r = await commands_api.set_model("m1", None)
    assert "Current model for **m1**" in r.message
    assert r.data["agent"] == "m1"


@pytest.mark.asyncio
async def test_model_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # validate_model is lenient in general; force a rejection to exercise the error path.
    monkeypatch.setattr(config, "validate_model", lambda m: "Unknown model.")
    r = await commands_api.set_model(None, "whatever")
    assert r.ok is False and "Unknown model" in r.message


@pytest.mark.asyncio
async def test_model_set_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "validate_model", lambda m: "")
    monkeypatch.setattr(config, "normalize_model", lambda m: "opus")
    monkeypatch.setattr(config, "set_model", lambda m, provider=None: "")
    monkeypatch.setattr(config, "get_model", lambda: "opus")
    r = await commands_api.set_model(None, "opus")
    assert r.ok and "Default model set" in r.message
    assert r.data["agent"] is None


# --- reset-context ---


@pytest.mark.asyncio
async def test_reset_context_not_found() -> None:
    r = await commands_api.reset_context("ghost")
    assert r.ok is False


@pytest.mark.asyncio
async def test_reset_context_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    s = AgentSession(name="r1", cwd="/tmp/r1")
    agents.agents["r1"] = s
    monkeypatch.setattr(agents, "reset_session", AsyncMock(return_value=s))
    r = await commands_api.reset_context("r1")
    assert r.ok and "Context reset for **r1**" in r.message
    assert r.data["cwd"] == "/tmp/r1"


# --- HTTP endpoints ---


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from axi import config as cfg
    from axi import http_api

    monkeypatch.setattr(cfg, "HTTP_API_TOKEN", "")
    return TestClient(http_api.app)


def test_http_scope_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    # model view (global) -> ok
    resp = client.post("/v1/model", json={})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    # debug-all with no agents -> ok
    resp2 = client.post("/v1/debug-all", json={"mode": "on"})
    assert resp2.status_code == 200 and resp2.json()["ok"] is True
    # verbose/plan/reset on unknown agent -> ok False
    for path in ("/v1/agents/ghost/verbose", "/v1/agents/ghost/plan", "/v1/agents/ghost/reset"):
        r = client.post(path, json={})
        assert r.status_code == 200, (path, r.text)
        assert r.json()["ok"] is False, path
