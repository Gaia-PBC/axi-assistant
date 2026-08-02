"""Regression tests for R10 — the per-message timing footer.

The old path appended "\\n-# {elapsed}s{trace_tag}" (discord_stream.py:1502-1528):
Discord subtext on its own line, WALL-CLOCK elapsed measured from
session.activity.query_started, carrying the OTEL trace tag, with three
placements (append to a deferred message / inline into the buffer / edit the
last message) and a channel.send fallback when the edit failed (:1527-1528).

The refactor appended " ({cost}, {duration})" on the same line: it added cost —
a genuine improvement, kept here — but dropped the trace tag, switched to
QueryResult.duration_ms (model time, not what the user waited), and on edit
failure logged at debug and posted nothing, so the timing vanished.

Format chosen by the user: subtext line, wall-clock, cost, trace tag, fallback.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import QueryResult
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242


class _Activity:
    def __init__(self, seconds_ago: float | None) -> None:
        self.query_started = (
            None if seconds_ago is None
            else datetime.now(UTC) - timedelta(seconds=seconds_ago)
        )


class _Session:
    def __init__(self, seconds_ago: float | None) -> None:
        self.activity = _Activity(seconds_ago)


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append(text)

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


@pytest.fixture
def edits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture edit_message content; set fail=True on the client to make it raise."""
    captured: list[str] = []

    class _Client:
        fail = False

        async def edit_message(self, channel_id: int, message_id: str, content: str) -> None:
            if self.fail:
                raise RuntimeError("edit blew up")
            captured.append(content)

    from axi import config

    monkeypatch.setattr(config, "discord_client", _Client())
    return captured


def _renderer(monkeypatch: pytest.MonkeyPatch, *, seconds_ago: float | None = 4.2,
              trace: str = "") -> DiscordStreamRenderer:
    from axi import agents as agents_mod

    monkeypatch.setitem(agents_mod.agents, "agent", _Session(seconds_ago))  # type: ignore[arg-type]
    monkeypatch.setattr(agents_mod, "get_active_trace_tag", lambda _n: trace)
    return DiscordStreamRenderer("agent", _FakeChannel(), None, streaming_enabled=False)  # type: ignore[arg-type]


def _result(cost: float = 0.0123) -> QueryResult:
    # duration_ms is deliberately absurd: the footer must use wall-clock instead.
    return QueryResult(session_id="s", cost_usd=cost, duration_ms=999_999)


@pytest.mark.asyncio
async def test_footer_is_subtext_with_wallclock_cost_and_trace(
    monkeypatch: pytest.MonkeyPatch, edits: list[str]
) -> None:
    renderer = _renderer(monkeypatch, seconds_ago=4.2, trace="[trace=abc123]")
    renderer._last_flushed_msg_id = "1"
    renderer._last_flushed_channel_id = 4242
    renderer._last_flushed_content = "the answer"

    await renderer.handle(_result())

    assert len(edits) == 1
    body = edits[0]
    assert body.startswith("the answer\n-# ")
    assert "4.2s" in body, "must be wall-clock, not duration_ms (which was 999999ms)"
    assert "999" not in body
    assert "$0.0123" in body
    assert "[trace=abc123]" in body


@pytest.mark.asyncio
async def test_edit_failure_posts_instead_of_dropping(
    monkeypatch: pytest.MonkeyPatch, posted: list[str]
) -> None:
    """The refactor logged at debug and lost the timing entirely."""
    renderer = _renderer(monkeypatch, seconds_ago=3.0)
    renderer._last_flushed_msg_id = "1"
    renderer._last_flushed_channel_id = 4242
    renderer._last_flushed_content = "the answer"

    from axi import config

    config.discord_client.fail = True  # type: ignore[attr-defined]

    await renderer.handle(_result())

    assert len(posted) == 1
    assert posted[0].startswith("-# ")
    assert "3.0s" in posted[0]


@pytest.mark.asyncio
async def test_deferred_message_gets_the_footer_appended(
    monkeypatch: pytest.MonkeyPatch, edits: list[str]
) -> None:
    """Non-streaming: the footer rides along with the pending message."""
    renderer = _renderer(monkeypatch, seconds_ago=1.5)
    renderer._deferred_msg = "the answer"

    await renderer.handle(_result())

    assert edits == [], "must not edit when a deferred message is waiting"
    assert renderer._deferred_msg.startswith("the answer\n-# ")
    assert "1.5s" in renderer._deferred_msg


@pytest.mark.asyncio
async def test_no_target_still_posts_the_footer(
    monkeypatch: pytest.MonkeyPatch, posted: list[str]
) -> None:
    renderer = _renderer(monkeypatch, seconds_ago=2.0)

    await renderer.handle(_result())

    assert len(posted) == 1
    assert "2.0s" in posted[0]


@pytest.mark.asyncio
async def test_trace_tag_omitted_when_absent(
    monkeypatch: pytest.MonkeyPatch, edits: list[str]
) -> None:
    renderer = _renderer(monkeypatch, seconds_ago=1.0, trace="")
    renderer._last_flushed_msg_id = "1"
    renderer._last_flushed_channel_id = 4242
    renderer._last_flushed_content = "x"

    await renderer.handle(_result())

    assert edits[0] == "x\n-# 1.0s · $0.0123", edits[0]


@pytest.mark.asyncio
async def test_zero_cost_is_omitted(
    monkeypatch: pytest.MonkeyPatch, edits: list[str]
) -> None:
    renderer = _renderer(monkeypatch, seconds_ago=1.0)
    renderer._last_flushed_msg_id = "1"
    renderer._last_flushed_channel_id = 4242
    renderer._last_flushed_content = "x"

    await renderer.handle(_result(cost=0.0))

    assert edits[0] == "x\n-# 1.0s"


@pytest.mark.asyncio
async def test_flowchart_result_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, posted: list[str], edits: list[str]
) -> None:
    renderer = _renderer(monkeypatch, seconds_ago=1.0)

    await renderer.handle(QueryResult(session_id="flowchart", is_flowchart=True))

    assert posted == []
    assert edits == []


@pytest.mark.asyncio
async def test_falls_back_to_stream_start_when_session_has_no_start_time(
    monkeypatch: pytest.MonkeyPatch, posted: list[str]
) -> None:
    """An unknown or freshly-reconstructed session must still get a duration."""
    from agenthub.stream_types import StreamStart

    renderer = _renderer(monkeypatch, seconds_ago=None)
    await renderer.handle(StreamStart())

    await renderer.handle(_result())

    assert len(posted) == 1
    assert "s ·" in posted[0] or posted[0].endswith("s")
    await renderer.stop_typing()
