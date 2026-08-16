"""Unit tests for the frontend-agnostic streaming engine (agenthub/streaming.py).

Tests the transformation from raw SDK messages to StreamOutput events
without any Discord/frontend dependency.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
)
from claudewire.events import ActivityState
from hypothesis import given
from hypothesis import strategies as st

from agenthub.stream_types import (
    BlockStart,
    CompactComplete,
    CompactStart,
    FlowchartEnd,
    FlowchartStart,
    QueryResult,
    RateLimitHit,
    SessionId,
    SpawnEnd,
    SpawnStart,
    StreamEnd,
    StreamKilled,
    StreamOutput,
    StreamStart,
    TextDelta,
    TextFlush,
    ThinkingEnd,
    ThinkingStart,
    TodoUpdate,
    ToolInputDelta,
    ToolUseEnd,
    ToolUseStart,
    TransientError,
)
from agenthub.streaming import _extract_tool_preview, stream_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    """Minimal AgentSession stand-in for testing."""

    name: str = "test-agent"
    client: Any = None
    session_id: str | None = None
    activity: ActivityState = field(default_factory=ActivityState)
    agent_log: Any = None
    transport: Any = None
    context_tokens: int = 0
    context_window: int = 0
    compact_instructions: str | None = None
    cwd: str = ""


def _se(event: dict[str, Any], sid: str | None = None) -> StreamEvent:
    """Make a StreamEvent."""
    return StreamEvent(uuid="u", session_id=sid or "", event=event)


def _result(sid: str = "s1", cost: float = 0.01) -> ResultMessage:
    """Make a ResultMessage."""
    return ResultMessage(
        subtype="result", duration_ms=500, duration_api_ms=500,
        is_error=False, num_turns=1, session_id=sid, total_cost_usd=cost,
    )


def _assistant(error: str | None = None, text: str = "") -> AssistantMessage:
    """Make an AssistantMessage."""
    content = [TextBlock(text=text)] if text else []
    return AssistantMessage(content=content, model="test", error=error)  # type: ignore[arg-type]


def _system(subtype: str, data: dict[str, Any] | None = None) -> SystemMessage:
    """Make a SystemMessage."""
    return SystemMessage(subtype=subtype, data=data or {})


async def _collect(session: Any, messages: list[Any], **kwargs: Any) -> list[StreamOutput]:
    """Run stream_response with mocked receive and collect all outputs."""
    with patch("agenthub.streaming.receive_response_safe") as mock_recv:
        async def _gen(s: Any):  # type: ignore[no-untyped-def]
            for m in messages:
                yield m
        mock_recv.return_value = _gen(session)
        return [event async for event in stream_response(session, **kwargs)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamResponseBasic:
    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        events = await _collect(FakeSession(), [])
        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], StreamEnd)

    @pytest.mark.asyncio
    async def test_text_delta(self) -> None:
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(deltas) == 1
        assert deltas[0].text == "Hello"

    @pytest.mark.asyncio
    async def test_end_turn_flush(self) -> None:
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello world"}}),
            _se({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert len(flushes) >= 1
        assert "Hello world" in flushes[0].text
        assert flushes[0].reason == "end_turn"

    @pytest.mark.asyncio
    async def test_query_result(self) -> None:
        messages = [_result("s1", 0.05)]
        events = await _collect(FakeSession(), messages)
        results = [e for e in events if isinstance(e, QueryResult)]
        assert len(results) == 1
        assert results[0].session_id == "s1"
        assert results[0].cost_usd == 0.05


class TestStreamResponseThinking:
    @pytest.mark.asyncio
    async def test_thinking_lifecycle(self) -> None:
        messages = [
            _se({"type": "content_block_start", "content_block": {"type": "thinking"}}),
            _se({"type": "content_block_start", "content_block": {"type": "text"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        assert any(isinstance(e, ThinkingStart) for e in events)
        assert any(isinstance(e, ThinkingEnd) for e in events)


class TestStreamResponseToolUse:
    @pytest.mark.asyncio
    async def test_tool_use_lifecycle(self) -> None:
        session = FakeSession()
        session.activity = ActivityState(phase="waiting", tool_name="Bash")
        messages = [
            _se({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}, "index": 0}),
            _se({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'}}),
            _se({"type": "content_block_stop"}),
            _result(),
        ]
        events = await _collect(session, messages)
        starts = [e for e in events if isinstance(e, ToolUseStart)]
        ends = [e for e in events if isinstance(e, ToolUseEnd)]
        assert len(starts) == 1
        assert starts[0].tool_name == "Bash"
        assert len(ends) == 1
        assert ends[0].preview == "ls"

    @pytest.mark.asyncio
    async def test_todo_write_extraction(self) -> None:
        session = FakeSession()
        session.activity = ActivityState(phase="waiting", tool_name="TodoWrite")
        todo_json = '{"todos":[{"content":"fix bug","status":"pending"}]}'
        messages = [
            _se({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "TodoWrite"}, "index": 0}),
            _se({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": todo_json}}),
            _se({"type": "content_block_stop"}),
            _result(),
        ]
        events = await _collect(session, messages)
        todos = [e for e in events if isinstance(e, TodoUpdate)]
        assert len(todos) == 1
        assert todos[0].todos[0]["content"] == "fix bug"


class TestStreamResponseErrors:
    @pytest.mark.asyncio
    async def test_rate_limit(self) -> None:
        messages = [_assistant(error="rate_limit", text="Rate limited")]
        events = await _collect(FakeSession(), messages)
        hits = [e for e in events if isinstance(e, RateLimitHit)]
        assert len(hits) == 1
        assert hits[0].error_type == "rate_limit"

    @pytest.mark.asyncio
    async def test_transient_error(self) -> None:
        messages = [_assistant(error="overloaded")]
        events = await _collect(FakeSession(), messages)
        errors = [e for e in events if isinstance(e, TransientError)]
        assert len(errors) == 1
        assert errors[0].error_type == "overloaded"

    @pytest.mark.asyncio
    async def test_stream_killed(self) -> None:
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}),
        ]
        events = await _collect(FakeSession(), messages)
        assert any(isinstance(e, StreamKilled) for e in events)
        # Partial text should be flushed
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert any("partial" in f.text for f in flushes)


class TestStreamResponseFlowchart:
    @pytest.mark.asyncio
    async def test_flowchart_block(self) -> None:
        messages = [
            _system("block_start", {"data": {"block_name": "build", "block_type": "action"}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        blocks = [e for e in events if isinstance(e, BlockStart)]
        assert len(blocks) == 1
        assert blocks[0].block_name == "build"

    @pytest.mark.asyncio
    async def test_flowchart_complete(self) -> None:
        messages = [
            _system("flowchart_complete", {"data": {"status": "completed", "duration_ms": 3000, "cost_usd": 0.1, "blocks_executed": 5}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        ends = [e for e in events if isinstance(e, FlowchartEnd)]
        assert len(ends) == 1
        assert ends[0].status == "completed"


class TestStreamResponseCompaction:
    @pytest.mark.asyncio
    async def test_compact_start(self) -> None:
        messages = [_system("status", {"status": "compacting"}), _result()]
        events = await _collect(FakeSession(), messages)
        assert any(isinstance(e, CompactStart) for e in events)


class TestExtractToolPreview:
    def test_bash_preview(self) -> None:
        assert _extract_tool_preview("Bash", '{"command": "ls -la"}') == "ls -la"

    def test_read_preview(self) -> None:
        assert _extract_tool_preview("Read", '{"file_path": "/foo/bar.py"}') == "/foo/bar.py"

    def test_grep_preview(self) -> None:
        result = _extract_tool_preview("Grep", '{"pattern": "foo", "path": "/src"}')
        assert result is not None
        assert "foo" in result

    def test_glob_preview(self) -> None:
        assert _extract_tool_preview("Glob", '{"pattern": "**/*.py"}') == "**/*.py"

    def test_partial_json_bash(self) -> None:
        assert _extract_tool_preview("Bash", '{"command": "git status') == "git status"

    def test_unknown_tool(self) -> None:
        assert _extract_tool_preview("Unknown", '{"foo": "bar"}') is None


class TestMidTurnSplit:
    @pytest.mark.asyncio
    async def test_large_text_gets_split(self) -> None:
        big_text = "x" * 2000
        messages = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": big_text}}),
            _result(),
        ]
        events = await _collect(FakeSession(), messages)
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert len(flushes) >= 1
        total_text = "".join(f.text for f in flushes)
        assert len(total_text) == 2000


_TEXT_CHUNKS = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=20)


class TestStreamProperties:
    @pytest.mark.asyncio
    @given(st.lists(_TEXT_CHUNKS, min_size=1, max_size=8), st.booleans())
    async def test_stream_shape_and_flush_count_invariants(self, chunks: list[str], include_result: bool) -> None:
        messages: list[Any] = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}})
            for chunk in chunks
        ]
        if include_result:
            messages.append(_result())

        events = await _collect(FakeSession(), messages)

        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], StreamEnd)
        flushes = [e for e in events if isinstance(e, TextFlush)]
        assert events[-1].flush_count == len(flushes)
        has_killed = any(isinstance(e, StreamKilled) for e in events)
        if include_result:
            assert has_killed is False
        else:
            assert has_killed is True

    @pytest.mark.asyncio
    @given(st.lists(_TEXT_CHUNKS, min_size=1, max_size=4))
    async def test_flowchart_result_does_not_emit_normal_session_id(self, chunks: list[str]) -> None:
        messages: list[Any] = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}, sid="flowchart")
            for chunk in chunks
        ]
        messages.append(_result("flowchart"))

        session = FakeSession(session_id="orig-session")
        events = await _collect(session, messages)

        assert session.session_id == "orig-session"
        results = [e for e in events if isinstance(e, QueryResult)]
        assert len(results) == 1
        assert results[0].is_flowchart is True

    @pytest.mark.asyncio
    @given(
        tool_name=st.sampled_from(["Bash", "Read", "TodoWrite"]),
        partials=st.lists(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=12), min_size=0, max_size=4),
        include_stop=st.booleans(),
        include_result=st.booleans(),
    )
    async def test_tool_use_grammar_preserves_start_end_and_terminal_shape(
        self,
        tool_name: str,
        partials: list[str],
        include_stop: bool,
        include_result: bool,
    ) -> None:
        session = FakeSession()
        session.activity = ActivityState(phase="waiting", tool_name=tool_name)
        messages: list[Any] = [
            _se({"type": "content_block_start", "content_block": {"type": "tool_use", "name": tool_name}, "index": 0}),
        ]
        messages.extend(
            _se({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": partial}})
            for partial in partials
        )
        if include_stop:
            messages.append(_se({"type": "content_block_stop"}))
        if include_result:
            messages.append(_result())

        events = await _collect(session, messages)

        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], StreamEnd)
        starts = [e for e in events if isinstance(e, ToolUseStart)]
        inputs = [e for e in events if isinstance(e, ToolInputDelta)]
        ends = [e for e in events if isinstance(e, ToolUseEnd)]
        kills = [e for e in events if isinstance(e, StreamKilled)]
        results = [e for e in events if isinstance(e, QueryResult)]

        assert len(starts) == 1
        assert starts[0].tool_name == tool_name
        assert [e.partial_json for e in inputs] == partials
        assert len(ends) == (1 if include_stop else 0)
        assert len(results) == (1 if include_result else 0)
        assert len(kills) == (0 if include_result else 1)
        if ends:
            assert ends[0].tool_name == tool_name

    @pytest.mark.asyncio
    @given(
        chunks=st.lists(_TEXT_CHUNKS, min_size=1, max_size=5),
        terminal=st.sampled_from(["result", "rate_limit", "transient", "killed"]),
    )
    async def test_terminal_event_grammar_preserves_exclusive_outcome(
        self,
        chunks: list[str],
        terminal: str,
    ) -> None:
        messages: list[Any] = [
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}})
            for chunk in chunks
        ]
        if terminal == "result":
            messages.append(_result())
        elif terminal == "rate_limit":
            messages.append(_assistant(error="rate_limit", text="retry after 7 seconds"))
        elif terminal == "transient":
            messages.append(_assistant(error="overloaded", text="server busy"))

        events = await _collect(FakeSession(), messages)

        assert isinstance(events[0], StreamStart)
        assert isinstance(events[-1], StreamEnd)
        kills = [e for e in events if isinstance(e, StreamKilled)]
        results = [e for e in events if isinstance(e, QueryResult)]
        rate_limits = [e for e in events if isinstance(e, RateLimitHit)]
        transient_errors = [e for e in events if isinstance(e, TransientError)]
        flushes = [e for e in events if isinstance(e, TextFlush)]

        if terminal == "result":
            assert len(results) == 1
            assert not kills
            assert not rate_limits
            assert not transient_errors
        elif terminal == "rate_limit":
            assert len(rate_limits) == 1
            assert not kills
            assert not results
            assert not transient_errors
            # a terminal flush exists iff there was non-whitespace buffered text to flush;
            # the outcome itself is carried by the RateLimitHit event (asserted above), not the flush.
            assert (any(f.reason == "rate_limit" for f in flushes)) == bool("".join(chunks).strip())
        elif terminal == "transient":
            assert len(transient_errors) == 1
            assert not kills
            assert not results
            assert not rate_limits
            assert (any(f.reason in {"assistant_error", "transient_error"} for f in flushes)) == bool("".join(chunks).strip())
        else:
            assert len(kills) == 1
            assert not results
            assert not rate_limits
            assert not transient_errors
            assert (any(f.reason == "post_kill" for f in flushes)) == bool("".join(chunks).strip())


# ---------------------------------------------------------------------------
# Session-context seam (Task 2)
# ---------------------------------------------------------------------------


class _RawReceive:
    """Yields raw dicts like the transport's read_messages."""

    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


