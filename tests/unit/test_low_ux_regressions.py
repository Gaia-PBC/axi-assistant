"""Regression tests for the LOW-severity findings (R4, R13, R14).

R4 — double @user ping on the procmux reconnect-drain path. The audit flagged
this UNCONFIRMED. It is real, and provable without a live reconnect:
_drain_inflight_stream broadcasts every event to the router (so StreamEnd
reaches DiscordStreamRenderer._on_stream_end, which posts the ping) and then
posted mentions again itself. Removing the second one also means this path
inherits the renderer's error-path suppression from 41df8a5, which it never had.

Also fixed alongside: _drain_inflight_stream called drain_stderr() and threw the
result away *before* broadcasting, which defeated b2cf244 — the frontend drains
and posts stderr under /debug, but only if something else has not already
emptied the buffer.

R13 — the renderer saved todos to disk but left discord_state(session).todo_items
stale, so main.py rendered an out-of-date list until the next wake reloaded it.

R14 — a CompactComplete with no pre_tokens was dropped entirely. With pre_tokens
the hub defers to AxiTurnHooks for a richer summary; without them nothing was
recorded and nothing was ever posted.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    CompactComplete,
    StreamEnd,
    StreamKilled,
    StreamStart,
    TodoUpdate,
)


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.sent: list[str] = []

    async def send(self, content: str) -> Any:
        self.sent.append(content)
        return None


def _renderer(channel: _FakeChannel) -> Any:
    from axi.discord_stream_renderer import DiscordStreamRenderer

    return DiscordStreamRenderer("agent", channel, None, streaming_enabled=False)


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append(text)

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


# ---------------------------------------------------------------------------
# R4 — exactly one ping per drained stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_drain_pings_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The renderer owns the ping; the drain loop must not post a second one."""
    from axi import agents as agents_mod

    pings: list[str] = []
    channel = _FakeChannel()
    renderer = _renderer(channel)

    class _Router:
        async def on_stream_event(self, name: str, event: Any) -> None:
            await renderer.handle(event)

        async def post_message(self, name: str, text: str) -> None:
            pings.append(text)

    async def _stream(session: Any, **_kw: Any):
        yield StreamStart()
        yield StreamEnd()

    monkeypatch.setattr(agents_mod, "_get_router", lambda: _Router())
    monkeypatch.setattr("agenthub.streaming.stream_response", _stream)
    monkeypatch.setattr(agents_mod, "drain_stderr", lambda s: [])

    class _Session:
        name = "agent"
        frontend_state = None

    await agents_mod._drain_inflight_stream(_Session())  # type: ignore[arg-type]

    total = len(pings) + sum(1 for m in channel.sent if "<@" in m)
    assert total == 1, f"expected one ping, got {total} (router={pings}, channel={channel.sent})"
    await renderer.stop_typing()


@pytest.mark.asyncio
async def test_killed_stream_still_pings_once_and_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from axi import agents as agents_mod

    slept: list[bool] = []
    channel = _FakeChannel()
    renderer = _renderer(channel)

    class _Router:
        async def on_stream_event(self, name: str, event: Any) -> None:
            await renderer.handle(event)

        async def post_message(self, name: str, text: str) -> None:
            channel.sent.append(text)

    async def _stream(session: Any, **_kw: Any):
        yield StreamStart()
        yield StreamKilled()
        yield StreamEnd()

    async def _sleep(session: Any, force: bool = False) -> None:
        slept.append(force)

    monkeypatch.setattr(agents_mod, "_get_router", lambda: _Router())
    monkeypatch.setattr("agenthub.streaming.stream_response", _stream)
    monkeypatch.setattr(agents_mod, "drain_stderr", lambda s: [])
    monkeypatch.setattr(agents_mod, "sleep_agent", _sleep)

    class _Session:
        name = "agent"
        frontend_state = None

    await agents_mod._drain_inflight_stream(_Session())  # type: ignore[arg-type]

    assert sum(1 for m in channel.sent if "<@" in m) == 1
    assert slept == [True], "a killed stream must still force-sleep the agent"
    await renderer.stop_typing()


@pytest.mark.asyncio
async def test_stderr_survives_to_the_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    """b2cf244 posts stderr from the frontend, but only if nothing drained it first.

    The drain loop used to call drain_stderr() and discard the result *before*
    broadcasting, so on this path /debug output stayed invisible even after the
    frontend learned to post it.
    """
    from axi import agents as agents_mod
    from axi.axi_types import discord_state

    class _Session:
        name = "agent"
        frontend_state = None

    session = _Session()
    discord_state(session).stderr_buffer.append("cli warning")  # type: ignore[arg-type]

    seen_buffers: list[list[str]] = []

    class _Router:
        async def on_stream_event(self, name: str, event: Any) -> None:
            seen_buffers.append(list(discord_state(session).stderr_buffer))  # type: ignore[arg-type]

        async def post_message(self, name: str, text: str) -> None:
            pass

    async def _stream(s: Any, **_kw: Any):
        yield StreamStart()
        yield StreamEnd()

    monkeypatch.setattr(agents_mod, "_get_router", lambda: _Router())
    monkeypatch.setattr("agenthub.streaming.stream_response", _stream)

    await agents_mod._drain_inflight_stream(session)  # type: ignore[arg-type]

    assert seen_buffers[0] == ["cli warning"], (
        "the buffer was emptied before the frontend saw the event"
    )


# ---------------------------------------------------------------------------
# R13 — in-memory todos kept in step with disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_todo_update_refreshes_in_memory_copy(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from axi import agents as agents_mod
    from axi.axi_types import discord_state

    class _Session:
        frontend_state = None

    session = _Session()
    discord_state(session).todo_items = [{"content": "stale", "status": "pending"}]  # type: ignore[arg-type]
    monkeypatch.setitem(agents_mod.agents, "agent", session)  # type: ignore[arg-type]
    monkeypatch.setattr("axi.discord_ui._save_todo_items", lambda *a: None)

    todos = [{"content": "fresh", "status": "in_progress"}]
    await _renderer(_FakeChannel()).handle(TodoUpdate(todos=todos))

    assert discord_state(session).todo_items == todos  # type: ignore[arg-type]
    assert any("fresh" in m for m in posted)


@pytest.mark.asyncio
async def test_todo_update_for_unknown_agent_does_not_raise(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("axi.discord_ui._save_todo_items", lambda *a: None)

    await _renderer(_FakeChannel()).handle(TodoUpdate(todos=[{"content": "x"}]))

    assert posted, "the list should still be rendered even with no session"


# ---------------------------------------------------------------------------
# R14 — compaction with no pre_tokens is announced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_without_pre_tokens_is_announced(posted: list[str]) -> None:
    await _renderer(_FakeChannel()).handle(CompactComplete(pre_tokens=0, trigger="cli"))

    assert any("compacted" in m.lower() for m in posted)


@pytest.mark.asyncio
async def test_compact_with_pre_tokens_defers_to_turn_hooks(posted: list[str]) -> None:
    """AxiTurnHooks posts the richer summary; a second message here would duplicate it."""
    await _renderer(_FakeChannel()).handle(
        CompactComplete(pre_tokens=120_000, trigger="cli")
    )

    assert posted == []
