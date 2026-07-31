"""Phase 8c (part 2) — compact/clear via a hub 'raw' turn.

compact/clear send raw CLI slash commands. They now go through the hub as a 'raw' turn
(metadata raw=True) which skips the flowchart content-transform but keeps the hub's turn
accounting + streaming. commands_api.compact/clear + POST /v1 endpoints expose them; the
Discord handlers are thin wrappers.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from agenthub import AgentHub, AgentSession, FrontendRouter, TurnHooks
from agenthub.stream_types import QueryResult, StreamEnd, StreamStart
from axi import agents, commands_api


class _FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: list[Any] = []

    async def query(self, content: Any) -> None:
        self.queries.append(content)

    async def interrupt(self) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


async def _ok_stream(session: Any, **kwargs: Any) -> Any:
    yield StreamStart()
    yield QueryResult(session_id=f"sid-{session.name}", cost_usd=0.0, num_turns=1, duration_ms=1)
    yield StreamEnd(elapsed_s=0.0, msg_count=1, flush_count=0)


class _WrapHooks(TurnHooks):
    async def transform_content(self, session: Any, content: Any) -> Any:
        return f"WRAPPED::{content}"


@pytest.mark.asyncio
async def test_raw_turn_skips_content_transform() -> None:
    box: dict[str, _FakeClient] = {}

    async def create_client(session: Any, options: Any) -> _FakeClient:
        c = _FakeClient(session.name)
        box[session.name] = c
        return c

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[FrontendRouter()],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda s, i: {},
        max_awake=3,
        query_timeout=2.0,
        stream_factory=_ok_stream,
        turn_hooks=_WrapHooks(),
    )
    await hub.spawn_agent(name="rw", cwd=tempfile.mkdtemp(prefix="rw_"))
    await hub.submit_user_message("rw", "normal")
    await hub.submit_user_message("rw", "/compact", metadata={"raw": True})

    for _ in range(100):
        await asyncio.sleep(0.02)
        if box.get("rw") and len(box["rw"].queries) >= 2:
            break

    queries = box["rw"].queries
    assert "WRAPPED::normal" in queries  # a normal turn goes through transform_content
    assert "/compact" in queries  # the raw turn bypasses it (literal command to the CLI)


# --- commands_api.compact / clear ---


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


@pytest.mark.asyncio
async def test_compact_not_found() -> None:
    r = await commands_api.compact("ghost")
    assert r.ok is False and "not found" in r.message


@pytest.mark.asyncio
async def test_compact_busy_rejected() -> None:
    s = AgentSession(name="busy")
    s.client = object()
    await s.query_lock.acquire()
    try:
        agents.agents["busy"] = s
        r = await commands_api.compact("busy")
        assert r.ok is False and "busy" in r.message
    finally:
        s.query_lock.release()


@pytest.mark.asyncio
async def test_compact_submits_raw_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _FakeHub()
    monkeypatch.setattr(agents, "hub", hub)
    s = AgentSession(name="c1")
    s.compact_instructions = "keep the key facts"
    agents.agents["c1"] = s
    r = await commands_api.compact("c1")
    assert r.ok and "Compacting" in r.message
    name, content, metadata = hub.submitted[0]
    assert name == "c1"
    assert content == "/compact keep the key facts"
    assert metadata == {"raw": True}


@pytest.mark.asyncio
async def test_clear_submits_raw_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _FakeHub()
    monkeypatch.setattr(agents, "hub", hub)
    agents.agents["c2"] = AgentSession(name="c2")
    r = await commands_api.clear("c2")
    assert r.ok and "Clearing" in r.message
    name, content, metadata = hub.submitted[0]
    assert content == "/clear"
    assert metadata == {"raw": True}


def test_http_compact_clear_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from axi import config as cfg
    from axi import http_api

    monkeypatch.setattr(cfg, "HTTP_API_TOKEN", "")
    client = TestClient(http_api.app)
    for path in ("/v1/agents/ghost/compact", "/v1/agents/ghost/clear"):
        resp = client.post(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.json()["ok"] is False
