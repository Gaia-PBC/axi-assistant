"""Regression tests for R11 and R12 — two flowchart-block regressions.

R11 — output-schema JSON leaking into the channel. The old path set
suppress_stream from the block's has_output_schema flag (discord_stream.py:
1206-1210) and then skipped _flush_text on block_complete (:1229-1232), so a
block's internal branching JSON never reached Discord. The refactor keyed
suppression on block_type instead — and BlockStart carried no has_output_schema
field at all, making the original rule unrepresentable. Worse, _suppress_stream
was only consulted in _on_text_delta, never in _on_text_flush, so suppressed
text still flushed at block end. Two defects: wrong predicate, and suppression
that did not suppress.

R12 — block_timeout went silent. The old path posted
"⏱️ Block X (`type`) timed out after Ns (limit: Ms)" (:1249-1263); streaming.py
funnels the subtype into a generic SystemNotification and the renderer dropped
it into a log.debug else-branch, so a killed block looked like nothing happened.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    BlockStart,
    FlowchartStart,
    StreamEnd,
    SystemNotification,
    TextDelta,
    TextFlush,
)
from axi.discord_stream_renderer import DiscordStreamRenderer

SCHEMA_JSON = '{"isTask": true, "nextAction": "reply"}'


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.sent: list[str] = []

    async def send(self, content: str) -> Any:
        self.sent.append(content)
        return None


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture _send_system output."""
    captured: list[str] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append(text)

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


@pytest.fixture
def flushed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture whatever reaches the channel as normal assistant text."""
    captured: list[str] = []

    async def _fake_send_long(channel: Any, text: str) -> Any:
        captured.append(text)
        return None

    import axi.agents

    monkeypatch.setattr(axi.agents, "send_long", _fake_send_long)
    return captured


def _renderer(channel: _FakeChannel) -> DiscordStreamRenderer:
    return DiscordStreamRenderer("agent", channel, None, streaming_enabled=False)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R11 — contract + suppression
# ---------------------------------------------------------------------------


def test_block_start_carries_output_schema_flag() -> None:
    assert BlockStart(block_name="B", block_type="prompt").has_output_schema is False
    assert BlockStart(block_name="B", has_output_schema=True).has_output_schema is True


@pytest.mark.asyncio
async def test_output_schema_block_suppresses_delta_and_flush(
    posted: list[str], flushed: list[str]
) -> None:
    """The core leak: suppressed text used to still flush at block end."""
    renderer = _renderer(_FakeChannel())

    await renderer.handle(FlowchartStart(command="mil"))
    await renderer.handle(
        BlockStart(block_name="CLASSIFY", block_type="prompt", has_output_schema=True)
    )
    await renderer.handle(TextDelta(text=SCHEMA_JSON))
    await renderer.handle(TextFlush(text=SCHEMA_JSON, reason="block_complete"))

    assert flushed == [], f"internal JSON leaked to the channel: {flushed}"
    assert not any(SCHEMA_JSON in m for m in posted)


@pytest.mark.asyncio
async def test_ordinary_block_text_still_reaches_the_channel(
    flushed: list[str],
) -> None:
    """Suppression must not swallow real prose."""
    renderer = _renderer(_FakeChannel())

    await renderer.handle(FlowchartStart(command="mil"))
    await renderer.handle(
        BlockStart(block_name="WRITE", block_type="prompt", has_output_schema=False)
    )
    await renderer.handle(TextFlush(text="here is your answer", reason="block_complete"))
    await renderer.handle(StreamEnd())

    assert flushed == ["here is your answer"]


@pytest.mark.asyncio
async def test_prompt_block_without_schema_is_not_suppressed() -> None:
    """The refactor suppressed every prompt/branch/refresh block — too broad."""
    renderer = _renderer(_FakeChannel())

    for block_type in ("prompt", "branch", "refresh"):
        await renderer.handle(
            BlockStart(block_name="B", block_type=block_type, has_output_schema=False)
        )
        assert renderer._suppress_stream is False, block_type


@pytest.mark.asyncio
async def test_fc_show_output_schema_env_overrides(
    monkeypatch: pytest.MonkeyPatch, flushed: list[str]
) -> None:
    monkeypatch.setenv("FC_SHOW_OUTPUT_SCHEMA", "1")
    renderer = _renderer(_FakeChannel())

    await renderer.handle(
        BlockStart(block_name="CLASSIFY", block_type="prompt", has_output_schema=True)
    )
    await renderer.handle(TextFlush(text=SCHEMA_JSON, reason="block_complete"))
    await renderer.handle(StreamEnd())

    assert flushed == [SCHEMA_JSON], "the escape hatch must still work"


@pytest.mark.asyncio
async def test_suppression_clears_on_block_complete(flushed: list[str]) -> None:
    """A suppressed block must not mute the block that follows it."""
    from agenthub.stream_types import BlockComplete

    renderer = _renderer(_FakeChannel())

    await renderer.handle(
        BlockStart(block_name="CLASSIFY", block_type="prompt", has_output_schema=True)
    )
    await renderer.handle(BlockComplete(block_name="CLASSIFY", success=True))
    await renderer.handle(TextFlush(text="visible again", reason="post_loop"))
    await renderer.handle(StreamEnd())

    assert flushed == ["visible again"]


# ---------------------------------------------------------------------------
# R12 — block_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_timeout_is_reported(posted: list[str]) -> None:
    renderer = _renderer(_FakeChannel())

    await renderer.handle(
        SystemNotification(
            subtype="block_timeout",
            data={
                "data": {
                    "block_name": "RESEARCH",
                    "block_type": "prompt",
                    "elapsed_ms": 125_000,
                    "timeout_seconds": 120,
                }
            },
        )
    )

    assert len(posted) == 1
    msg = posted[0]
    assert "RESEARCH" in msg
    assert "125s" in msg
    assert "120" in msg
    assert "⏱️" in msg


@pytest.mark.asyncio
async def test_block_timeout_tolerates_missing_fields(posted: list[str]) -> None:
    renderer = _renderer(_FakeChannel())

    await renderer.handle(SystemNotification(subtype="block_timeout", data={"data": {}}))

    assert len(posted) == 1, "a malformed timeout must still surface, not crash"


@pytest.mark.asyncio
async def test_unknown_subtype_still_ignored(posted: list[str]) -> None:
    renderer = _renderer(_FakeChannel())

    await renderer.handle(SystemNotification(subtype="something_else", data={"data": {}}))

    assert posted == []
