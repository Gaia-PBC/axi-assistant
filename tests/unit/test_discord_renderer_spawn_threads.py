"""Renderer session-routing + spawn thread behavior."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    BlockStart,
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

    async def archive(self) -> None:
        self.archived = True


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.threads: list[_FakeThread] = []

    async def create_thread(self, *, name: str, auto_archive_duration: int) -> _FakeThread:
        t = _FakeThread(name)
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
        return self._threads.get(thread_id)


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


def _renderer(channel: _FakeChannel, bot: _FakeBot) -> DiscordStreamRenderer:
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
