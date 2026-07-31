"""Test Phase 7 (capstone) — the hub is frontend-agnostic under StubFrontend, zero Discord.

The individual pieces are covered elsewhere (spawn 74d, wake/kill 74c, sleep 75d,
initial-prompt-no-channel 73, queue-drain stop_queue, reconnect 75c, compaction 75b). This
ties them together into cohesive end-to-end proofs that the FULL Axi lifecycle + turn loop
run through the real AgentHub with AxiTurnHooks and only a StubFrontend attached — no Discord
objects, no channel.id, no legacy process_message.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from agenthub import AgentHub, TurnOutcome
from agenthub.stream_types import QueryResult, StreamEnd, StreamStart
from agenthub.stub_frontend import StubFrontend


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


async def _hang_stream(session: Any, **kwargs: Any) -> Any:
    # Emit one event, then hang past the query timeout so the hub's turn timeout fires.
    yield StreamStart()
    await asyncio.sleep(5)


def _patch_axi_turn_deps(monkeypatch: pytest.MonkeyPatch, stub: StubFrontend) -> dict[str, _FakeClient]:
    """Neutralize AxiTurnHooks.before_turn's Discord-flavored side-effects + route posts to stub."""
    monkeypatch.setattr("axi.agents._reset_session_activity", lambda s: None)
    monkeypatch.setattr("axi.agents.drain_stderr", lambda s: [])
    monkeypatch.setattr("axi.agents.drain_sdk_buffer", lambda s: 0)
    monkeypatch.setattr("axi.agents._wrap_content_with_flowchart", lambda content, session: content)
    monkeypatch.setattr("axi.turn_hooks._channel_id_of", lambda s: None)
    monkeypatch.setattr("axi.agents._get_router", lambda: stub)
    return {}


def _build_axi_hub(stub: StubFrontend, box: dict[str, _FakeClient], *, stream: Any, query_timeout: float = 2.0) -> AgentHub:
    from axi.turn_hooks import AxiTurnHooks

    async def create_client(session: Any, options: Any) -> _FakeClient:
        c = _FakeClient(session.name)
        box[session.name] = c
        return c

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    return AgentHub(
        frontends=[stub],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, session_id: {},
        max_awake=4,
        query_timeout=query_timeout,
        stream_factory=stream,
        turn_hooks=AxiTurnHooks(),
    )


async def _wait_for(pred: Any, tries: int = 150, delay: float = 0.02) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 1. Full lifecycle sequence, frontend-agnostic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle_sequence_under_stub_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubFrontend()
    box = _patch_axi_turn_deps(monkeypatch, stub)
    hub = _build_axi_hub(stub, box, stream=_ok_stream)
    monkeypatch.setattr("axi.agents.hub", hub)

    # spawn -> drive a full turn -> (auto-sleep) -> remove, all with only a StubFrontend.
    await hub.spawn_agent(name="cap", cwd=tempfile.mkdtemp(prefix="cap_"))
    await hub.submit_user_message("cap", "hello")
    await _wait_for(lambda: any(c.method == "on_sleep" for c in stub.log))  # turn done -> auto-sleep
    await hub.remove_agent("cap")

    methods = [c.method for c in stub.log]
    # every lifecycle event was broadcast to the frontend-agnostic router
    assert "on_spawn" in methods
    assert "on_wake" in methods
    assert "on_stream_event" in methods  # stream consumed + rendered via the frontend
    assert "on_sleep" in methods
    assert "on_kill" in methods
    # ordering: spawn -> wake -> stream -> sleep, kill last
    assert methods.index("on_spawn") < methods.index("on_wake") < methods.index("on_stream_event")
    assert methods.index("on_stream_event") < methods.index("on_sleep")
    assert methods.index("on_kill") == max(i for i, m in enumerate(methods) if m == "on_kill")
    # the turn actually drove end-to-end through the hub (client received the query)
    assert box["cap"].queries == ["hello"]


# ---------------------------------------------------------------------------
# 2. run_initial_prompt spawns + drives with NO Discord channel (no channel.id error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_prompt_drives_without_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    stub = StubFrontend()
    box = _patch_axi_turn_deps(monkeypatch, stub)
    hub = _build_axi_hub(stub, box, stream=_ok_stream)
    monkeypatch.setattr("axi.agents.hub", hub)

    session = await hub.spawn_agent(name="ip", cwd=tempfile.mkdtemp(prefix="ip_"))

    # run_initial_prompt takes NO channel object — routing is frontend_state-based. This must
    # not raise AttributeError: 'NoneType'/channel object has no attribute 'id'.
    await agents.run_initial_prompt(session, "kick off the work")
    await _wait_for(lambda: box.get("ip") is not None and box["ip"].queries)

    # the initial-prompt turn drove through the hub (content carried the prompt), zero Discord
    assert any("kick off the work" in str(q) for q in box["ip"].queries)
    # after_turn posted the finished-initial-task notice via the frontend-agnostic router
    await _wait_for(lambda: any(c.method == "post_system" for c in stub.log))
    assert any(c.method == "post_system" for c in stub.log)


# ---------------------------------------------------------------------------
# 3. Timeout recovery — the hub recovers + AxiTurnHooks.turn_scope cleans up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_timeout_recovers_and_cleans_axi_span(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi.agents import _active_trace_ids

    stub = StubFrontend()
    box = _patch_axi_turn_deps(monkeypatch, stub)
    hub = _build_axi_hub(stub, box, stream=_hang_stream, query_timeout=0.15)
    monkeypatch.setattr("axi.agents.hub", hub)

    await hub.spawn_agent(name="to", cwd=tempfile.mkdtemp(prefix="to_"))
    await hub.submit_user_message("to", "will hang")

    # The hanging stream trips the query timeout -> hub sleeps the agent (recovery).
    await _wait_for(lambda: any(c.method == "on_sleep" for c in stub.log))
    assert any(c.method == "on_sleep" for c in stub.log)
    # AxiTurnHooks.turn_scope's trace-id context is cleaned up even on the timeout path.
    assert "to" not in _active_trace_ids


# ---------------------------------------------------------------------------
# 4. Regression guard — the legacy lifecycle deleted in 7.5f stays gone
# ---------------------------------------------------------------------------


def test_legacy_lifecycle_functions_are_deleted() -> None:
    from axi import agents

    for name in (
        "process_message",
        "process_message_queue",
        "_process_inter_agent_prompt",
        "wake_or_queue",
        "wake_agent",
        "_retry_stream_via_router",
        "handle_query_timeout",
    ):
        assert not hasattr(agents, name), f"legacy {name} was resurrected"
    # and the hub-routed survivors are still present
    for name in ("run_initial_prompt", "send_prompt_to_agent", "_drain_inflight_stream", "sleep_agent"):
        assert hasattr(agents, name), f"expected survivor {name} missing"
