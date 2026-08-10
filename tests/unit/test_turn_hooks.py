"""Unit tests for AgentHub turn hooks (Phase 7.1).

Two layers:
- the core hub mechanism (``agenthub.TurnHooks``) fires every hook, in order, and
  the no-op default preserves the pre-hooks behavior;
- Axi's concrete ``AxiTurnHooks`` adapter delegates each hook to the right
  ``agents.py`` behavior.
"""

from __future__ import annotations

import os

# Bootstrap a dummy env before importing axi so config's import-time token
# resolution (discord_config.py) does not fail in a headless run.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import asyncio
import tempfile
from typing import Any

import pytest

from agenthub import AgentHub, AgentSession, FrontendRouter, TurnHooks, TurnOutcome
from agenthub.stream_types import QueryResult, StreamEnd, StreamStart


async def _result_stream(session: Any, **kwargs: Any):
    yield StreamStart()
    yield QueryResult(session_id=f"sid-{session.name}", cost_usd=0.01, num_turns=1, duration_ms=25)
    yield StreamEnd(elapsed_s=0.01, msg_count=1, flush_count=0)


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

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_hub(turn_hooks: Any = None, *, client_box: dict[str, _FakeClient] | None = None) -> AgentHub:
    router = FrontendRouter()

    async def create_client(session: Any, options: Any) -> _FakeClient:
        client = _FakeClient(session.name)
        if client_box is not None:
            client_box[session.name] = client
        return client

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    def make_agent_options(session: Any, session_id: str | None) -> dict[str, Any]:
        return {}

    return AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=make_agent_options,
        max_awake=3,
        query_timeout=1.0,
        stream_factory=_result_stream,
        turn_hooks=turn_hooks,
    )


class _RecordingHooks(TurnHooks):
    def __init__(self) -> None:
        self.events: list[str] = []
        self.transform_inputs: list[Any] = []
        self.after_outcomes: list[Any] = []

    async def before_turn(self, session: Any, turn: Any) -> None:
        self.events.append("before_turn")

    async def transform_content(self, session: Any, content: Any) -> Any:
        self.events.append("transform_content")
        self.transform_inputs.append(content)
        return f"WRAPPED::{content}"

    async def after_turn(self, session: Any, turn: Any, outcome: Any) -> None:
        self.events.append("after_turn")
        self.after_outcomes.append(outcome)

    def turn_scope(self, session: Any, turn: Any) -> Any:
        import contextlib

        recorder = self.events

        @contextlib.asynccontextmanager
        async def _scope() -> Any:
            recorder.append("scope_enter")
            try:
                yield
            finally:
                recorder.append("scope_exit")

        return _scope()


# --------------------------------------------------------------------------- #
# Core hub mechanism
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hooks_fire_in_order() -> None:
    hooks = _RecordingHooks()
    hub = _make_hub(hooks)
    await hub.spawn_agent(name="h1", cwd=tempfile.mkdtemp(prefix="h1_"))
    await hub.submit_user_message("h1", "hello")
    await asyncio.sleep(0.05)

    # scope wraps the whole turn; before precedes transform; after runs last (inside scope)
    assert hooks.events == [
        "scope_enter",
        "before_turn",
        "transform_content",
        "after_turn",
        "scope_exit",
    ]
    assert hooks.after_outcomes == [TurnOutcome.COMPLETED]


@pytest.mark.asyncio
async def test_transform_content_reaches_client() -> None:
    hooks = _RecordingHooks()
    box: dict[str, _FakeClient] = {}
    hub = _make_hub(hooks, client_box=box)
    await hub.spawn_agent(name="h2", cwd=tempfile.mkdtemp(prefix="h2_"))
    await hub.submit_user_message("h2", "ping")
    await asyncio.sleep(0.05)

    # the client receives the TRANSFORMED content, not the raw turn content
    assert box["h2"].queries == ["WRAPPED::ping"]
    assert hooks.transform_inputs == ["ping"]


@pytest.mark.asyncio
async def test_default_hooks_are_noop() -> None:
    box: dict[str, _FakeClient] = {}
    hub = _make_hub(None, client_box=box)
    assert isinstance(hub.turn_hooks, TurnHooks)  # defaulted, not None
    await hub.spawn_agent(name="h3", cwd=tempfile.mkdtemp(prefix="h3_"))
    await hub.submit_user_message("h3", "raw-content")
    await asyncio.sleep(0.05)

    # no-op default passes content through unchanged and the turn still completes
    assert box["h3"].queries == ["raw-content"]


# --------------------------------------------------------------------------- #
# Axi concrete adapter
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_axi_before_turn_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi.turn_hooks import AxiTurnHooks

    calls: list[Any] = []
    monkeypatch.setattr("axi.turn_hooks._channel_id_of", lambda s: 999)
    monkeypatch.setattr("axi.agents._reset_session_activity", lambda s: calls.append("reset"))
    monkeypatch.setattr("axi.agents.drain_stderr", lambda s: (calls.append("drain_stderr"), [])[1])
    monkeypatch.setattr("axi.agents.drain_sdk_buffer", lambda s: (calls.append("drain_sdk"), 0)[1])
    monkeypatch.setattr(
        "axi.log_context.set_agent_context",
        lambda name, channel_id=None: calls.append(("ctx", name, channel_id)),
    )

    session = AgentSession(name="a")
    await AxiTurnHooks().before_turn(session, None)

    assert "reset" in calls
    assert "drain_stderr" in calls
    assert "drain_sdk" in calls
    assert ("ctx", "a", 999) in calls  # channel id threaded into the log context
    assert session.bridge_busy is False


@pytest.mark.asyncio
async def test_axi_transform_content_delegates() -> None:
    from axi.turn_hooks import AxiTurnHooks

    # a non-flowcoder agent: _wrap_content_with_flowchart returns content unchanged
    session = AgentSession(name="c", agent_type="claude_code")
    out = await AxiTurnHooks().transform_content(session, "hello world")
    assert out == "hello world"


@pytest.mark.asyncio
async def test_axi_after_turn_calls_maybe_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi.turn_hooks import AxiTurnHooks

    seen: list[Any] = []

    async def fake_compact(session: Any, channel: Any = None) -> None:
        seen.append((session.name, channel))

    monkeypatch.setattr("axi.agents._maybe_compact", fake_compact)

    session = AgentSession(name="b")
    await AxiTurnHooks().after_turn(session, None, TurnOutcome.COMPLETED)
    assert seen == [("b", None)]  # called with the session, no Discord channel


@pytest.mark.asyncio
async def test_axi_turn_scope_runs_and_cleans_up() -> None:
    from axi.agents import _active_trace_ids
    from axi.turn_hooks import AxiTurnHooks

    session = AgentSession(name="d")
    ran = False
    async with AxiTurnHooks().turn_scope(session, None):
        ran = True
    assert ran
    # trace-id context is cleaned up on scope exit
    assert "d" not in _active_trace_ids
