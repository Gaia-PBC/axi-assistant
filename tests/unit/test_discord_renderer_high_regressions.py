"""Regression tests for two HIGH UX regressions from the hub refactor.

R2 — flowchart block spam. The old path gated the "▶ block" announcement on
``ds.verbose or ds.fc_current_command not in _FC_QUIET_COMMANDS``
(discord_stream.py:1221, quiet set at :1090 = {soul, soul-flow}). Because /soul
wraps every user message, block chatter was invisible in normal conversation.
The refactored renderer posted unconditionally, so every ordinary turn emitted a
"▶" line per block.

R3 — spurious @user ping. The old path pinged only on clean completion
(discord_stream.py:1537) and on kill (:1493); the rate-limit (:1455) and
transient-error (:1466) paths returned *before* reaching it. streaming.py:238
now yields StreamEnd on every terminal path, so the ported ping (0f85e12) fired
even when the turn produced no answer — summoning the user to an empty result.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    BlockComplete,
    BlockStart,
    FlowchartEnd,
    FlowchartStart,
    RateLimitHit,
    StreamEnd,
    TransientError,
)
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.sent: list[str] = []

    async def send(self, content: str) -> Any:
        self.sent.append(content)
        return None


@pytest.fixture
def system_msgs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture everything the renderer routes through _send_system."""
    captured: list[str] = []

    async def _fake_send(channel: Any, text: str, **kwargs: Any) -> None:
        captured.append(text)

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake_send)
    return captured


def _renderer(channel: _FakeChannel) -> DiscordStreamRenderer:
    return DiscordStreamRenderer("agent", channel, None, streaming_enabled=False)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R3 — end-of-stream ping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_stream_end_still_pings() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(StreamEnd())

    assert len(channel.sent) == 1, "clean completion must still ping the user"
    assert "<@" in channel.sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        RateLimitHit(error_type="rate_limit", error_text="limit"),
        TransientError(error_type="overloaded"),
    ],
    ids=["rate_limit", "transient_error"],
)
async def test_error_stream_end_does_not_ping(event: Any) -> None:
    """Old code returned before the ping on both of these paths."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(event)
    await renderer.handle(StreamEnd())

    assert channel.sent == [], f"{type(event).__name__} must not summon the user"


@pytest.mark.asyncio
async def test_error_flag_is_per_renderer() -> None:
    """A fresh renderer (next turn) pings normally again."""
    ch1 = _FakeChannel()
    r1 = _renderer(ch1)
    await r1.handle(TransientError(error_type="overloaded"))
    await r1.handle(StreamEnd())
    assert ch1.sent == []

    ch2 = _FakeChannel()
    r2 = _renderer(ch2)
    await r2.handle(StreamEnd())
    assert len(ch2.sent) == 1


# ---------------------------------------------------------------------------
# R2 — flowchart block spam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["soul", "soul-flow"])
async def test_quiet_commands_suppress_block_output(
    system_msgs: list[str], command: str
) -> None:
    """/soul wraps every message — its block chatter must stay invisible."""
    renderer = _renderer(_FakeChannel())

    await renderer.handle(FlowchartStart(command=command, block_count=3))
    await renderer.handle(BlockStart(block_name="CLASSIFY", block_type="prompt"))
    await renderer.handle(BlockComplete(block_name="CLASSIFY", success=False))

    assert system_msgs == [], f"/{command} leaked block progress: {system_msgs}"


@pytest.mark.asyncio
async def test_non_quiet_command_still_posts_blocks(system_msgs: list[str]) -> None:
    renderer = _renderer(_FakeChannel())

    await renderer.handle(FlowchartStart(command="mil", block_count=2))
    await renderer.handle(BlockStart(block_name="BUILD", block_type="prompt"))
    await renderer.handle(BlockComplete(block_name="BUILD", success=False))

    assert any("▶" in m and "BUILD" in m for m in system_msgs)
    assert any("❌" in m and "BUILD" in m for m in system_msgs)


@pytest.mark.asyncio
async def test_no_flowchart_context_still_posts(system_msgs: list[str]) -> None:
    """Blocks outside a known flowchart keep the old default (visible)."""
    renderer = _renderer(_FakeChannel())

    await renderer.handle(BlockStart(block_name="LOOSE", block_type="prompt"))

    assert any("LOOSE" in m for m in system_msgs)


@pytest.mark.asyncio
async def test_verbose_agent_sees_quiet_blocks(
    system_msgs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """verbose mode overrides the quiet set, as in discord_stream.py:1221."""
    from axi import agents as agents_mod
    from axi.axi_types import discord_state

    class _Session:
        frontend_state = None

    session = _Session()
    discord_state(session).verbose = True  # type: ignore[arg-type]
    monkeypatch.setitem(agents_mod.agents, "agent", session)  # type: ignore[arg-type]

    renderer = _renderer(_FakeChannel())
    await renderer.handle(FlowchartStart(command="soul", block_count=1))
    await renderer.handle(BlockStart(block_name="CLASSIFY", block_type="prompt"))

    assert any("CLASSIFY" in m for m in system_msgs)


@pytest.mark.asyncio
async def test_silent_block_types_never_post(system_msgs: list[str]) -> None:
    renderer = _renderer(_FakeChannel())

    await renderer.handle(FlowchartStart(command="mil", block_count=3))
    for bt in ("start", "end", "variable"):
        await renderer.handle(BlockStart(block_name=bt.upper(), block_type=bt))

    assert system_msgs == []


@pytest.mark.asyncio
async def test_flowchart_end_clears_command(system_msgs: list[str]) -> None:
    """After the flowchart ends, later blocks are no longer treated as quiet."""
    renderer = _renderer(_FakeChannel())

    await renderer.handle(FlowchartStart(command="soul", block_count=1))
    await renderer.handle(FlowchartEnd(status="completed"))
    await renderer.handle(BlockStart(block_name="AFTER", block_type="prompt"))

    assert any("AFTER" in m for m in system_msgs)


@pytest.mark.asyncio
async def test_quiet_gate_is_independent_of_output_suppression(
    system_msgs: list[str],
) -> None:
    """Muting the announcement must not disturb output-suppression state.

    The two gates are orthogonal: the quiet gate (R2) decides whether to post
    "▶ block", while suppression (R11) decides whether the block's *text* is
    internal JSON. This originally asserted that any prompt block suppresses,
    which encoded the block_type predicate R11 replaced with has_output_schema.
    """
    renderer = _renderer(_FakeChannel())
    await renderer.handle(FlowchartStart(command="soul", block_count=2))

    await renderer.handle(
        BlockStart(block_name="CLASSIFY", block_type="prompt", has_output_schema=True)
    )
    assert system_msgs == [], "quiet command must still mute the announcement"
    assert renderer._suppress_stream is True

    await renderer.handle(
        BlockStart(block_name="WRITE", block_type="prompt", has_output_schema=False)
    )
    assert system_msgs == []
    assert renderer._suppress_stream is False, "prose blocks must not be suppressed"
