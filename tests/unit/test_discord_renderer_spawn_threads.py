"""Renderer session-routing + spawn thread behavior."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

import discord

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    BlockStart,
    FlowchartEnd,
    FlowchartStart,
    QueryResult,
    SpawnEnd,
    SpawnStart,
    StreamEnd,
    TextFlush,
)
from agenthub.types import AgentSession
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeThread:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = 888
        self.jump_url = f"https://discord.com/channels/1/2/{self.id}"
        self.archived = False

    async def edit(self, *, archived: bool = False, **_: object) -> None:
        self.archived = archived


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.threads: list[_FakeThread] = []

    async def create_thread(
        self, *, name: str, auto_archive_duration: int, type: Any = None
    ) -> _FakeThread:
        t = _FakeThread(name)
        t.type = type
        self.threads.append(t)
        return t

    async def send(self, content: str) -> Any:
        # _on_stream_end pings the channel at stream close; capture nothing,
        # just keep the stream-end path exercised (matches block_suppression).
        return None


class _FakeBot:
    def __init__(self) -> None:
        self._threads: dict[int, _FakeThread] = {}

    def get_channel(self, thread_id: int) -> _FakeThread | None:
        # Real discord.py registers threads created via create_thread in the
        # bot's cache; mirror that by falling back to the channel's created
        # threads (wired by _renderer) when _threads misses.
        thread = self._threads.get(thread_id)
        if thread is not None:
            return thread
        channel = getattr(self, "_channel", None)
        if channel is not None:
            for t in channel.threads:
                if t.id == thread_id:
                    return t
        return None


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    """Capture audited_channel_send (channel-or-thread, text) — _send_system path."""
    captured: list[tuple[Any, str]] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append((channel, text))

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


@pytest.fixture
def flushed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    """Capture send_long (channel-or-thread, text) — assistant text path."""
    captured: list[tuple[Any, str]] = []

    async def _fake_send_long(channel: Any, text: str) -> Any:
        captured.append((channel, text))
        return None

    import axi.agents

    monkeypatch.setattr(axi.agents, "send_long", _fake_send_long)
    return captured


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> AgentSession:
    """Register a real AgentSession under 'agent' in the agents registry."""
    import axi.agents as agents_mod

    session = AgentSession(name="agent")
    monkeypatch.setitem(agents_mod.agents, "agent", session)
    return session


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_SPAWN_THREADS", "1")
    monkeypatch.setenv("FC_THREAD_GRACE_SECS", "0")  # instant archive for tests
    # config reads both env vars at import time, so setenv alone is a no-op for
    # the module attributes the renderer consults (see the FC_SPAWN_THREADS
    # comment in test_spawn_threads_disabled_...). Patch the attribute to make
    # the "instant archive" intent real.
    from axi import config

    monkeypatch.setattr(config, "FC_THREAD_GRACE_SECS", 0)


def _renderer(channel: _FakeChannel, bot: _FakeBot) -> DiscordStreamRenderer:
    # Mirror discord.py: threads created on a channel are discoverable via the
    # bot's get_channel (used by the archive paths).
    bot._channel = channel
    return DiscordStreamRenderer("agent", channel, bot, streaming_enabled=False)  # type: ignore[arg-type]


async def test_child_flush_routes_to_thread(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """A child-session TextFlush sends into the child's thread, not the channel."""
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    # Child text is deferred: each flush delivers the previous one, and the
    # final buffer drains at spawn_complete (see _on_spawn_end).
    await renderer.handle(TextFlush(text="first chunk", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="second chunk", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="", reason="end_turn", session="lint"))
    await renderer.handle(SpawnEnd(agent_name="lint", status="completed", session=""))

    assert any(t is thread and "first chunk" in text for t, text in flushed)
    assert any(t is thread and "second chunk" in text for t, text in flushed)
    assert not any(t is channel for t, _ in flushed), "no child text in the parent channel"


