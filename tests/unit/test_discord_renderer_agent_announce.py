"""Regression tests for R5 — Agent tool_use announcements lost in the hub refactor.

The old path (_announce_agent_tool_use, discord_stream.py:436-468, called from
:859 and :1020) posted "`🔧 Agent {subagent_type} — {description} ({id})`" plus
the pretty-printed JSON input for every *top-level* Agent call, and ENRICHED the
same message as the input streamed in rather than posting a second one. The
refactored renderer only called log.debug, so subagent launches became invisible.

Restoring it required a contract change first: ToolUseStart/ToolUseEnd carried no
tool_use_id, which is what the old code keyed dedup, enrichment and the
top-level-vs-nested distinction on.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import ToolUseEnd, ToolUseStart
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeMessage:
    _counter = 0

    def __init__(self, content: str) -> None:
        type(self)._counter += 1
        self.id = type(self)._counter
        self.content = content

    async def edit(self, content: str = "", **_kw: Any) -> None:
        self.content = content


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.messages: list[_FakeMessage] = []

    async def send(self, content: str) -> _FakeMessage:
        msg = _FakeMessage(content)
        self.messages.append(msg)
        return msg


def _renderer(channel: _FakeChannel) -> DiscordStreamRenderer:
    return DiscordStreamRenderer("agent", channel, None, streaming_enabled=False)  # type: ignore[arg-type]


AGENT_INPUT = {
    "subagent_type": "code-reviewer",
    "description": "review the diff",
    "prompt": "look at everything",
}


# ---------------------------------------------------------------------------
# Contract: the ids the announce logic depends on
# ---------------------------------------------------------------------------


def test_tool_events_carry_correlation_ids() -> None:
    """Without these the old dedup/enrich/nesting logic cannot be expressed."""
    start = ToolUseStart(tool_name="Agent", tool_use_id="tu_1", parent_tool_use_id=None)
    end = ToolUseEnd(tool_name="Agent", tool_use_id="tu_1", parent_tool_use_id=None)
    assert start.tool_use_id == "tu_1"
    assert end.tool_use_id == "tu_1"
    assert start.parent_tool_use_id is None


def test_tool_event_ids_default_to_none() -> None:
    """New fields must stay optional so existing constructions keep working."""
    assert ToolUseStart(tool_name="Bash").tool_use_id is None
    assert ToolUseEnd(tool_name="Bash").parent_tool_use_id is None


# ---------------------------------------------------------------------------
# Announce behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_call_is_announced_then_enriched_in_place() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id="tu_1"))
    assert len(channel.messages) == 1, "start should post the label immediately"
    assert "🔧" in channel.messages[0].content
    assert "tu_1" in channel.messages[0].content

    await renderer.handle(
        ToolUseEnd(tool_name="Agent", tool_use_id="tu_1", tool_input=AGENT_INPUT)
    )

    assert len(channel.messages) == 1, "enrich must edit, not post a second message"
    body = channel.messages[0].content
    assert "```json" in body
    assert "code-reviewer" in body
    assert "review the diff" in body


@pytest.mark.asyncio
async def test_nested_agent_calls_are_not_announced() -> None:
    """A subagent's own Agent call is covered by its parent's announcement."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(
        ToolUseStart(tool_name="Agent", tool_use_id="tu_2", parent_tool_use_id="tu_1")
    )
    await renderer.handle(
        ToolUseEnd(
            tool_name="Agent",
            tool_use_id="tu_2",
            parent_tool_use_id="tu_1",
            tool_input=AGENT_INPUT,
        )
    )

    assert channel.messages == []


@pytest.mark.asyncio
async def test_non_agent_tools_are_not_announced() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(ToolUseStart(tool_name="Bash", tool_use_id="tu_9"))
    await renderer.handle(
        ToolUseEnd(tool_name="Bash", tool_use_id="tu_9", tool_input={"command": "ls"})
    )

    assert channel.messages == []


@pytest.mark.asyncio
async def test_missing_tool_use_id_is_ignored_not_crashed() -> None:
    """Older producers may not populate the new field."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(ToolUseStart(tool_name="Agent"))
    await renderer.handle(ToolUseEnd(tool_name="Agent", tool_input=AGENT_INPUT))

    assert channel.messages == []


@pytest.mark.asyncio
async def test_two_agent_calls_get_separate_announcements() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    for tid in ("tu_1", "tu_2"):
        await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id=tid))
    assert len(channel.messages) == 2
    assert "tu_1" in channel.messages[0].content
    assert "tu_2" in channel.messages[1].content


@pytest.mark.asyncio
async def test_label_is_recorded_for_downstream_task_prefixes() -> None:
    """R7 needs this map to prefix a subagent's task updates."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id="tu_1"))
    await renderer.handle(
        ToolUseEnd(tool_name="Agent", tool_use_id="tu_1", tool_input=AGENT_INPUT)
    )

    label = renderer._agent_labels["tu_1"]
    assert "code-reviewer" in label
    assert "review the diff" in label


@pytest.mark.asyncio
async def test_oversized_input_is_chunked_across_messages() -> None:
    """The old path chunked rather than letting a >2000 char edit 400."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    big = {"subagent_type": "x", "description": "y", "prompt": "z" * 4000}
    await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id="tu_1"))
    await renderer.handle(
        ToolUseEnd(tool_name="Agent", tool_use_id="tu_1", tool_input=big)
    )

    assert len(channel.messages) > 1, "long input must span multiple messages"
    for msg in channel.messages:
        assert len(msg.content) <= 2000


@pytest.mark.asyncio
async def test_repeated_end_without_new_input_does_not_repost() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id="tu_1"))
    end = ToolUseEnd(tool_name="Agent", tool_use_id="tu_1", tool_input=AGENT_INPUT)
    await renderer.handle(end)
    count_after_first = len(channel.messages)
    await renderer.handle(end)

    assert len(channel.messages) == count_after_first


@pytest.mark.asyncio
async def test_json_body_is_deterministic() -> None:
    """sort_keys keeps the rendered payload stable between edits."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id="tu_1"))
    await renderer.handle(
        ToolUseEnd(tool_name="Agent", tool_use_id="tu_1", tool_input=AGENT_INPUT)
    )

    rendered = "".join(m.content for m in channel.messages)
    expected = json.dumps(AGENT_INPUT, indent=2, sort_keys=True, ensure_ascii=False)
    assert expected.splitlines()[1] in rendered
