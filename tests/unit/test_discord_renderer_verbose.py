"""Regression tests for R8 — /verbose changed nothing in the hub renderer.

The old path (discord_stream.py:876-905), gated on discord_state(session).verbose,
posted the accumulated thinking text as a "thinking.md" file attachment and a
per-tool line "`🔧 {tool}: {preview}`" as each tool ran. After the refactor a
grep for ".verbose" across the renderer, the frontend and agenthub/streaming
returned nothing at all — the toggle was inert.

The data was already on the events the whole time: ThinkingEnd carries
thinking_text and ToolUseEnd carries preview; the renderer simply ignored both.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import ThinkingEnd, ToolUseEnd
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242

    async def send(self, content: str) -> Any:
        return None


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append(text)

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    """Capture (filename, bytes) for anything sent as an attachment."""
    captured: list[tuple[str, bytes]] = []

    async def _fake(channel: Any, text: str, **kw: Any) -> None:
        f = kw.get("file")
        if f is not None:
            captured.append((f.filename, f.fp.read()))

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


def _renderer(verbose: bool, monkeypatch: pytest.MonkeyPatch) -> DiscordStreamRenderer:
    from axi import agents as agents_mod
    from axi.axi_types import discord_state

    class _Session:
        frontend_state = None

    session = _Session()
    discord_state(session).verbose = verbose  # type: ignore[arg-type]
    monkeypatch.setitem(agents_mod.agents, "agent", session)  # type: ignore[arg-type]
    return DiscordStreamRenderer("agent", _FakeChannel(), None, streaming_enabled=False)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tool previews
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verbose_narrates_each_tool(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(True, monkeypatch)

    await renderer.handle(
        ToolUseEnd(tool_name="Bash", preview="ls -la /tmp", tool_use_id="tu_1")
    )

    assert posted == ["`🔧 Bash: ls -la /tmp`"]


@pytest.mark.asyncio
async def test_tool_without_preview_still_named(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(True, monkeypatch)

    await renderer.handle(ToolUseEnd(tool_name="Read", preview=None, tool_use_id="tu_1"))

    assert posted == ["`🔧 Read`"]


@pytest.mark.asyncio
async def test_long_preview_is_truncated(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(True, monkeypatch)

    await renderer.handle(ToolUseEnd(tool_name="Bash", preview="x" * 500, tool_use_id="t"))

    assert len(posted) == 1
    assert posted[0].count("x") == 120, "old path capped the preview at 120 chars"


@pytest.mark.asyncio
async def test_quiet_agent_gets_no_tool_lines(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(False, monkeypatch)

    await renderer.handle(ToolUseEnd(tool_name="Bash", preview="ls", tool_use_id="tu_1"))

    assert posted == []


@pytest.mark.asyncio
async def test_unknown_agent_is_treated_as_quiet(posted: list[str]) -> None:
    """No registry entry must not raise — just stay silent."""
    renderer = DiscordStreamRenderer("nobody", _FakeChannel(), None, streaming_enabled=False)  # type: ignore[arg-type]

    await renderer.handle(ToolUseEnd(tool_name="Bash", preview="ls", tool_use_id="tu_1"))

    assert posted == []


# ---------------------------------------------------------------------------
# thinking.md attachment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verbose_attaches_thinking_as_a_file(
    files: list[tuple[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(True, monkeypatch)

    await renderer.handle(ThinkingEnd(thinking_text="  weighing two options  "))

    assert len(files) == 1
    name, body = files[0]
    assert name == "thinking.md"
    assert body == b"weighing two options", "text should be stripped, as before"


@pytest.mark.asyncio
async def test_quiet_agent_gets_no_thinking_file(
    files: list[tuple[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(False, monkeypatch)

    await renderer.handle(ThinkingEnd(thinking_text="weighing two options"))

    assert files == []


@pytest.mark.asyncio
async def test_empty_thinking_posts_nothing(
    files: list[tuple[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer(True, monkeypatch)

    await renderer.handle(ThinkingEnd(thinking_text="   "))
    await renderer.handle(ThinkingEnd())

    assert files == []


@pytest.mark.asyncio
async def test_thinking_end_still_clears_the_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verbose handling must not disturb the existing indicator cleanup."""
    renderer = _renderer(False, monkeypatch)
    renderer._thinking_msg_id = "123"

    deleted: list[str] = []

    class _Client:
        async def delete_message(self, channel_id: int, message_id: str) -> None:
            deleted.append(message_id)

    from axi import config

    monkeypatch.setattr(config, "discord_client", _Client())

    await renderer.handle(ThinkingEnd(thinking_text=""))

    assert deleted == ["123"]
    assert renderer._thinking_msg_id is None