async def test_child_flush_without_thread_falls_back_prefixed(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """No thread recorded → prefixed into the parent channel, never dropped."""
    channel = _FakeChannel()
    renderer = _renderer(channel, _FakeBot())

    await renderer.handle(TextFlush(text="orphan output", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="", reason="end_turn", session="lint"))
    # Drain the deferred buffer through the spawn-end path (fallback target).
    await renderer.handle(SpawnEnd(agent_name="lint", status="completed", session=""))

    assert any(
        t is channel and "[lint]" in text and "orphan output" in text for t, text in flushed
    )


async def test_spawn_threads_disabled_matches_pre_feature_behavior(
    env, agent: AgentSession, flushed: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from axi import config

    # config.FC_SPAWN_THREADS is read at import time, so the env var alone
    # would not flip the flag (the plan's os.environ line was a no-op); patch
    # the module attribute the renderer actually consults.
    monkeypatch.setattr(config, "FC_SPAWN_THREADS", False)
    channel = _FakeChannel()
    renderer = _renderer(channel, _FakeBot())

    await renderer.handle(TextFlush(text="plain output", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="", reason="end_turn", session="lint"))
    # Drain the deferred buffer through the spawn-end path (channel target).
    await renderer.handle(SpawnEnd(agent_name="lint", status="completed", session=""))

    assert any(t is channel and "plain output" in text and "[lint]" not in text
               for t, text in flushed)


async def test_child_output_schema_suppression_is_per_session(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(BlockStart(
        block_name="Schema", block_type="prompt",
        has_output_schema=True, session="lint",
    ))
    # Parent text must NOT be suppressed by a child's output-schema block
    await renderer.handle(TextFlush(text="parent visible", reason="end_turn"))
    # Parent flushes are deferred and drain at stream end (matches every
    # other renderer suite's convention).
    await renderer.handle(StreamEnd())

    assert any(t is channel and "parent visible" in text for t, text in flushed)


# ---------------------------------------------------------------------------
# Fix round 1 regressions
# ---------------------------------------------------------------------------


async def test_stream_end_drains_child_deferred_without_spawn_end(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """A child stream ending without SpawnEnd still surfaces its last chunk.

    Hard kill / StreamKilled / error teardown never emit SpawnEnd, so the
    stream-end drain is the never-drop backstop for child buffers.
    """
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(TextFlush(text="final words", reason="mid_turn_split", session="lint"))
    # No SpawnEnd — the stream just dies.
    await renderer.handle(StreamEnd())

    assert any(t is thread and "final words" in text for t, text in flushed)
    assert renderer._child_deferred == {}, "buffer must be cleared after the drain"


async def test_stream_end_child_fallback_prefix_without_thread(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """Stream-end drain of a threadless child uses the [agent] fallback."""
    channel = _FakeChannel()
    renderer = _renderer(channel, _FakeBot())

    await renderer.handle(TextFlush(text="orphan tail", reason="mid_turn_split", session="lint"))
    await renderer.handle(StreamEnd())

    assert any(
        t is channel and "[lint]" in text and "orphan tail" in text for t, text in flushed
    )


async def test_child_drain_does_not_clobber_parent_last_flushed(
    env, agent: AgentSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_send_long(session=...) must not hijack the parent's timing-suffix edit.

    Regression: _send_long recorded _last_flushed_msg_id from a child's thread
    message but set _last_flushed_channel_id to the parent's — an inconsistent
    pair. If a child send is the last one before the parent's QueryResult, the
    suffix edit would target the parent channel with the child's message id
    (editing the wrong message) and then post a stray timing line. Parent-only
    bookkeeping fixes it.
    """
    from datetime import UTC, datetime, timedelta

    from axi.axi_types import discord_state

    # _elapsed_seconds measures from session.activity.query_started; seed it so
    # the QueryResult path produces a timing suffix (mirrors the timing suite).
    agent.activity = type(
        "Activity", (), {"query_started": datetime.now(UTC) - timedelta(seconds=1.5)}
    )()

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    # send_long returns distinguishable messages per channel so the edit's
    # target message id reveals whether a child send hijacked the bookkeeping.
    sent: list[tuple[Any, str]] = []

    class _Msg:
        def __init__(self, id: str, content: str) -> None:
            self.id = id
            self.content = content

    async def _fake_send_long(target: Any, text: str) -> _Msg:
        sent.append((target, text))
        return _Msg("child-msg" if getattr(target, "id", None) == 888 else "parent-msg", text)

    import axi.agents

    monkeypatch.setattr(axi.agents, "send_long", _fake_send_long)

    edits: list[tuple[int, str, str]] = []

    class _Client:
        async def edit_message(self, channel_id: int, message_id: str, content: str) -> None:
            edits.append((channel_id, message_id, content))

    from axi import config

    monkeypatch.setattr(config, "discord_client", _Client())

    # Parent text first: establishes the parent's last-flushed message.
    await renderer.handle(TextFlush(text="parent answer", reason="end_turn"))
    # A child send via _send_long (e.g. a child drain before StreamEnd) must
    # NOT overwrite the parent's last-flushed bookkeeping. It runs while the
    # child's thread is still open: StreamEnd would archive unfinished threads
    # and drop the spawn_threads mapping, falling back to the channel.
    await renderer._send_long("child tail", session="lint")
    await renderer.handle(StreamEnd())

    # The timing suffix must edit the PARENT's last message, not the child's.
    await renderer.handle(QueryResult(cost_usd=0.0, duration_ms=1500, session=""))

    assert any(t is thread and "child tail" in text for t, text in sent), (
        "child text still routes to its thread"
    )
    assert edits, "expected the QueryResult timing edit to run"
    assert all(mid == "parent-msg" for _, mid, _ in edits), (
        f"timing edit must target the parent's message id, got {edits}"
    )
    assert any("1.5s" in content for _, _, content in edits)


async def test_child_flowchart_events_are_isolated_from_parent(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """Child flowchart events must not clobber the parent's FC state.

    A child's FlowchartStart/End used to fall into the parent-only handlers,
    mutating _fc_command/_in_flowchart — so a parent block after the child's
    flowchart ran would inherit the child's quiet-command gating (or vice
    versa). The child's completion summary renders into its thread.
    """
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(FlowchartStart(command="soul", block_count=1, session="lint"))
    await renderer.handle(FlowchartEnd(
        status="completed", duration_ms=5000, cost_usd=0.1, blocks_executed=2, session="lint",
    ))

    assert renderer._fc_command is None, "child flowchart must not set the parent's command"
    assert renderer._in_flowchart is False, "child flowchart must not set the parent's flag"
    assert "lint" not in renderer._child_fc_command, "child FC command cleared at end"
    assert any(
        t is thread and "Flowchart **completed**" in text and "5s" in text for t, text in flushed
    ), "child completion summary routes into the child's thread"


async def test_child_flowchart_command_quiet_gates_child_blocks_only(
    env, agent: AgentSession, posted: list[tuple[Any, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child /soul flowchart quiet-gates the child's block lines, not the parent's."""
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    # Quiet-gate check is _child_fc_command in _FC_QUIET_COMMANDS and NOT verbose.
    from axi import config

    monkeypatch.setattr(config, "FC_SPAWN_THREADS", True)
    await renderer.handle(FlowchartStart(command="soul", block_count=1, session="lint"))
    await renderer.handle(BlockStart(
        block_name="CHILD", block_type="prompt", has_output_schema=False, session="lint",
    ))
    # Parent block lines are governed by the parent's own (unset) FC command.
    await renderer.handle(BlockStart(
        block_name="PARENT", block_type="prompt", has_output_schema=False,
    ))

    assert not any(t is thread and "CHILD" in text for t, text in posted), (
        "child block line suppressed by child quiet command"
    )
    assert any(t is channel and "PARENT" in text for t, text in posted), (
        "parent block line unaffected by the child's flowchart"
    )


# ---------------------------------------------------------------------------
# Task 7: spawn thread lifecycle (start/end, fallback, stream-end cleanup)
# ---------------------------------------------------------------------------


async def test_spawn_start_creates_thread_and_posts_status(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnStart(
        agent_name="lint", command_name="lint-fix", model="opus",
        backend="claude", parent_session="main",
    ))

    assert len(channel.threads) == 1
    thread = channel.threads[0]
    assert thread.name == "lint"
    assert thread.type == discord.ChannelType.public_thread, (
        "threads must be public — private threads are invisible to non-members"
    )
    assert discord_state(agent).spawn_threads == {"lint": thread.id}
    assert any(
        t is channel and "spawned" in text and "lint" in text for t, text in posted
    ), "parent status line"
    assert any(
        t is thread and "Spawned agent **lint**" in text for t, text in flushed
    ), "spawned line in thread"
    assert any(
        t is thread and "lint-fix" in text and "opus" in text for t, text in flushed
    ), "command/model line in thread"


async def test_nested_spawn_gets_ancestry_name(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    parent_thread = _FakeThread("lint")
    parent_thread.id = 888
    bot._threads[888] = parent_thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    # Task 3 contract: a nested spawn's event carries session == parent_session
    # (the emitting walker's session name), so the status line routes into the
    # parent's thread rather than the parent channel.
    await renderer.handle(SpawnStart(
        agent_name="fmt", command_name="fmt-do", parent_session="lint", session="lint",
    ))

    assert len(channel.threads) == 1
    assert channel.threads[0].name == "lint/fmt"
    assert any(
        t is parent_thread and "fmt" in text and "spawned" in text for t, text in posted
    ), "nested spawn status line routes into the parent's thread"


async def test_spawn_end_routes_status_to_emitting_session(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    """A nested spawn's completion line goes into the parent's thread."""
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    parent_thread = _FakeThread("lint")
    parent_thread.id = 888
    bot._threads[888] = parent_thread
    ds = discord_state(agent)
    ds.spawn_threads["lint"] = 888
    ds.spawn_threads["fmt"] = 889
    child_thread = _FakeThread("lint/fmt")
    child_thread.id = 889
    bot._threads[889] = child_thread
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnEnd(
        agent_name="fmt", status="completed", duration_ms=500,
        cost_usd=0.0, session="lint",
    ))

    assert any(
        t is parent_thread and "fmt" in text and "completed" in text for t, text in posted
    ), "completion line in the parent's thread"
    assert any(t is child_thread and "Spawn **completed**" in text for t, text in flushed)


async def test_spawn_end_posts_summary_and_archives(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnEnd(
        agent_name="lint", status="completed", duration_ms=1234,
        cost_usd=0.042, session="",
    ))

    assert any(t is thread and "Spawn **completed**" in text for t, text in flushed)
    assert any(t is thread and "1.2s" in text for t, text in flushed), "duration in summary"
    assert any(t is thread and "$0.0420" in text for t, text in flushed), "cost in summary"
    assert any(t is channel and "lint" in text and "completed" in text for t, text in posted)
    assert "lint" not in discord_state(agent).spawn_threads, "mapping removed"
    await asyncio.sleep(0.1)  # let the (0-grace) archive task run
    assert thread.archived


async def test_spawn_end_without_thread_is_noop(
    env, agent: AgentSession, posted: list[tuple[Any, str]]
) -> None:
    renderer = _renderer(_FakeChannel(), _FakeBot())
    await renderer.handle(SpawnEnd(agent_name="ghost", status="completed", session=""))
    assert posted == []


async def test_spawn_end_dead_thread_does_not_schedule_archive(
    env, agent: AgentSession, posted: list[tuple[Any, str]]
) -> None:
    """F4: when the spawn thread is unresolvable at spawn_end (thread deleted),
    the mapping is removed and the handler is done — no dangling grace-delay
    archive task is scheduled for a thread that cannot exist (design doc §9)."""
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    # spawn_threads still records the thread id, but the bot can no longer
    # resolve it (thread deleted mid-stream).
    discord_state(agent).spawn_threads["lint"] = 999
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnEnd(
        agent_name="lint", status="completed", duration_ms=1234,
        cost_usd=0.042, session="",
    ))

    assert "lint" not in discord_state(agent).spawn_threads, "mapping removed"
    assert "lint" not in discord_state(agent).pending_archives, (
        "no archive task scheduled for an unresolvable thread"
    )
    assert any(t is channel and "lint" in text for t, text in posted), (
        "status line still routes to the parent channel"
    )


async def test_stream_end_archives_unfinished_threads(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(StreamEnd(elapsed_s=1.0))

    assert thread.archived
    assert any(t is thread and "interrupted" in text for t, text in flushed)
    assert discord_state(agent).spawn_threads == {}