class _FakeQuery:
    def __init__(self, items):
        self._message_receive = _RawReceive(items)

    def receive_messages(self):
        return self._message_receive


def _raw_stream_event(uuid="u1", session="lint"):
    return {
        "type": "stream_event",
        "uuid": uuid,
        "session_id": "child-session-1",
        "event": {"type": "message_start"},
        "_session_context": {"session": session, "block_id": "b1", "block_name": "B"},
    }


def _raw_result():
    return {
        "type": "result",
        "subtype": "success",
        "uuid": "u2",
        "session_id": "flowchart",
        "duration_ms": 1,
        "duration_api_ms": 0,
        "is_error": False,
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": "done",
    }


@pytest.mark.asyncio
async def test_receive_response_safe_attaches_session_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_stream_event(), _raw_result()])))
    got = []
    async for parsed in streaming_mod.receive_response_safe(session):
        got.append(parsed)
    # The stream_event carried the stamp; the result message did not (its raw
    # dict has no _session_context key), so it defaults to {}.
    assert getattr(got[0], "_session_context", None) == {
        "session": "lint", "block_id": "b1", "block_name": "B",
    }
    assert getattr(got[1], "_session_context", None) == {}


@pytest.mark.asyncio
async def test_receive_response_safe_defaults_context_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event()
    raw.pop("_session_context")
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery([raw, _raw_result()])))
    async for parsed in streaming_mod.receive_response_safe(session):
        assert getattr(parsed, "_session_context", {}) == {}


