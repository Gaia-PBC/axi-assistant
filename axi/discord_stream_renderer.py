"""DiscordStreamRenderer — renders StreamOutput events as Discord messages.

Each instance handles one agent's active stream. Created by
DiscordFrontend.on_stream_event on StreamStart, destroyed on StreamEnd.

Uses the discord REST client and live-edit machinery from discord_stream.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from axi import config
from axi.discord_stream import (
    _agent_context_label,
    _BLANK_CONTENT,
    _LiveEditState,
    _live_edit_post,
    _live_edit_update,
    _render_chunked,
    _retry_discord_503,
)
from agenthub.stream_types import (
    BlockComplete,
    BlockStart,
    CompactComplete,
    CompactStart,
    FlowchartEnd,
    FlowchartStart,
    QueryResult,
    RateLimitHit,
    SessionId,
    StreamEnd,
    StreamKilled,
    StreamStart,
    StreamOutput,
    SystemNotification,
    TextDelta,
    TextFlush,
    ThinkingEnd,
    ThinkingStart,
    TodoUpdate,
    ToolInputDelta,
    ToolUseEnd,
    ToolUseStart,
    TransientError,
)

if TYPE_CHECKING:
    from discord import Message, TextChannel
    from discord.ext.commands import Bot

log = logging.getLogger(__name__)

_STREAMING_CURSOR = "█"
_STREAMING_MSG_LIMIT = 1900
_SILENT_BLOCK_TYPES = {"start", "end", "variable"}

# Flowchart commands whose per-block progress is suppressed in Discord.
# /soul wraps every user message, so without this every ordinary turn would
# post a "▶ block" line per block. Mirrors discord_stream.py:1090.
_fc_quiet_str = os.environ.get("FC_QUIET_COMMANDS", "soul,soul-flow")
_FC_QUIET_COMMANDS: set[str] = {c.strip() for c in _fc_quiet_str.split(",") if c.strip()}


class DiscordStreamRenderer:
    """Stateful renderer that turns StreamOutput events into Discord messages.

    Manages live-edit state, typing indicators, thinking messages,
    and text buffering for one agent's conversation turn.
    """

    __slots__ = (
        "_agent_announcements",
        "_agent_labels",
        "_agent_name",
        "_bot",
        "_channel",
        "_deferred_msg",
        "_fc_command",
        "_flush_count",
        "_in_flowchart",
        "_last_flushed_channel_id",
        "_last_flushed_content",
        "_last_flushed_msg_id",
        "_live_edit",
        "_saw_error",
        "_streaming_enabled",
        "_suppress_stream",
        "_text_buffer",
        "_thinking_msg_id",
        "_typing_task",
    )

    def __init__(
        self,
        agent_name: str,
        channel: TextChannel,
        bot: Bot,
        *,
        streaming_enabled: bool | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._channel = channel
        self._bot = bot
        if streaming_enabled is None:
            streaming_enabled = config.STREAMING_DISCORD
        self._streaming_enabled = streaming_enabled

        self._text_buffer = ""
        self._flush_count = 0
        self._deferred_msg = ""
        self._last_flushed_msg_id: str | None = None
        self._last_flushed_channel_id: int | None = None
        self._last_flushed_content = ""
        self._live_edit: _LiveEditState | None = (
            _LiveEditState(channel.id) if streaming_enabled else None
        )
        self._thinking_msg_id: str | None = None
        self._typing_task: asyncio.Task[None] | None = None
        self._in_flowchart = False
        self._suppress_stream = False
        self._fc_command: str | None = None
        # Top-level Agent tool calls announced this stream, keyed by tool_use_id:
        # the Discord messages backing each announcement, and the short label
        # (also what R7 will need to prefix that subagent's task updates).
        self._agent_announcements: dict[str, list[Message]] = {}
        self._agent_labels: dict[str, str] = {}
        # Set when the stream hits a rate limit or transient API error. The old
        # path returned before its end-of-stream ping on both (discord_stream.py
        # :1455, :1466); StreamEnd is now emitted on every terminal path, so the
        # guard has to be explicit.
        self._saw_error = False

    async def handle(self, event: StreamOutput) -> None:
        """Dispatch a StreamOutput event to the appropriate handler."""
        if isinstance(event, TextDelta):
            await self._on_text_delta(event)
        elif isinstance(event, TextFlush):
            await self._on_text_flush(event)
        elif isinstance(event, ThinkingStart):
            await self._on_thinking_start()
        elif isinstance(event, ThinkingEnd):
            await self._on_thinking_end()
        elif isinstance(event, ToolUseStart):
            await self._on_tool_use_start(event)
        elif isinstance(event, ToolInputDelta):
            pass  # tool input tracking handled by consumer
        elif isinstance(event, ToolUseEnd):
            await self._on_tool_use_end(event)
        elif isinstance(event, TodoUpdate):
            await self._on_todo_update(event)
        elif isinstance(event, SessionId):
            pass  # session ID tracking handled by consumer
        elif isinstance(event, StreamStart):
            await self._on_stream_start()
        elif isinstance(event, StreamEnd):
            await self._on_stream_end(event)
        elif isinstance(event, QueryResult):
            await self._on_query_result(event)
        elif isinstance(event, RateLimitHit):
            self._saw_error = True  # notice is orchestration-level; suppress the end ping
            await self.stop_typing()
        elif isinstance(event, TransientError):
            self._saw_error = True  # retry handling is orchestration-level
            await self.stop_typing()
        elif isinstance(event, StreamKilled):
            await self.stop_typing()  # kill handling is orchestration-level
        elif isinstance(event, CompactStart):
            await self._on_compact_start(event)
        elif isinstance(event, CompactComplete):
            pass  # compact completion deferred to next query start
        elif isinstance(event, FlowchartStart):
            await self._on_flowchart_start(event)
        elif isinstance(event, FlowchartEnd):
            await self._on_flowchart_end(event)
        elif isinstance(event, BlockStart):
            await self._on_block_start(event)
        elif isinstance(event, BlockComplete):
            await self._on_block_complete(event)
        elif isinstance(event, SystemNotification):
            await self._on_system_notification(event)

    # --- Lifecycle ---

    async def _keep_typing(self) -> None:
        """Hold ``channel.typing()`` open until cancelled.

        ``Typing.__aenter__`` sends one typing packet and spawns a task that
        re-sends every 5s.  Discord expires the indicator after ~10s, so the
        context manager must stay open for the whole turn: ``await``ing
        ``channel.typing()`` once covers only the first 10s, and handing it to
        ``create_task`` raises TypeError — it is a context manager, not a
        coroutine.
        """
        try:
            async with self._channel.typing():
                await asyncio.Event().wait()
        except Exception:
            log.warning(
                "Typing indicator failed for '%s'", self._agent_name, exc_info=True
            )

    def start_typing(self) -> None:
        """Start the typing indicator, unless one is already running."""
        if self._typing_task is not None:
            return
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def stop_typing(self) -> None:
        """Cancel the typing indicator and let the context manager unwind."""
        task = self._typing_task
        if task is None:
            return
        self._typing_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _on_stream_start(self) -> None:
        self.start_typing()

    async def _on_stream_end(self, event: StreamEnd) -> None:
        await self.stop_typing()

        if self._deferred_msg:
            await self._send_long(self._deferred_msg)
            self._deferred_msg = ""

        # Rate limit / transient error: the old path returned before the ping
        # (discord_stream.py:1455, :1466). Pinging here would summon the user to
        # a turn that produced no answer and no explanation.
        if self._saw_error:
            return

        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        await _retry_discord_503(self._channel.send, mentions)

    # --- Text rendering ---

    async def _on_text_delta(self, event: TextDelta) -> None:
        if self._suppress_stream:
            return
        self._text_buffer += event.text
        if self._streaming_enabled and self._live_edit and not self._in_flowchart:
            await self._do_live_edit_tick()

    async def _on_text_flush(self, event: TextFlush) -> None:
        text = event.text
        if not text.strip():
            return
        self._flush_count += 1
        log.info(
            "RENDER_FLUSH[%s] #%d reason=%s len=%d",
            self._agent_name, self._flush_count, event.reason, len(text.strip()),
        )

        if self._live_edit is not None:
            self._text_buffer = text
            await self._do_live_edit_finalize()
        else:
            if self._deferred_msg:
                await self._send_long(self._deferred_msg)
            self._deferred_msg = text.lstrip()

    # --- Thinking indicators ---

    async def _on_thinking_start(self) -> None:
        try:
            resp = await _retry_discord_503(
                config.discord_client.send_message,
                self._channel.id,
                "*thinking...*",
            )
            self._thinking_msg_id = resp["id"]
        except Exception:
            log.debug("Failed to post thinking indicator for '%s'", self._agent_name)

    async def _on_thinking_end(self) -> None:
        if self._thinking_msg_id:
            try:
                await _retry_discord_503(
                    config.discord_client.delete_message,
                    self._channel.id,
                    self._thinking_msg_id,
                )
            except Exception:
                log.debug("Failed to delete thinking indicator for '%s'", self._agent_name)
            self._thinking_msg_id = None

    # --- Tool use ---

    async def _on_tool_use_start(self, event: ToolUseStart) -> None:
        log.debug("RENDER[%s] tool_use_start: %s", self._agent_name, event.tool_name)
        # The input has not streamed in yet, so this posts the bare label and
        # _on_tool_use_end fills in the JSON once it is complete.
        await self._announce_agent_tool_use(event.tool_name, event, None)

    async def _on_tool_use_end(self, event: ToolUseEnd) -> None:
        if event.preview:
            log.debug(
                "RENDER[%s] tool_use_end: %s -> %s",
                self._agent_name, event.tool_name, event.preview[:80],
            )
        await self._announce_agent_tool_use(event.tool_name, event, event.tool_input)

    async def _announce_agent_tool_use(
        self, tool_name: str, event: Any, tool_input: dict[str, Any] | None,
    ) -> None:
        """Post or enrich the announcement for a top-level Agent tool call.

        Port of discord_stream.py:436-468. Only top-level Agent calls are shown —
        a nested one is already covered by its parent's announcement. The same
        messages are edited as the input arrives rather than reposted, which is
        why this needs the tool_use_id that ToolUseStart/End now carry.
        """
        if tool_name != "Agent" or event.parent_tool_use_id:
            return
        tool_use_id = event.tool_use_id
        if not tool_use_id:
            return

        payload = tool_input or {}
        label = _agent_context_label(tool_use_id, payload)
        # Keep the richest label seen: the start event has no description yet.
        if payload or tool_use_id not in self._agent_labels:
            self._agent_labels[tool_use_id] = label

        content = f"`🔧 {label}`"
        if payload:
            body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            content = f"`🔧 {label}`\n```json\n{body}\n```"

        tracked = self._agent_announcements.get(tool_use_id)
        if tracked is None:
            fresh: list[Message] = []
            self._agent_announcements[tool_use_id] = fresh
            await _render_chunked(self._channel, fresh, content)
            return

        current = "".join(m.content for m in tracked if m.content != _BLANK_CONTENT)
        if payload and current != content:
            await _render_chunked(self._channel, tracked, content)

    # --- Todo ---

    async def _on_todo_update(self, event: TodoUpdate) -> None:
        from axi.discord_ui import format_todo_list, _save_todo_items

        _save_todo_items(self._agent_name, event.todos)
        body = f"**Todo List**\n{format_todo_list(event.todos)}"
        await self._send_system(body)

    # --- Query result ---

    async def _on_query_result(self, event: QueryResult) -> None:
        # The old path stopped typing on the first ResultMessage regardless of
        # flowchart-ness (discord_stream.py:1046, ahead of its flowchart check).
        await self.stop_typing()
        if event.is_flowchart:
            return
        cost = f"${event.cost_usd:.4f}" if event.cost_usd else ""
        duration = f"{event.duration_ms / 1000:.1f}s" if event.duration_ms else ""
        parts = [p for p in [cost, duration] if p]
        if parts:
            suffix = f" ({', '.join(parts)})"
            if self._last_flushed_msg_id and self._last_flushed_channel_id:
                try:
                    new_content = self._last_flushed_content + suffix
                    await _retry_discord_503(
                        config.discord_client.edit_message,
                        self._last_flushed_channel_id,
                        self._last_flushed_msg_id,
                        new_content,
                    )
                except Exception:
                    log.debug("Failed to append timing to last message for '%s'", self._agent_name)

    # --- Compaction ---

    async def _on_compact_start(self, event: CompactStart) -> None:
        label = "Axi-triggered compaction" if event.self_triggered else "Compacting"
        tokens = f" ({event.token_count:,} tokens)" if event.token_count else ""
        await self._send_system(f"\U0001f504 {label}{tokens}...")

    # --- Flowchart ---

    async def _on_flowchart_start(self, event: FlowchartStart) -> None:
        self._in_flowchart = True
        self._suppress_stream = False
        self._fc_command = event.command or None

    async def _on_flowchart_end(self, event: FlowchartEnd) -> None:
        self._in_flowchart = False
        self._suppress_stream = False
        self._fc_command = None

    def _block_output_allowed(self) -> bool:
        """Whether per-block progress should be posted for the running flowchart.

        Mirrors discord_stream.py:1221 — quiet for /soul and /soul-flow (which
        wrap every user message) unless the agent is in verbose mode.
        """
        if self._fc_command not in _FC_QUIET_COMMANDS:
            return True
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state

        session: Any = _agents_mod.agents.get(self._agent_name)
        if session is None:
            return False
        return bool(discord_state(session).verbose)

    async def _on_block_start(self, event: BlockStart) -> None:
        if event.block_type in _SILENT_BLOCK_TYPES:
            return
        self._suppress_stream = event.block_type in ("prompt", "branch", "refresh")
        if not self._block_output_allowed():
            return
        label = f"**{event.block_name}**" if event.block_name else "?"
        block_type = f" (`{event.block_type}`)" if event.block_type else ""
        await self._send_system(f"▶ {label}{block_type}")

    async def _on_block_complete(self, event: BlockComplete) -> None:
        self._suppress_stream = False
        if not event.success and self._block_output_allowed():
            await self._send_system(f"❌ Block **{event.block_name}** failed")

    # --- System notifications ---

    async def _on_system_notification(self, event: SystemNotification) -> None:
        if event.subtype == "task_started":
            data = event.data.get("data", {})
            desc = data.get("description", "")
            if desc:
                await self._send_system(f"\U0001f680 Task started: {desc}")
        elif event.subtype == "task_progress":
            data = event.data.get("data", {})
            content = data.get("content", "")
            if content:
                await self._send_system(content)
        elif event.subtype == "task_notification":
            data = event.data.get("data", {})
            content = data.get("content", "")
            if content:
                await self._send_system(content)
        elif event.subtype == "input_request":
            data = event.data.get("data", {})
            block_id = data.get("block_id", "")
            block_name = data.get("block_name", "input")
            from axi import agents as _agents_mod
            from axi.axi_types import discord_state
            session = _agents_mod.agents.get(self._agent_name)
            if session:
                discord_state(session).pending_input_block_id = block_id
            mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
            await self._send_system(
                f"**{block_name}**: Flowchart is waiting for input. "
                f"Type your response below.\n{mentions}"
            )
            log.info(
                "Input block '%s' (id=%s) waiting for user input for '%s'",
                block_name, block_id, self._agent_name,
            )
        else:
            log.debug(
                "RENDER[%s] unhandled system notification: %s",
                self._agent_name, event.subtype,
            )

    # --- Internal helpers ---

    async def _send_long(self, text: str) -> None:
        from axi.agents import send_long

        msg = await send_long(self._channel, text)
        if msg is not None:
            self._last_flushed_msg_id = str(msg.id)
            self._last_flushed_channel_id = self._channel.id
            self._last_flushed_content = msg.content

    async def _send_system(self, text: str) -> None:
        from axi.discord_wire import audited_channel_send

        try:
            await audited_channel_send(
                self._channel, text, operation="stream_renderer"
            )
        except Exception:
            log.debug("Failed to send system message for '%s'", self._agent_name)

    async def _do_live_edit_tick(self) -> None:
        le = self._live_edit
        if le is None or le.finalized:
            return
        text = self._text_buffer.lstrip()
        if not text:
            return

        now = time.monotonic()

        if le.message_id is None:
            await _live_edit_post(le, text + _STREAMING_CURSOR, _FakeSession(self._agent_name))
            return

        if len(text) > _STREAMING_MSG_LIMIT:
            split_at = text.rfind("\n", 0, _STREAMING_MSG_LIMIT)
            if split_at == -1:
                split_at = _STREAMING_MSG_LIMIT
            final_content = text[:split_at]
            await _live_edit_update(le, final_content, _FakeSession(self._agent_name))
            self._text_buffer = text[split_at:].lstrip("\n")
            le.message_id = None
            le.content = ""
            le.edit_pending = False
            remainder = self._text_buffer.lstrip()
            if remainder:
                await _live_edit_post(le, remainder + _STREAMING_CURSOR, _FakeSession(self._agent_name))
            return

        if now - le.last_edit_time >= config.STREAMING_EDIT_INTERVAL:
            await _live_edit_update(le, text + _STREAMING_CURSOR, _FakeSession(self._agent_name))

    async def _do_live_edit_finalize(self) -> None:
        le = self._live_edit
        if le is None:
            return
        text = self._text_buffer.lstrip()
        if le.message_id is not None and text:
            from discordquery import split_message

            chunks = split_message(text)
            if len(chunks) == 1:
                await _live_edit_update(le, chunks[0], _FakeSession(self._agent_name))
                self._last_flushed_msg_id = le.message_id
                self._last_flushed_channel_id = le.channel_id
                self._last_flushed_content = chunks[0]
            else:
                await _live_edit_update(le, chunks[0], _FakeSession(self._agent_name))
                for chunk in chunks[1:]:
                    await _live_edit_post(le, chunk, _FakeSession(self._agent_name))
                self._last_flushed_msg_id = le.message_id
                self._last_flushed_channel_id = le.channel_id
                self._last_flushed_content = chunks[-1]
        elif le.message_id is None and text:
            await self._send_long(text)

        le.message_id = None
        le.content = ""
        le.finalized = False
        le.edit_pending = False
        self._text_buffer = ""


class _FakeSession:
    """Minimal session-like object for live-edit function signatures."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name
