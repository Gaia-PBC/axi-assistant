"""Phase 7.5b — compaction routed through the hub.

Verifies the three parts of the compaction migration:
  1. hub_wiring._stream_factory threads Axi's compaction dicts into stream_response
     (so a CLI auto-compaction inside a hub turn records _pending_compact).
  2. agents._handle_pending_compact posts the completion summary and queues a
     "Continue from where you left off." auto-resume as a hub turn.
  3. agents._maybe_compact queues a "/compact" hub turn instead of the old nested
     client.query + _retry_stream_via_router, and AxiTurnHooks.after_turn orchestrates
     both (clearing the self-compact flag + guarding against re-compaction loops).
"""

from __future__ import annotations

import asyncio
import tempfile
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from agenthub import AgentHub, AgentSession, FrontendRouter, TurnOutcome
from agenthub.stream_types import QueryResult, StreamEnd, StreamStart


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


class _FakeHub:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, dict | None]] = []

    async def submit_user_message(self, name: str, content: str, metadata: dict | None = None) -> None:
        self.submitted.append((name, content, metadata))


class _FakeRouter:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def post_system(self, name: str, text: str) -> None:
        self.posts.append(text)


@pytest.fixture
def clean_compaction_state():
    """Snapshot + clear the module-global compaction dicts around each test."""
    from axi.discord_stream import _compact_start_times, _pending_compact, _self_compacting

    saved = (dict(_pending_compact), set(_self_compacting), dict(_compact_start_times))
    _pending_compact.clear()
    _self_compacting.clear()
    _compact_start_times.clear()
    yield _pending_compact, _self_compacting, _compact_start_times
    _pending_compact.clear()
    _pending_compact.update(saved[0])
    _self_compacting.clear()
    _self_compacting.update(saved[1])
    _compact_start_times.clear()
    _compact_start_times.update(saved[2])


def _session(name: str, *, tokens: int = 0, window: int = 100_000) -> AgentSession:
    s = AgentSession(name=name)
    s.context_tokens = tokens
    s.context_window = window
    return s


# ---------------------------------------------------------------------------
# Part 1 — hub stream factory threads the compaction dicts
# ---------------------------------------------------------------------------


def test_stream_factory_threads_compaction_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import hub_wiring
    from axi.discord_stream import _compact_start_times, _pending_compact, _self_compacting

    captured: dict[str, Any] = {}

    def fake_stream_response(session: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return iter(())

    monkeypatch.setattr("agenthub.streaming.stream_response", fake_stream_response)

    hub_wiring._stream_factory(_session("sf"), set_session_id_fn="SID", record_usage_fn="RU")

    assert captured["pending_compact"] is _pending_compact
    assert captured["self_compacting_names"] is _self_compacting
    assert captured["compact_start_times"] is _compact_start_times
    assert captured["set_session_id_fn"] == "SID"
    assert captured["record_usage_fn"] == "RU"


# ---------------------------------------------------------------------------
# Part 3 — _maybe_compact queues a /compact hub turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_compact_queues_compact_turn(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi import agents

    _pending, _selfc, _starts = clean_compaction_state
    hub = _FakeHub()
    router = _FakeRouter()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: router)

    session = _session("mc", tokens=90_000)  # 0.90 >= 0.80 threshold
    session.compact_instructions = "keep the key facts"

    await agents._maybe_compact(session)

    assert len(hub.submitted) == 1
    name, content, metadata = hub.submitted[0]
    assert name == "mc"
    assert content == "/compact keep the key facts"
    assert metadata == {"compaction": True}
    assert "mc" in _selfc  # marked self-triggered
    assert "mc" in _starts
    assert any("compacting" in p for p in router.posts)


@pytest.mark.asyncio
async def test_maybe_compact_under_threshold_is_noop(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi import agents

    hub = _FakeHub()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: _FakeRouter())

    await agents._maybe_compact(_session("lo", tokens=10_000))  # 0.10 < 0.80

    assert hub.submitted == []