# ---------------------------------------------------------------------------
# Per-session streaming (Task 3): session tagging + spawn events
# ---------------------------------------------------------------------------


def _raw_block_start(session: str) -> dict:
    return {
        "type": "system",
        "subtype": "block_start",
        "data": {"block_id": "b2", "block_name": "Prompt", "block_type": "prompt",
                 "session": session},
    }


def _raw_spawn_start() -> dict:
    return {
        "type": "system",
        "subtype": "spawn_start",
        "data": {"agent_name": "lint", "command_name": "lint-fix",
                 "model": "opus", "backend": "claude",
                 "cwd": "/tmp", "parent_session": "main"},
    }


def _raw_spawn_complete(status: str = "completed") -> dict:
    return {
        "type": "system",
        "subtype": "spawn_complete",
        "data": {"agent_name": "lint", "status": status,
                 "duration_ms": 1234, "cost_usd": 0.042, "result": "{}"},
    }


@pytest.mark.asyncio
async def test_spawn_events_are_yielded(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_spawn_start(), _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    spawns = [e for e in events if isinstance(e, (SpawnStart, SpawnEnd))]
    assert len(spawns) == 2
    assert isinstance(spawns[0], SpawnStart) and spawns[0].agent_name == "lint"
    assert spawns[0].parent_session == "main"
    assert isinstance(spawns[1], SpawnEnd) and spawns[1].status == "completed"
    assert spawns[1].duration_ms == 1234


@pytest.mark.asyncio
async def test_child_block_start_tagged_and_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_block_start("lint"), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    blocks = [e for e in events if isinstance(e, BlockStart)]
    assert len(blocks) == 1 and blocks[0].session == "lint"


@pytest.mark.asyncio
async def test_child_text_flushed_on_spawn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")  # text delta, see Task 2 helper
    raw["event"] = {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "child says hi"}}
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [raw, _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    flushes = [e for e in events if isinstance(e, TextFlush) and e.session == "lint"]
    assert flushes and flushes[-1].reason == "spawn_complete"
    assert "child says hi" in flushes[-1].text


@pytest.mark.asyncio
async def test_child_flowchart_events_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_block_start("lint")
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_spawn_start(), raw, _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    blocks = [e for e in events if isinstance(e, BlockStart)]
    assert len(blocks) == 1 and blocks[0].session == "lint"

    # The child's flowchart lifecycle is also session-tagged.
    fc_start = {"type": "system", "subtype": "flowchart_start",
                "data": {"command": "lint-fix", "block_count": 2, "session": "lint"}}
    fc_complete = {"type": "system", "subtype": "flowchart_complete",
                   "data": {"status": "completed", "duration_ms": 10,
                            "cost_usd": 0.0, "blocks_executed": 2, "session": "lint"}}
    session2 = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_spawn_start(), fc_start, fc_complete, _raw_spawn_complete(), _raw_result()])))
    events2 = [e async for e in streaming_mod.stream_response(session2)]
    starts = [e for e in events2 if isinstance(e, FlowchartStart)]
    ends = [e for e in events2 if isinstance(e, FlowchartEnd)]
    assert len(starts) == 1 and starts[0].session == "lint"
    assert len(ends) == 1 and ends[0].session == "lint"


