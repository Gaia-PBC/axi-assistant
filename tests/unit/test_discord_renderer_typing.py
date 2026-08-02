"""Regression tests for the typing indicator lost in the hub refactor.

``DiscordStreamRenderer._on_stream_start`` used to do::

    self._typing_task = asyncio.create_task(self._channel.typing())

``Messageable.typing()`` is a plain sync method returning a
``discord.context_managers.Typing`` — a context manager, not a coroutine — so
``create_task`` raised ``TypeError: a coroutine was expected``. The surrounding
``except Exception`` logged it at DEBUG, so the indicator silently never
appeared. Awaiting the object instead would not have fixed it either: a bare
``await channel.typing()`` sends a single typing packet that Discord expires
after ~10s, whereas ``async with channel.typing()`` spawns the 5s refresh loop
that keeps it alive for the whole turn.

These tests pin: the task is a real coroutine task, it holds the context
manager open, it is torn down on the terminal stream events, and the frontend
can drop it while a gate blocks on the user.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    QueryResult,
    RateLimitHit,
    StreamKilled,
    StreamStart,
    TransientError,
)
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeTyping:
    """Stand-in for discord.context_managers.Typing.

    Mirrors the real class in the way that matters here: it is NOT a coroutine,
    it is an async context manager, and entering it is what starts the refresh
    loop.
    """

    def __init__(self, channel: _FakeChannel) -> None:
        self._channel = channel

    async def __aenter__(self) -> None:
        self._channel.enters += 1

    async def __aexit__(self, *exc: object) -> None:
        self._channel.exits += 1


class _FakeChannel:
    """Minimal TextChannel stand-in exposing the typing() context manager."""

    def __init__(self) -> None:
        self.id = 4242
        self.enters = 0
        self.exits = 0
        self.sent: list[str] = []

    def typing(self) -> _FakeTyping:
        return _FakeTyping(self)

    async def send(self, content: str) -> Any:
        self.sent.append(content)
        return None


def _renderer(channel: _FakeChannel) -> DiscordStreamRenderer:
    # streaming_enabled=False keeps live-edit state out of the way; these tests
    # only exercise the typing lifecycle.
    return DiscordStreamRenderer("agent", channel, None, streaming_enabled=False)  # type: ignore[arg-type]


async def _settle() -> None:
    """Yield enough for the typing task to reach __aenter__."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_typing_starts_and_holds_context_manager_open() -> None:
    """The old create_task(channel.typing()) raised TypeError; this must not."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(StreamStart())
    await _settle()

    assert renderer._typing_task is not None, "typing task was never created"
    assert not renderer._typing_task.done(), (
        f"typing task died immediately: {renderer._typing_task.exception()!r}"
    )
    # __aenter__ ran => the refresh loop is live, not a one-shot packet.
    assert channel.enters == 1
    assert channel.exits == 0

    await renderer.stop_typing()


@pytest.mark.asyncio
async def test_stop_typing_cancels_and_unwinds() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(StreamStart())
    await _settle()
    await renderer.stop_typing()

    assert renderer._typing_task is None
    assert channel.exits == 1, "context manager never unwound"

    # Idempotent: a second stop is a no-op, not an error.
    await renderer.stop_typing()
    assert channel.exits == 1


@pytest.mark.asyncio
async def test_start_typing_is_idempotent() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    renderer.start_typing()
    first = renderer._typing_task
    renderer.start_typing()
    await _settle()

    assert renderer._typing_task is first, "second start replaced the live task"
    assert channel.enters == 1

    await renderer.stop_typing()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        QueryResult(session_id="s", cost_usd=0.0, duration_ms=0),
        QueryResult(session_id="flowchart", is_flowchart=True),
        RateLimitHit(error_type="rate_limit", error_text=""),
        TransientError(error_type="overloaded"),
        StreamKilled(),
    ],
    ids=["query_result", "flowchart_result", "rate_limit", "transient", "killed"],
)
async def test_terminal_events_stop_typing(event: Any) -> None:
    """Old path stopped typing at ResultMessage / rate limit / API error / kill.

    See discord_stream.py:1046, :983, :995, :1484.
    """
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(StreamStart())
    await _settle()
    assert channel.enters == 1

    await renderer.handle(event)

    assert renderer._typing_task is None, f"{type(event).__name__} left typing running"
    assert channel.exits == 1


@pytest.mark.asyncio
async def test_frontend_set_typing_stops_the_renderer() -> None:
    """Plan-approval / ask_question gates drop the indicator via set_typing."""
    from axi.discord_frontend import DiscordFrontend

    channel = _FakeChannel()
    frontend = DiscordFrontend(None)  # type: ignore[arg-type]
    renderer = _renderer(channel)
    frontend._stream_renderers["agent"] = renderer

    renderer.start_typing()
    await _settle()
    assert channel.enters == 1

    await frontend.set_typing("agent", False)
    assert renderer._typing_task is None
    assert channel.exits == 1

    # And it can be brought back.
    await frontend.set_typing("agent", True)
    await _settle()
    assert channel.enters == 2

    await renderer.stop_typing()


@pytest.mark.asyncio
async def test_set_typing_no_renderer_is_a_noop() -> None:
    from axi.discord_frontend import DiscordFrontend

    frontend = DiscordFrontend(None)  # type: ignore[arg-type]
    await frontend.set_typing("nobody", False)
    await frontend.set_typing("nobody", True)