@pytest.mark.asyncio
async def test_maybe_compact_without_hub_is_noop(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi import agents

    monkeypatch.setattr("axi.agents.hub", None)
    monkeypatch.setattr("axi.agents._get_router", lambda: _FakeRouter())

    # Must not raise even though it's over threshold.
    await agents._maybe_compact(_session("nh", tokens=95_000))


# ---------------------------------------------------------------------------
# Part 2 — _handle_pending_compact posts summary + queues auto-resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_pending_compact_posts_and_queues_resume(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi import agents

    _pending, _selfc, _starts = clean_compaction_state
    hub = _FakeHub()
    router = _FakeRouter()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: router)
    monkeypatch.setattr("axi.agents._reset_session_activity", lambda s: None)

    session = _session("pc", tokens=40_000)
    _pending["pc"] = {"pre_tokens": 90_000, "start_time": 0.0}

    resumed = await agents._handle_pending_compact(session)

    assert resumed is True
    assert "pc" not in _pending  # popped
    assert len(hub.submitted) == 1
    name, content, metadata = hub.submitted[0]
    assert name == "pc"
    assert content == "Continue from where you left off."
    assert metadata == {"auto_resume": True}
    assert any("Compacted" in p for p in router.posts)


@pytest.mark.asyncio
async def test_handle_pending_compact_no_pending_returns_false(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi import agents

    hub = _FakeHub()
    router = _FakeRouter()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: router)

    resumed = await agents._handle_pending_compact(_session("np"))

    assert resumed is False
    assert hub.submitted == []
    assert router.posts == []


# ---------------------------------------------------------------------------
# after_turn orchestration — clears flag + guards proactive compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_turn_compaction_turn_clears_flag_and_skips_proactive(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi.turn_hooks import AxiTurnHooks

    _pending, _selfc, _starts = clean_compaction_state
    _selfc.add("ct")

    proactive_calls: list[str] = []

    async def fake_maybe_compact(session: Any, channel: Any = None) -> None:
        proactive_calls.append(session.name)

    async def fake_handle_pending(session: Any) -> bool:
        return False

    monkeypatch.setattr("axi.agents._maybe_compact", fake_maybe_compact)
    monkeypatch.setattr("axi.agents._handle_pending_compact", fake_handle_pending)

    turn = types.SimpleNamespace(metadata={"compaction": True})
    await AxiTurnHooks().after_turn(_session("ct"), turn, TurnOutcome.COMPLETED)

    assert "ct" not in _selfc  # flag cleared after the /compact turn
    assert proactive_calls == []  # proactive compaction skipped on a compaction turn