@pytest.mark.asyncio
async def test_child_events_do_not_mutate_parent_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")
    raw["event"] = {"type": "content_block_start",
                    "content_block": {"type": "tool_use", "name": "Bash", "id": "t1"}}
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([raw, _raw_result()])),
        activity=types.SimpleNamespace(phase="idle", tool_name=None, query_started=None),
    )
    events = [e async for e in streaming_mod.stream_response(session)]
    assert session.activity.phase == "idle", "child tool use must not touch parent activity"
    assert session.activity.tool_name is None


@pytest.mark.asyncio
async def test_child_tool_use_end_emitted_and_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child's tool_use start/delta/stop yields a tagged ToolUseEnd even with the
    parent in 'writing' phase — the child path must not read session.activity."""
    from agenthub import streaming as streaming_mod

    start = _raw_stream_event(session="lint")
    start["event"] = {"type": "content_block_start",
                      "content_block": {"type": "tool_use", "name": "Bash", "id": "t9"},
                      "index": 0}
    delta = _raw_stream_event(session="lint", uuid="u2")
    delta["event"] = {"type": "content_block_delta",
                      "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'}}
    stop = _raw_stream_event(session="lint", uuid="u3")
    stop["event"] = {"type": "content_block_stop"}
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([start, delta, stop, _raw_result()])),
        activity=types.SimpleNamespace(phase="writing", tool_name="Read", query_started=None),
    )
    events = [e async for e in streaming_mod.stream_response(session)]
    ends = [e for e in events if isinstance(e, ToolUseEnd)]
    assert len(ends) == 1, "child ToolUseEnd must not be dropped by parent phase"
    assert ends[0].session == "lint"
    assert ends[0].tool_name == "Bash"
    assert ends[0].preview == "ls"


@pytest.mark.asyncio
async def test_child_thinking_text_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child's thinking deltas accumulate per-session; its ThinkingEnd carries the
    child's text, never the parent's live session.activity.thinking_text."""
    from agenthub import streaming as streaming_mod

    start = _raw_stream_event(session="lint")
    start["event"] = {"type": "content_block_start",
                      "content_block": {"type": "thinking", "thinking": ""}}
    d1 = _raw_stream_event(session="lint", uuid="u2")
    d1["event"] = {"type": "content_block_delta",
                   "delta": {"type": "thinking_delta", "thinking": "child thinks "}}
    d2 = _raw_stream_event(session="lint", uuid="u3")
    d2["event"] = {"type": "content_block_delta",
                   "delta": {"type": "thinking_delta", "thinking": "deeply"}}
    stop = _raw_stream_event(session="lint", uuid="u4")
    stop["event"] = {"type": "content_block_start",
                     "content_block": {"type": "text"}}
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([start, d1, d2, stop, _raw_result()])),
        activity=types.SimpleNamespace(phase="idle", tool_name=None, thinking_text="PARENT THINKING"),
    )
    events = [e async for e in streaming_mod.stream_response(session)]
    ends = [e for e in events if isinstance(e, ThinkingEnd)]
    assert len(ends) == 1
    assert ends[0].session == "lint"
    assert ends[0].thinking_text == "child thinks deeply"
    # The child's thinking must never leak the parent's activity value.
    assert "PARENT" not in ends[0].thinking_text
    # Parent activity must be untouched by the child's thinking events.
    assert session.activity.phase == "idle"
    assert session.activity.thinking_text == "PARENT THINKING"


