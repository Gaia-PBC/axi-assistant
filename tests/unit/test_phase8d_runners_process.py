"""Phase 8d — runners + process commands via commands_api + HTTP.

flowchart (normal hub submit), build-user-profile / build-music-preferences (raw-turn prompt
injection), restart (hot-reload via shutdown coordinator), restart-including-bridge (via a
main.py-registered handler). Voice (vc-join/vc-leave) stays Discord-only (no endpoint / no fn).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
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


class _FakeHub:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, Any, dict | None]] = []

    async def submit_user_message(self, name: str, content: Any, metadata: dict | None = None) -> None:
        self.submitted.append((name, content, metadata))


# --- flowchart ---


@pytest.mark.asyncio
async def test_flowchart_not_found() -> None:
    r = await commands_api.run_flowchart("ghost", "soul")
    assert r.ok is False and "not found" in r.message


@pytest.mark.asyncio
async def test_flowchart_requires_flowcoder() -> None:
    agents.agents["cc"] = AgentSession(name="cc", agent_type="claude_code")
    r = await commands_api.run_flowchart("cc", "soul")
    assert r.ok is False and "flowcoder" in r.message


@pytest.mark.asyncio
async def test_flowchart_submits_slash_content(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _FakeHub()
    monkeypatch.setattr(agents, "hub", hub)
    agents.agents["fc"] = AgentSession(name="fc", agent_type="flowcoder")
    r = await commands_api.run_flowchart("fc", "/soul", "do the thing")
    assert r.ok and "Flowchart `soul` started" in r.message
    name, content, metadata = hub.submitted[0]
    assert content == "/soul do the thing"  # NOT raw -> flowchart engine wraps + runs it


# --- profile / music interviews ---


@pytest.mark.asyncio
async def test_build_user_profile_not_found() -> None:
    r = await commands_api.build_user_profile("ghost")
    assert r.ok is False and "not found" in r.message


@pytest.mark.asyncio
async def test_build_music_not_found() -> None:
    r = await commands_api.build_music_preferences("ghost")
    assert r.ok is False and "not found" in r.message


# --- restart ---


@pytest.mark.asyncio
async def test_restart_not_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "shutdown_coordinator", None)
    r = await commands_api.restart()
    assert r.ok is False and "not fully initialized" in r.message


@pytest.mark.asyncio
async def test_restart_fires_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fired: list[Any] = []

    def fake_fire(coro: Any) -> None:
        fired.append(coro)
        if hasattr(coro, "close"):
            coro.close()

    async def _noop(reason: str) -> None:
        return None

    monkeypatch.setattr(agents, "fire_and_forget", fake_fire)
    monkeypatch.setattr(agents, "shutdown_coordinator", SimpleNamespace(force_shutdown=_noop, graceful_shutdown=_noop))
    r = await commands_api.restart(force=False)
    assert r.ok and "graceful restart" in r.message
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_restart_including_bridge_uses_registered_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_handler(force: bool) -> Any:
        return commands_api.CommandResult(message=f"full restart force={force}", data={"force": force})

    monkeypatch.setattr(commands_api, "_full_restart_handler", fake_handler)
    r = await commands_api.restart_including_bridge(force=True)
    assert r.ok and "full restart force=True" in r.message


@pytest.mark.asyncio
async def test_restart_including_bridge_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands_api, "_full_restart_handler", None)
    r = await commands_api.restart_including_bridge()
    assert r.ok is False and "not available" in r.message


# --- HTTP endpoints ---


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from axi import config as cfg
    from axi import http_api

    monkeypatch.setattr(cfg, "HTTP_API_TOKEN", "")
    return TestClient(http_api.app)


def test_http_runner_endpoints_reject_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    resp = client.post("/v1/flowchart", json={"agent": "ghost", "flowchart": "soul"})
    assert resp.status_code == 200 and resp.json()["ok"] is False
    for path in ("/v1/agents/ghost/build-profile", "/v1/agents/ghost/build-music"):
        r = client.post(path)
        assert r.status_code == 200 and r.json()["ok"] is False


def test_http_restart_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fire(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    async def _noop(reason: str) -> None:
        return None

    monkeypatch.setattr(agents, "fire_and_forget", fake_fire)
    monkeypatch.setattr(agents, "shutdown_coordinator", SimpleNamespace(force_shutdown=_noop, graceful_shutdown=_noop))
    client = _client(monkeypatch)
    resp = client.post("/v1/restart", json={"force": False})
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_voice_commands_have_no_http_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """vc-join/vc-leave are Discord-only: no /v1 route + no commands_api function."""
    assert not hasattr(commands_api, "vc_join")
    assert not hasattr(commands_api, "vc_leave")
    client = _client(monkeypatch)
    assert client.post("/v1/vc-join").status_code == 404