@pytest.mark.asyncio
async def test_after_turn_skips_proactive_when_resume_queued(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi.turn_hooks import AxiTurnHooks

    proactive_calls: list[str] = []

    async def fake_maybe_compact(session: Any, channel: Any = None) -> None:
        proactive_calls.append(session.name)

    async def fake_handle_pending(session: Any) -> bool:
        return True  # an auto-resume was queued this turn

    monkeypatch.setattr("axi.agents._maybe_compact", fake_maybe_compact)
    monkeypatch.setattr("axi.agents._handle_pending_compact", fake_handle_pending)

    turn = types.SimpleNamespace(metadata={})
    await AxiTurnHooks().after_turn(_session("rq"), turn, TurnOutcome.COMPLETED)

    assert proactive_calls == []  # skipped because a resume was just queued


@pytest.mark.asyncio
async def test_after_turn_normal_turn_runs_proactive_compaction(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    from axi.turn_hooks import AxiTurnHooks

    proactive_calls: list[str] = []

    async def fake_maybe_compact(session: Any, channel: Any = None) -> None:
        proactive_calls.append(session.name)

    async def fake_handle_pending(session: Any) -> bool:
        return False

    monkeypatch.setattr("axi.agents._maybe_compact", fake_maybe_compact)
    monkeypatch.setattr("axi.agents._handle_pending_compact", fake_handle_pending)

    turn = types.SimpleNamespace(metadata={})
    await AxiTurnHooks().after_turn(_session("nt"), turn, TurnOutcome.COMPLETED)

    assert proactive_calls == ["nt"]  # normal turn -> proactive compaction runs


# ---------------------------------------------------------------------------
# Part 1 (wiring) — create_hub actually passes _stream_factory to the AgentHub
# ---------------------------------------------------------------------------


def test_create_hub_wires_stream_factory() -> None:
    """The hub must USE _stream_factory (not just define it) — otherwise a CLI
    auto-compaction in a hub turn never records _pending_compact and the whole
    auto-resume fix is dead. A unit test that calls _stream_factory directly would
    not catch a missing `stream_factory=` in the AgentHub constructor.
    """
    from axi import hub_wiring

    hub = hub_wiring.create_hub(MagicMock(), {})
    assert hub.stream_factory is hub_wiring._stream_factory


# ---------------------------------------------------------------------------
# End-to-end — a compaction during a real hub turn queues AND DRIVES the resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_drives_queued_resume_after_compaction(
    monkeypatch: pytest.MonkeyPatch, clean_compaction_state: Any
) -> None:
    """Re-entrancy proof: after_turn submits 'Continue...' *mid-turn* (current_turn still
    set). Verify the real hub then drains + DRIVES that queued turn — the fake client
    receives a second query. This is the whole reason for the queued design; a mocked-hub
    unit test cannot prove the follow-up turn actually runs (no deadlock / no drop).
    """
    from axi.turn_hooks import AxiTurnHooks

    _pending, _selfc, _starts = clean_compaction_state

    # Stub before_turn side-effects that need real session internals + router/hub globals.
    monkeypatch.setattr("axi.agents._reset_session_activity", lambda s: None)
    monkeypatch.setattr("axi.agents.drain_stderr", lambda s: [])
    monkeypatch.setattr("axi.agents.drain_sdk_buffer", lambda s: 0)
    monkeypatch.setattr("axi.agents._wrap_content_with_flowchart", lambda content, session: content)
    monkeypatch.setattr("axi.turn_hooks._channel_id_of", lambda s: 1)
    monkeypatch.setattr("axi.agents._get_router", lambda: _FakeRouter())

    box: dict[str, _FakeClient] = {}
    compacted_once = {"done": False}

    async def stream(session: Any, **kwargs: Any) -> Any:
        # Simulate stream_response's compact_boundary handling on the FIRST turn only.
        if not compacted_once["done"]:
            compacted_once["done"] = True
            _pending[session.name] = {"pre_tokens": 90_000, "start_time": 0.0}
        yield StreamStart()
        yield QueryResult(session_id=f"sid-{session.name}", cost_usd=0.0, num_turns=1, duration_ms=1)
        yield StreamEnd(elapsed_s=0.0, msg_count=1, flush_count=0)

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
        make_agent_options=lambda session, session_id: {},
        max_awake=3,
        query_timeout=2.0,
        stream_factory=stream,
        turn_hooks=AxiTurnHooks(),
    )
    # _handle_pending_compact / _maybe_compact submit to the module-global hub.
    monkeypatch.setattr("axi.agents.hub", hub)

    await hub.spawn_agent(name="e2e", cwd=tempfile.mkdtemp(prefix="e2e_"))
    await hub.submit_user_message("e2e", "do the thing")

    # Let the turn task create the client, drive turn 1, then drain + drive the queued resume.
    for _ in range(150):
        await asyncio.sleep(0.02)
        c = box.get("e2e")
        if c is not None and len(c.queries) >= 2:
            break

    c = box.get("e2e")
    assert c is not None, "client was never created — turn did not drive"
    assert "Continue from where you left off." in c.queries, (
        f"resume turn never drove; client saw only: {c.queries!r}"
    )
    assert _pending.get("e2e") is None  # pending consumed, not left dangling