# ---------------------------------------------------------------------------
# Final-review fix wave: 'main'-stamped parent, never-drop child flushes,
# spawn_complete ordering
# ---------------------------------------------------------------------------


def _raw_stream_event_main(event: dict[str, Any], uuid: str = "u1") -> dict:
    """A 'main'-stamped stream event: the transport stamps EVERY forwarded
    inner message — including the main session's own — with session='main'."""
    raw = _raw_stream_event(session="main", uuid=uuid)
    raw["event"] = event
    return raw


@pytest.mark.asyncio
async def test_main_stamped_parent_events_are_not_child_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: a session='main'-stamped parent stream must behave as the parent.

    The flowcoder transport stamps every forwarded inner message with
    session='main' (the main session's own turn included). _msg_session must
    normalize that to '' so the parent's turn takes the parent branches:
    activity updates, SessionId emission, and compaction handling all live on
    the parent path.
    """
    from agenthub import streaming as streaming_mod

    # A tool-use block + compact status, both stamped session='main' — exactly
    # what the transport forwards for the parent's own turn. The status message
    # uses the real CLI wire shape ('status' at the top level of the raw dict,
    # which SystemMessage.data preserves verbatim).
    tool_start = _raw_stream_event_main(
        {"type": "content_block_start",
         "content_block": {"type": "tool_use", "name": "Bash", "id": "t1"}, "index": 0},
        uuid="u1",
    )
    compact = {
        "type": "system",
        "subtype": "status",
        "status": "compacting",
        "session_id": "sess-1",
        "uuid": "u2",
        "_session_context": {"session": "main", "block_id": "", "block_name": ""},
    }
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([tool_start, compact, _raw_result()])),
        activity=ActivityState(),
        session_id="orig-session",
        compacting=False,
        name="test-agent",
        context_tokens=100,
    )

    events = [e async for e in streaming_mod.stream_response(session)]

    # Parent branch: activity reflects the parent's tool use...
    assert session.activity.phase == "tool_use"
    assert session.activity.tool_name == "Bash"
    # ...the child gate must not swallow the parent's compaction status...
    compacts = [e for e in events if isinstance(e, CompactStart)]
    assert len(compacts) == 1, "'main'-stamped parent compaction must reach the parent handler"
    # ...and the parent's turn must not be counted as a child message: all
    # three messages (tool start, compact status, result) are parent-stamped.
    assert events[-1].msg_count == 3, "all parent messages counted for the parent"


@pytest.mark.asyncio
async def test_main_stamped_session_id_events_are_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: SessionId emission must not vanish for 'main'-stamped parent messages.

    _handle_stream_event's session-id gate checks `if not session_tag`; a
    'main' session_tag made mid-turn SessionId events disappear.
    """
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event_main(
        {"type": "message_start"}, uuid="u1",
    )
    raw["session_id"] = "new-session-42"
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([raw, _raw_result()])),
        activity=ActivityState(),
        session_id="orig-session",
        compacting=False,
        name="test-agent",
        context_tokens=0,
    )
    events = [e async for e in streaming_mod.stream_response(session)]
    ids = [e for e in events if isinstance(e, SessionId)]
    assert len(ids) == 1, "SessionId must be emitted for a 'main'-stamped parent message"
    assert ids[0].session_id == "new-session-42"


