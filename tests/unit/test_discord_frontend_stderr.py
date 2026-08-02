"""Regression tests for R9 — debug-mode stderr never reached Discord.

The old path (_drain_and_send_stderr, discord_stream.py:105-121) drained the
CLI stderr buffer AND, when discord_state(session).debug was on, posted each
line as a fenced code block. It ran per-event inside the stream loop (:1414)
and again after it (:1449).

After the hub refactor runtime.py's _consume_stream had no drain at all, and
agents.drain_stderr only RETURNS the list — it is called from
turn_hooks.before_turn, which discards the result. So /debug output vanished,
and stderr written during a turn sat in the buffer until the START of the next
turn and was then thrown away.

The buffer lives on DiscordAgentState, i.e. it is frontend-owned state, so the
drain belongs in DiscordFrontend.on_stream_event — which already receives every
event. No hook in generic agenthub is required.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import StreamEnd, TextDelta
from axi.axi_types import discord_state
from axi.discord_frontend import DiscordFrontend


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242


class _Session:
    frontend_state = None


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append(text)

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


@pytest.fixture
def frontend(monkeypatch: pytest.MonkeyPatch) -> DiscordFrontend:
    async def _fake_channel(agent_name: str) -> Any:
        return _FakeChannel()

    import axi.channels

    monkeypatch.setattr(axi.channels, "get_agent_channel", _fake_channel)
    return DiscordFrontend(None)  # type: ignore[arg-type]


def _register(
    monkeypatch: pytest.MonkeyPatch, *, debug: bool, lines: list[str]
) -> _Session:
    from axi import agents as agents_mod

    session = _Session()
    ds = discord_state(session)  # type: ignore[arg-type]
    ds.debug = debug
    ds.stderr_buffer.extend(lines)
    monkeypatch.setitem(agents_mod.agents, "agent", session)  # type: ignore[arg-type]
    return session


@pytest.mark.asyncio
async def test_debug_agent_sees_stderr_as_code_blocks(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, debug=True, lines=["warning: deprecated flag"])

    await frontend.on_stream_event("agent", TextDelta(text="hi"))

    assert len(posted) == 1
    assert "warning: deprecated flag" in posted[0]
    assert posted[0].startswith("```")


@pytest.mark.asyncio
async def test_buffer_is_drained_even_when_quiet(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Always drain, or the buffer grows unbounded across a long turn."""
    session = _register(monkeypatch, debug=False, lines=["noise", "more noise"])

    await frontend.on_stream_event("agent", TextDelta(text="hi"))

    assert posted == [], "quiet agents must not see stderr"
    assert discord_state(session).stderr_buffer == [], "buffer must still be drained"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_stderr_is_drained_during_the_turn_not_the_next_one(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: output written mid-turn used to sit until the next turn."""
    session = _register(monkeypatch, debug=True, lines=[])

    discord_state(session).stderr_buffer.append("mid-turn warning")  # type: ignore[arg-type]
    await frontend.on_stream_event("agent", TextDelta(text="chunk"))

    assert any("mid-turn warning" in m for m in posted)


@pytest.mark.asyncio
async def test_stream_end_flushes_remaining_stderr(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """StreamEnd covers what the old post-loop drain did."""
    _register(monkeypatch, debug=True, lines=["final warning"])

    await frontend.on_stream_event("agent", StreamEnd())

    assert any("final warning" in m for m in posted)


@pytest.mark.asyncio
async def test_blank_lines_are_skipped(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, debug=True, lines=["", "   ", "\n"])

    await frontend.on_stream_event("agent", TextDelta(text="hi"))

    assert posted == []


@pytest.mark.asyncio
async def test_unknown_agent_is_a_noop(
    frontend: DiscordFrontend, posted: list[str]
) -> None:
    await frontend.on_stream_event("nobody", TextDelta(text="hi"))

    assert posted == []


@pytest.mark.asyncio
async def test_long_stderr_is_split(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, debug=True, lines=["x" * 5000])

    await frontend.on_stream_event("agent", TextDelta(text="hi"))

    assert len(posted) > 1, "oversized stderr must be chunked, not sent as one message"
    for part in posted:
        assert len(part) <= 2000


@pytest.mark.asyncio
async def test_nothing_buffered_posts_nothing(
    frontend: DiscordFrontend, posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, debug=True, lines=[])

    await frontend.on_stream_event("agent", TextDelta(text="hi"))

    assert posted == []
