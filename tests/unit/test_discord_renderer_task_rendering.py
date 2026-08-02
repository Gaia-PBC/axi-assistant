"""Regression tests for R6 and R7 — subagent task rendering.

R6 — the old path kept task_status_messages[task_id] + task_last_status_content
and EDITED the existing Discord message via _render_chunked, skipping no-op
updates (_upsert_task_status, discord_stream.py:470-486). The refactor called
_send_system on every tick, so one long subagent emitted a fresh message per
progress update.

R7 — the old formatters carried task_type, tool_use_id, task_id, tool/token/
duration counts, the last tool name, and a "[parent Agent label]" prefix for
nested tasks. The refactor reduced task_started to "🚀 Task started: {desc}".

Third defect, not in the original audit: task_* payloads live at the TOP level
of the system message, while block_*/flowchart_* nest under a "data" key
(compare discord_stream.py:1100-1171 against :1209+). The refactor read the
nested shape for all of them, which is why it reached for a "content" key the
producer never sets — so task_progress and task_notification rendered nothing
at all.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import SystemNotification, ToolUseEnd, ToolUseStart
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
        self.edits = 0

    async def send(self, content: str) -> _FakeMessage:
        msg = _FakeMessage(content)
        self.messages.append(msg)
        return msg


def _renderer(channel: _FakeChannel) -> DiscordStreamRenderer:
    return DiscordStreamRenderer("agent", channel, None, streaming_enabled=False)  # type: ignore[arg-type]


def _progress(task_id: str, tools: int, tokens: int, **extra: Any) -> SystemNotification:
    return SystemNotification(
        subtype="task_progress",
        data={
            "task_id": task_id,
            "tool_use_id": extra.pop("tool_use_id", "tu_1"),
            "description": "review the diff",
            "last_tool_name": extra.pop("last_tool_name", "Read"),
            "usage": {"tool_uses": tools, "total_tokens": tokens, "duration_ms": 4200},
            **extra,
        },
    )


# ---------------------------------------------------------------------------
# R6 — edit in place instead of reposting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_updates_edit_one_message() -> None:
    """The spam regression: a fresh message per tick used to flood the channel."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    for i in range(1, 6):
        await renderer.handle(_progress("task_a", tools=i, tokens=i * 100))

    assert len(channel.messages) == 1, f"posted {len(channel.messages)} messages"
    assert "5 tools" in channel.messages[0].content
    assert "500 tokens" in channel.messages[0].content


@pytest.mark.asyncio
async def test_identical_progress_is_a_noop() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    event = _progress("task_a", tools=3, tokens=300)
    await renderer.handle(event)
    first = channel.messages[0].content
    await renderer.handle(event)

    assert len(channel.messages) == 1
    assert channel.messages[0].content == first


@pytest.mark.asyncio
async def test_separate_tasks_get_separate_messages() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(_progress("task_a", tools=1, tokens=10))
    await renderer.handle(_progress("task_b", tools=1, tokens=10))

    assert len(channel.messages) == 2


# ---------------------------------------------------------------------------
# R7 / payload shape — detail actually rendered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_started_carries_full_detail() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(
        SystemNotification(
            subtype="task_started",
            data={
                "task_id": "task_a",
                "task_type": "code-reviewer",
                "tool_use_id": "tu_1",
                "description": "review the diff",
            },
        )
    )

    body = channel.messages[0].content
    assert "code-reviewer" in body
    assert "review the diff" in body
    assert "tu_1" in body
    assert "task_a" in body


@pytest.mark.asyncio
async def test_progress_renders_from_top_level_payload() -> None:
    """The refactor read a nested data['content'] key that never exists."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(_progress("task_a", tools=7, tokens=1234))

    assert channel.messages, "task_progress rendered nothing"
    body = channel.messages[0].content
    assert "7 tools" in body
    assert "1234 tokens" in body
    assert "4.2s" in body
    assert "Read" in body


@pytest.mark.asyncio
async def test_notification_renders_summary_and_details() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(
        SystemNotification(
            subtype="task_notification",
            data={
                "task_id": "task_a",
                "tool_use_id": "tu_1",
                "status": "completed",
                "summary": "found two bugs",
                "output_file": "/tmp/out.md",
                "usage": {"tool_uses": 9, "duration_ms": 8000},
            },
        )
    )

    body = channel.messages[0].content
    assert "completed" in body
    assert "found two bugs" in body
    assert "tools=9" in body
    assert "duration=8.0s" in body
    assert "/tmp/out.md" in body


@pytest.mark.asyncio
async def test_task_without_id_is_ignored() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(SystemNotification(subtype="task_progress", data={}))
    await renderer.handle(SystemNotification(subtype="task_started", data={}))

    assert channel.messages == []


@pytest.mark.asyncio
async def test_task_started_is_posted_once() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    event = SystemNotification(
        subtype="task_started",
        data={"task_id": "task_a", "task_type": "x", "description": "d"},
    )
    await renderer.handle(event)
    await renderer.handle(event)

    assert len(channel.messages) == 1


# ---------------------------------------------------------------------------
# R7 — [parent Agent label] prefixes (unblocked by 72f1f06)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_task_is_prefixed_with_its_agent_label() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    # A top-level Agent call gets announced, which records its label...
    await renderer.handle(ToolUseStart(tool_name="Agent", tool_use_id="tu_agent"))
    await renderer.handle(
        ToolUseEnd(
            tool_name="Agent",
            tool_use_id="tu_agent",
            tool_input={"subagent_type": "code-reviewer", "description": "review"},
        )
    )
    # ...and a tool running inside it points back at that call.
    await renderer.handle(
        ToolUseStart(tool_name="Read", tool_use_id="tu_child", parent_tool_use_id="tu_agent")
    )
    channel.messages.clear()

    await renderer.handle(_progress("task_a", tools=1, tokens=1, tool_use_id="tu_child"))

    body = channel.messages[0].content
    assert "[Agent code-reviewer" in body, body


@pytest.mark.asyncio
async def test_unparented_task_has_no_prefix() -> None:
    channel = _FakeChannel()
    renderer = _renderer(channel)

    await renderer.handle(_progress("task_a", tools=1, tokens=1, tool_use_id="tu_orphan"))

    assert not channel.messages[0].content.startswith("`🔧 [")


@pytest.mark.asyncio
async def test_parent_walk_survives_a_cycle() -> None:
    """A malformed parent chain must not hang the renderer."""
    channel = _FakeChannel()
    renderer = _renderer(channel)

    renderer._tool_parents["a"] = "b"
    renderer._tool_parents["b"] = "a"

    await renderer.handle(_progress("task_a", tools=1, tokens=1, tool_use_id="a"))

    assert channel.messages, "cycle should fall through to no prefix, not hang"