@pytest.mark.asyncio
async def test_main_stamped_parent_compaction_flags_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: 'main'-stamped parent compaction must set/clear session.compacting.

    The handler gate `if session_tag and msg.subtype in ('status',
    'compact_boundary'): return` swallowed the parent's own compact lifecycle
    when the transport stamped it session='main' — the compacting flag and
    pending-compact auto-resume were lost.
    """
    from agenthub import streaming as streaming_mod

    compact_start = {
        "type": "system",
        "subtype": "status",
        "status": "compacting",
        "session_id": "sess-1",
        "uuid": "u1",
        "_session_context": {"session": "main", "block_id": "", "block_name": ""},
    }
    compact_end = {
        "type": "system",
        "subtype": "compact_boundary",
        "session_id": "sess-1",
        "uuid": "u2",
        "compact_metadata": {"trigger": "auto", "pre_tokens": 512},
        "_session_context": {"session": "main", "block_id": "", "block_name": ""},
    }
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([compact_start, compact_end, _raw_result()])),
        activity=ActivityState(),
        session_id="orig-session",
        compacting=False,
        name="test-agent",
        context_tokens=100,
    )
    events = [e async for e in streaming_mod.stream_response(session)]

    assert session.compacting is False, "compact_boundary must clear the compacting flag"
    assert any(isinstance(e, CompactStart) for e in events), "CompactStart must be yielded"
    assert any(isinstance(e, CompactComplete) for e in events), "CompactComplete must be yielded"


@pytest.mark.asyncio
async def test_child_residual_text_flushed_post_kill_on_hard_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: never-drop — a child's buffered text must flush when the stream ends
    without a result (hard kill / StreamKilled / engine teardown)."""
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")
    raw["event"] = {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "orphaned child tail"}}
    # No spawn_complete, no result: the stream dies with the child mid-turn.
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery([raw])))
    events = [e async for e in streaming_mod.stream_response(session)]

    killed = [e for e in events if isinstance(e, StreamKilled)]
    assert len(killed) == 1, "parent still reports StreamKilled"
    child_flushes = [e for e in events if isinstance(e, TextFlush) and e.session == "lint"]
    assert len(child_flushes) == 1, "child residual buffer must be flushed exactly once"
    assert child_flushes[0].reason == "post_kill"
    assert "orphaned child tail" in child_flushes[0].text


@pytest.mark.asyncio
async def test_child_residual_text_flushed_post_kill_with_parent_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: never-drop — a child's residual buffer survives a parent result
    (spawn_complete lost to engine teardown); the child flush precedes the
    parent's own terminal flush."""
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")
    raw["event"] = {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "tail before teardown"}}
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [raw, _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]

    flushes = [e for e in events if isinstance(e, TextFlush)]
    child_flushes = [f for f in flushes if f.session == "lint"]
    assert len(child_flushes) == 1
    assert child_flushes[0].reason == "post_kill"
    assert "tail before teardown" in child_flushes[0].text
    assert any(isinstance(e, QueryResult) for e in events), "parent result still delivered"


@pytest.mark.asyncio
async def test_spawn_complete_residual_flush_precedes_spawn_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3: the child's spawn_complete residual flush must be yielded BEFORE the
    SpawnEnd event — the renderer drains the child's deferred buffer when it
    sees SpawnEnd, so a flush arriving after would miss the thread mapping."""
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")
    raw["event"] = {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "final words"}}
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [raw, _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]

    spawn_ends = [e for e in events if isinstance(e, SpawnEnd)]
    assert len(spawn_ends) == 1
    spawn_idx = events.index(spawn_ends[0])
    flush_idx = next(
        i for i, e in enumerate(events)
        if isinstance(e, TextFlush) and e.session == "lint" and e.reason == "spawn_complete"
    )
    assert flush_idx < spawn_idx, (
        "spawn_complete residual flush must precede the SpawnEnd event"
    )
    assert "final words" in events[flush_idx].text
