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
from typing import TYPE_CHECKING, Any, cast

from axi import config
from axi.discord_stream import (
    _agent_context_label,
    _BLANK_CONTENT,
    _LiveEditState,
    _live_edit_post,
    _live_edit_update,
    _render_chunked,
    _retry_discord_503,
    _task_text_preview,
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
    SpawnEnd,
    SpawnStart,
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


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce an untrusted payload field to a dict — task usage blocks are optional."""
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return {}


def _show_output_schema() -> bool:
    """Whether output-schema block text should be shown despite being internal JSON."""
    return os.environ.get("FC_SHOW_OUTPUT_SCHEMA", "").lower() in ("1", "true", "yes")


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
        "_child_deferred",
        "_child_fc_command",
        "_child_suppress",
        "_child_thinking_msg_id",
        "_deferred_msg",
        "_fc_command",
        "_flush_count",
        "_in_flowchart",
        "_last_flushed_channel_id",
        "_last_flushed_content",
        "_last_flushed_msg_id",
        "_live_edit",
        "_saw_error",
        "_stream_started_at",
        "_streaming_enabled",
        "_suppress_stream",
        "_task_last_status",
        "_task_start_messages",
        "_task_status_messages",
        "_text_buffer",
        "_thinking_msg_id",
        "_tool_parents",
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
        # Per-child-session rendering state (flush-based, no live-edit).
        self._child_deferred: dict[str, str] = {}
        self._child_suppress: dict[str, bool] = {}
        self._child_fc_command: dict[str, str] = {}
        self._child_thinking_msg_id: dict[str, str] = {}
        # Fallback wall-clock origin when the session has no query_started.
        self._stream_started_at: float | None = None
        # Top-level Agent tool calls announced this stream, keyed by tool_use_id:
        # the Discord messages backing each announcement, and the short label
        # (also what R7 will need to prefix that subagent's task updates).
        self._agent_announcements: dict[str, list[Message]] = {}
        self._agent_labels: dict[str, str] = {}
        # tool_use_id -> parent_tool_use_id, walked to attribute a task back to
        # the top-level Agent call that owns it.
        self._tool_parents: dict[str, str | None] = {}
        # Per-task Discord messages, edited in place rather than reposted.
        self._task_start_messages: dict[str, list[Message]] = {}
        self._task_status_messages: dict[str, list[Message]] = {}
        self._task_last_status: dict[str, str] = {}
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
            await self._on_thinking_start(event)
        elif isinstance(event, ThinkingEnd):
            await self._on_thinking_end(event)
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
            await self._on_compact_complete(event)
        elif isinstance(event, FlowchartStart):
            await self._on_flowchart_start(event)
        elif isinstance(event, FlowchartEnd):
            await self._on_flowchart_end(event)
        elif isinstance(event, SpawnStart):
            await self._on_spawn_start(event)
        elif isinstance(event, SpawnEnd):
            await self._on_spawn_end(event)
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
        self._stream_started_at = time.monotonic()
        self.start_typing()

    async def _on_stream_end(self, event: StreamEnd) -> None:
        await self.stop_typing()

        if self._deferred_msg:
            await self._send_long(self._deferred_msg)
            self._deferred_msg = ""

        # Drain every child's deferred buffer: a child stream that ends without
        # SpawnEnd (hard kill, StreamKilled, error teardown) must still surface
        # its last chunk — the never-drop contract. This runs before the
        # rate-limit/error return on purpose.
        for child_name, deferred in list(self._child_deferred.items()):
            if deferred:
                await self._send_child(child_name, deferred)
        self._child_deferred.clear()

        # Archive threads whose spawn never completed, AFTER the deferred-text
        # drain so interrupted child output surfaces before the thread closes.
        await self._cleanup_unfinished_spawn_threads()

        # Rate limit / transient error: the old path returned before the ping
        # (discord_stream.py:1455, :1466). Pinging here would summon the user to
        # a turn that produced no answer and no explanation.
        if self._saw_error:
            return

        from axi import stub_model

        if stub_model.suppress_completion_ping():
            return
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        await _retry_discord_503(self._channel.send, mentions)

    # --- Text rendering ---

    async def _on_text_delta(self, event: TextDelta) -> None:
        if event.session and event.session != "main":
            return  # child text arrives via TextFlush (flush-based rendering)
        if self._suppress_stream:
            return
        self._text_buffer += event.text
        if self._streaming_enabled and self._live_edit and not self._in_flowchart:
            await self._do_live_edit_tick()

    async def _on_text_flush(self, event: TextFlush) -> None:
        s = event.session
        if s and s != "main":
            if self._child_suppress.get(s):
                self._child_deferred.pop(s, None)
                return
            text = event.text
            if not text.strip():
                return
            deferred = self._child_deferred.get(s, "")
            if deferred:
                await self._send_child(s, deferred)
            self._child_deferred[s] = text.lstrip()
            return
        # Suppressed blocks must drop their flush too, not just their deltas —
        # the old path skipped _flush_text entirely (discord_stream.py:1229-1232).
        # Without this the internal JSON still lands in the channel at block end.
        if self._suppress_stream:
            self._text_buffer = ""
            return
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

    async def _on_thinking_start(self, event: ThinkingStart) -> None:
        s = event.session
        if s and s != "main":
            target = self._resolve_target(s)
            if target is None:
                return
            try:
                resp = await _retry_discord_503(
                    config.discord_client.send_message,
                    target.id,
                    "*thinking...*",
                )
                self._child_thinking_msg_id[s] = resp["id"]
            except Exception:
                log.debug("Failed to post thinking indicator for '%s'", self._agent_name)
            return
        try:
            resp = await _retry_discord_503(
                config.discord_client.send_message,
                self._channel.id,
                "*thinking...*",
            )
            self._thinking_msg_id = resp["id"]
        except Exception:
            log.debug("Failed to post thinking indicator for '%s'", self._agent_name)

    async def _on_thinking_end(self, event: ThinkingEnd) -> None:
        s = event.session
        if s and s != "main":
            msg_id = self._child_thinking_msg_id.pop(s, None)
            if msg_id:
                target = self._resolve_target(s)
                if target is not None:
                    try:
                        await _retry_discord_503(
                            config.discord_client.delete_message,
                            target.id,
                            msg_id,
                        )
                    except Exception:
                        log.debug("Failed to delete thinking indicator for '%s'", self._agent_name)
            # Verbose mode attaches the full thinking text as a file in the thread.
            thinking = (event.thinking_text or "").strip()
            if thinking and self._verbose() and self._resolve_target(s) is not None:
                await self._post_verbose_file(thinking, s)
            return
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

        # Verbose mode attaches the full thinking text as a file rather than
        # dumping it inline (discord_stream.py:879-889).
        thinking = (event.thinking_text or "").strip()
        if thinking and self._verbose():
            await self._post_verbose_file(thinking)

    # --- Tool use ---

    async def _on_tool_use_start(self, event: ToolUseStart) -> None:
        s = event.session
        if s and s != "main":
            if event.tool_use_id:
                self._tool_parents[event.tool_use_id] = event.parent_tool_use_id
            await self._announce_agent_tool_use(event.tool_name, event, None, s)
            return
        log.debug("RENDER[%s] tool_use_start: %s", self._agent_name, event.tool_name)
        if event.tool_use_id:
            self._tool_parents[event.tool_use_id] = event.parent_tool_use_id
        # The input has not streamed in yet, so this posts the bare label and
        # _on_tool_use_end fills in the JSON once it is complete.
        await self._announce_agent_tool_use(event.tool_name, event, None)

    async def _on_tool_use_end(self, event: ToolUseEnd) -> None:
        s = event.session
        if s and s != "main":
            await self._announce_agent_tool_use(event.tool_name, event, event.tool_input, s)
            if event.tool_name and self._verbose():
                preview = f": {event.preview[:120]}" if event.preview else ""
                await self._send_system(f"`🔧 {event.tool_name}{preview}`", s)
            return
        if event.preview:
            log.debug(
                "RENDER[%s] tool_use_end: %s -> %s",
                self._agent_name, event.tool_name, event.preview[:80],
            )
        await self._announce_agent_tool_use(event.tool_name, event, event.tool_input)

        # Verbose mode narrates each tool as it runs (discord_stream.py:890-905).
        if event.tool_name and self._verbose():
            preview = f": {event.preview[:120]}" if event.preview else ""
            await self._send_system(f"`🔧 {event.tool_name}{preview}`")

    async def _post_verbose_file(self, thinking: str, session: str = "") -> None:
        """Attach thinking text as thinking.md, as the old verbose path did."""
        import io

        import discord

        from axi.discord_wire import audited_channel_send

        try:
            await audited_channel_send(
                self._resolve_target(session) or self._channel,
                "\U0001f4ad",
                file=discord.File(io.BytesIO(thinking.encode("utf-8")), filename="thinking.md"),
                retry_fn=_retry_discord_503,
                operation="stream.thinking_file",
            )
        except Exception:
            log.debug("Failed to post thinking file for '%s'", self._agent_name)

    async def _announce_agent_tool_use(
        self, tool_name: str, event: Any, tool_input: dict[str, Any] | None,
        session: str = "",
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
            await _render_chunked(self._resolve_target(session) or self._channel, fresh, content)
            return

        current = "".join(m.content for m in tracked if m.content != _BLANK_CONTENT)
        if payload and current != content:
            await _render_chunked(self._resolve_target(session) or self._channel, tracked, content)

    # --- Todo ---

    async def _on_todo_update(self, event: TodoUpdate) -> None:
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        from axi.discord_ui import format_todo_list, _save_todo_items

        _save_todo_items(self._agent_name, event.todos)
        # Keep the in-memory copy in step with the file. main.py renders
        # discord_state(session).todo_items, which otherwise stays stale until
        # the next wake reloads it from disk (old path: discord_ui.py:439).
        session: Any = _agents_mod.agents.get(self._agent_name)
        if session is not None:
            discord_state(session).todo_items = event.todos
        body = f"**Todo List**\n{format_todo_list(event.todos)}"
        await self._send_system(body)

    # --- Query result ---

    def _elapsed_seconds(self) -> float | None:
        """Wall-clock seconds since the turn was submitted.

        The old path measured from session.activity.query_started
        (discord_stream.py:1505), i.e. what the user actually waited, rather
        than QueryResult.duration_ms which is model time only.
        """
        from datetime import UTC, datetime

        from axi import agents as _agents_mod

        session: Any = _agents_mod.agents.get(self._agent_name)
        started = getattr(getattr(session, "activity", None), "query_started", None)
        if started is not None:
            return (datetime.now(UTC) - started).total_seconds()
        if self._stream_started_at is not None:
            return time.monotonic() - self._stream_started_at
        return None

    async def _on_query_result(self, event: QueryResult) -> None:
        # The old path stopped typing on the first ResultMessage regardless of
        # flowchart-ness (discord_stream.py:1046, ahead of its flowchart check).
        await self.stop_typing()
        if event.is_flowchart:
            return

        from axi.agents import get_active_trace_tag

        elapsed = self._elapsed_seconds()
        parts: list[str] = []
        if elapsed is not None:
            parts.append(f"{elapsed:.1f}s")
        if event.cost_usd:
            parts.append(f"${event.cost_usd:.4f}")
        if not parts:
            return

        tag = get_active_trace_tag(self._agent_name)
        # Discord subtext on its own line, as before (discord_stream.py:1508).
        suffix = f"\n-# {' · '.join(parts)}{' ' + tag if tag else ''}"

        # Same three placements as the old path (:1510-1528).
        if self._deferred_msg:
            self._deferred_msg += suffix
            return
        if self._last_flushed_msg_id and self._last_flushed_channel_id:
            try:
                await _retry_discord_503(
                    config.discord_client.edit_message,
                    self._last_flushed_channel_id,
                    self._last_flushed_msg_id,
                    self._last_flushed_content + suffix,
                )
                return
            except Exception:
                log.warning(
                    "Failed to edit last message to append timing for '%s'",
                    self._agent_name, exc_info=True,
                )
        # Nothing to attach it to, or the edit failed — post it rather than
        # dropping it silently, which is what the refactor did.
        await self._send_system(suffix.lstrip("\n"))

    # --- Compaction ---

    async def _on_compact_complete(self, event: CompactComplete) -> None:
        """Announce a compaction that carries no pre_tokens.

        With pre_tokens the hub defers to AxiTurnHooks, which posts a richer
        summary once post-compaction token counts land (_handle_pending_compact).
        Without them nothing was recorded, so nothing was ever posted and the
        compaction looked like it never happened (old path: :1197-1198).
        """
        if event.pre_tokens:
            return
        await self._send_system("\U0001f504 Context compacted")

    async def _on_compact_start(self, event: CompactStart) -> None:
        label = "Axi-triggered compaction" if event.self_triggered else "Compacting"
        tokens = f" ({event.token_count:,} tokens)" if event.token_count else ""
        await self._send_system(f"\U0001f504 {label}{tokens}...")

    # --- Flowchart ---

    async def _on_flowchart_start(self, event: FlowchartStart) -> None:
        s = event.session
        if s and s != "main":
            # Child flowchart: record the command so the child's block lines are
            # quiet-gated (_child_fc_command drives _on_block_start's check).
            # Never touch the parent's _in_flowchart/_fc_command state.
            self._child_fc_command[s] = event.command or ""
            return
        self._in_flowchart = True
        self._suppress_stream = False
        self._fc_command = event.command or None

    async def _on_flowchart_end(self, event: FlowchartEnd) -> None:
        s = event.session
        if s and s != "main":
            self._child_fc_command.pop(s, None)
            # Completion summary routes into the child's thread (full-stream
            # fidelity — the child's flowchart renders where its text renders).
            # The *System:* prefix is load-bearing for sentinel consumers,
            # mirroring the parent path below.
            duration_s = event.duration_ms / 1000
            status = "**completed**" if event.status == "completed" else "**failed**"
            await self._send_child(
                s,
                f"*System:* Flowchart {status} in {duration_s:.0f}s "
                f"| Cost: ${event.cost_usd:.4f} | Blocks: {event.blocks_executed}",
            )
            test_sentinel = os.environ.get("AXI_TEST_SENTINEL")
            if test_sentinel:
                await self._send_child(s, test_sentinel)
            return
        self._in_flowchart = False
        self._suppress_stream = False
        self._fc_command = None
        # Post the completion sentinel — mirrors the legacy discord_stream.py:1281-1287
        # that this renderer migration dropped. The gaia-testbench `axi` adapter and
        # axi's own `axi_test.py --wait-mode sentinel` both key on a message that
        # starts with `*System:*` and contains "Flowchart **completed**"
        # (axi_test.py:82,884), so the `*System:*` prefix is load-bearing here.
        duration_s = event.duration_ms / 1000
        status = "**completed**" if event.status == "completed" else "**failed**"
        await self._send_long(
            f"*System:* Flowchart {status} in {duration_s:.0f}s "
            f"| Cost: ${event.cost_usd:.4f} | Blocks: {event.blocks_executed}"
        )
        test_sentinel = os.environ.get("AXI_TEST_SENTINEL")
        if test_sentinel:
            await self._send_long(test_sentinel)

    def _block_output_allowed(self) -> bool:
        """Whether per-block progress should be posted for the running flowchart.

        Mirrors discord_stream.py:1221 — quiet for /soul and /soul-flow (which
        wrap every user message) unless the agent is in verbose mode.
        """
        if self._fc_command not in _FC_QUIET_COMMANDS:
            return True
        return self._verbose()

    def _verbose(self) -> bool:
        """Whether /verbose is on for this agent."""
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state

        session: Any = _agents_mod.agents.get(self._agent_name)
        if session is None:
            return False
        return bool(discord_state(session).verbose)

    async def _on_block_start(self, event: BlockStart) -> None:
        s = event.session
        if s and s != "main":
            if event.block_type in _SILENT_BLOCK_TYPES:
                return
            self._child_suppress[s] = event.has_output_schema and not _show_output_schema()
            if self._child_fc_command.get(s) in _FC_QUIET_COMMANDS and not self._verbose():
                return
            label = f"**{event.block_name}**" if event.block_name else "?"
            block_type = f" (`{event.block_type}`)" if event.block_type else ""
            await self._send_system(f"▶ {label}{block_type}", s)
            return
        if event.block_type in _SILENT_BLOCK_TYPES:
            return
        # A block with an output schema emits JSON for internal branching, not
        # prose for the user (discord_stream.py:1206-1210). Keying this on
        # block_type instead let that JSON reach the channel.
        self._suppress_stream = event.has_output_schema and not _show_output_schema()
        if not self._block_output_allowed():
            return
        label = f"**{event.block_name}**" if event.block_name else "?"
        block_type = f" (`{event.block_type}`)" if event.block_type else ""
        await self._send_system(f"▶ {label}{block_type}")

    async def _on_block_complete(self, event: BlockComplete) -> None:
        s = event.session
        if s and s != "main":
            self._child_suppress.pop(s, None)
            if not event.success:
                await self._send_system(
                    f"❌ Block **{event.block_name}** failed", s
                )
            return
        self._suppress_stream = False
        if not event.success and self._block_output_allowed():
            await self._send_system(f"❌ Block **{event.block_name}** failed")

    # --- Subagent task rendering ---

    def _task_label_prefix(self, tool_use_id: str | None) -> str:
        """'[Agent label] ' when this task belongs to a top-level Agent call.

        Walks tool_use_id -> parent until it hits one that was announced
        (discord_stream.py:_resolve_parent_agent_tool_use_id).
        """
        current = tool_use_id
        seen: set[str] = set()
        while current and current not in seen:
            if current in self._agent_labels:
                return f"[{self._agent_labels[current]}] "
            seen.add(current)
            current = self._tool_parents.get(current)
        return ""

    async def _upsert_task_status(self, task_id: str, content: str) -> None:
        """Edit this task's status message in place instead of posting a new one.

        Port of discord_stream.py:470-486. Without the dedup + edit, a long
        subagent emits one fresh Discord message per progress tick.
        """
        if self._task_last_status.get(task_id) == content:
            return
        tracked = self._task_status_messages.setdefault(task_id, [])
        await _render_chunked(self._channel, tracked, content)
        self._task_last_status[task_id] = content

    async def _on_task_started(self, data: dict[str, Any]) -> None:
        task_id = str(data.get("task_id") or "")
        if not task_id or task_id in self._task_start_messages:
            return
        tool_use_id = data.get("tool_use_id") or "?"
        content = (
            f"`🔧 {self._task_label_prefix(tool_use_id)}"
            f"{data.get('task_type') or 'unknown'} — "
            f"{_task_text_preview(data.get('description') or 'subagent')} "
            f"({tool_use_id})`\nStarted task `{task_id}`"
        )
        tracked: list[Message] = []
        self._task_start_messages[task_id] = tracked
        await _render_chunked(self._channel, tracked, content)

    async def _on_task_progress(self, data: dict[str, Any]) -> None:
        task_id = str(data.get("task_id") or "")
        if not task_id:
            return
        tool_use_id = data.get("tool_use_id") or "?"
        usage = _as_dict(data.get("usage"))
        duration_ms = usage.get("duration_ms", 0)
        duration_s = duration_ms / 1000 if isinstance(duration_ms, (int, float)) else 0
        label = (
            f"{self._task_label_prefix(tool_use_id)}task progress — "
            f"{_task_text_preview(data.get('description') or 'subagent')} ({tool_use_id})"
        )
        await self._upsert_task_status(
            task_id,
            f"`🔧 {label}`\nTask `{task_id}` | {usage.get('tool_uses', 0)} tools | "
            f"{usage.get('total_tokens', 0)} tokens | {duration_s:.1f}s | "
            f"last tool: `{data.get('last_tool_name') or '?'}`",
        )

    async def _on_task_notification(self, data: dict[str, Any]) -> None:
        task_id = str(data.get("task_id") or "")
        if not task_id:
            return
        tool_use_id = data.get("tool_use_id") or "?"
        usage = _as_dict(data.get("usage"))
        details: list[str] = []
        tool_uses = usage.get("tool_uses")
        if tool_uses is not None:
            details.append(f"tools={tool_uses}")
        duration_ms = usage.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            details.append(f"duration={duration_ms / 1000:.1f}s")
        output_file = data.get("output_file")
        if output_file:
            details.append(f"output=`{output_file}`")

        label = (
            f"{self._task_label_prefix(tool_use_id)}"
            f"task {data.get('status') or 'unknown'} ({task_id})"
        )
        content = f"`🔧 {label}`"
        summary = data.get("summary")
        if summary:
            content += f"\n{_task_text_preview(summary, limit=240)}"
        if details:
            content += f"\n{' | '.join(details)}"
        await self._upsert_task_status(task_id, content)

    # --- Spawn lifecycle ---

    async def _on_spawn_start(self, event: SpawnStart) -> None:
        if not config.FC_SPAWN_THREADS:
            return
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None or self._bot is None:
            await self._send_system(f"▶ spawned **{event.agent_name}**", event.session)
            return
        ds = discord_state(session)
        if event.agent_name in ds.spawn_threads:
            return  # duplicate spawn_start — already handled
        thread_name = event.agent_name
        parent_name = event.parent_session or "main"
        if parent_name != "main":
            parent_id = ds.spawn_threads.get(parent_name)
            parent_thread = self._bot.get_channel(parent_id) if parent_id else None
            if parent_thread is not None:
                thread_name = f"{parent_thread.name}/{event.agent_name}"
        try:
            thread = await self._channel.create_thread(
                name=thread_name, auto_archive_duration=60
            )
        except Exception as e:
            log.warning(
                "Failed to create thread for spawned '%s': %s",
                event.agent_name, e,
            )
            await self._send_system(f"▶ spawned **{event.agent_name}** (no thread)", event.session)
            return
        ds.spawn_threads[event.agent_name] = thread.id
        # Status line routes to the emitting (parent) session: the parent
        # channel for top-level spawns, the parent's thread for nested ones.
        await self._send_system(f"▶ spawned **{event.agent_name}** → {thread.jump_url}", event.session)
        await self._send_child(event.agent_name, f"▶ Spawned agent **{event.agent_name}**")
        details = []
        if event.command_name:
            details.append(f"running `{event.command_name}`")
        if event.model:
            details.append(f"model `{event.model}`")
        if event.backend:
            details.append(f"backend `{event.backend}`")
        if details:
            await self._send_child(event.agent_name, " | ".join(details))

    async def _on_spawn_end(self, event: SpawnEnd) -> None:
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None:
            return
        ds = discord_state(session)
        # Drain any deferred child text FIRST — it must surface even when the
        # thread is gone or was never created (fallback prefix applies).
        deferred = self._child_deferred.pop(event.agent_name, "")
        if deferred:
            await self._send_child(event.agent_name, deferred)
        thread_id = ds.spawn_threads.get(event.agent_name)
        if not thread_id:
            return  # no thread (creation failed, or FC_SPAWN_THREADS=0)
        thread = self._bot.get_channel(thread_id) if self._bot else None
        if thread is not None:
            status = {
                "completed": "**completed**",
                "failed": "**failed**",
                "cancelled": "**cancelled**",
            }.get(event.status, "**finished**")
            summary = f"Spawn {status}"
            parts = []
            if event.duration_ms:
                parts.append(f"{event.duration_ms / 1000:.1f}s")
            if event.cost_usd:
                parts.append(f"${event.cost_usd:.4f}")
            if parts:
                summary += f" | {' | '.join(parts)}"
            await self._send_child(event.agent_name, summary)
        else:
            log.warning("Spawn thread gone for '%s' (id=%s)", event.agent_name, thread_id)
        ds.spawn_threads.pop(event.agent_name, None)
        await self._send_system(
            f"spawned **{event.agent_name}** {event.status or 'finished'}", event.session
        )
        ds.pending_archives[event.agent_name] = asyncio.create_task(
            self._archive_after(thread_id, event.agent_name, config.FC_THREAD_GRACE_SECS)
        )

    async def _archive_after(self, thread_id: int, agent_name: str, delay: float) -> None:
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        try:
            await asyncio.sleep(delay)
            thread = self._bot.get_channel(thread_id) if self._bot else None
            if thread is not None:
                await thread.archive()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Failed to archive thread for '%s': %s", agent_name, e)
        finally:
            session = _agents_mod.agents.get(self._agent_name)
            if session is not None:
                discord_state(session).pending_archives.pop(agent_name, None)

    async def _cleanup_unfinished_spawn_threads(self) -> None:
        """Archive threads whose spawn never completed (hard kill / no event).

        Only threads still in spawn_threads — spawns that completed are in
        pending_archives and left to their grace-delay task.
        """
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None:
            return
        ds = discord_state(session)
        for agent_name, thread_id in list(ds.spawn_threads.items()):
            thread = self._bot.get_channel(thread_id) if self._bot else None
            if thread is None:
                ds.spawn_threads.pop(agent_name, None)
                continue
            try:
                await self._send_child(agent_name, "Spawn **interrupted** — stream ended")
                await thread.archive()
            except Exception as e:
                log.warning("Failed to archive interrupted thread for '%s': %s", agent_name, e)
            ds.spawn_threads.pop(agent_name, None)

    # --- System notifications ---

    async def _on_system_notification(self, event: SystemNotification) -> None:
        # NOTE: task_* payloads sit at the TOP level of event.data, unlike
        # block_*/flowchart_* which nest under a "data" key. The refactor read
        # the nested shape for both, which is why it invented a "content" key
        # the producer never sets — see discord_stream.py:1100-1171 vs :1209+.
        if event.subtype == "task_started":
            await self._on_task_started(event.data)
        elif event.subtype == "task_progress":
            await self._on_task_progress(event.data)
        elif event.subtype == "task_notification":
            await self._on_task_notification(event.data)
        elif event.subtype == "block_timeout":
            # A per-block timeout kills the CLI and halts the flowchart; without
            # this the block just stops and looks like nothing happened.
            data = event.data.get("data", {})
            elapsed_ms = data.get("elapsed_ms", 0)
            elapsed_s = elapsed_ms / 1000 if isinstance(elapsed_ms, (int, float)) else 0
            await self._send_system(
                f"⏱️ Block **{data.get('block_name', '?')}** "
                f"(`{data.get('block_type', '?')}`) timed out after "
                f"{elapsed_s:.0f}s (limit: {data.get('timeout_seconds', 0)}s)"
            )
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

    def _resolve_target(self, session_name: str):
        """Thread for a child session, the channel for parent, None = fallback.

        Fallback (None) means: render into the channel with a [agent] prefix.
        When FC_SPAWN_THREADS is off, children render into the channel
        unlabeled — exactly the pre-feature behavior.
        """
        if not session_name or session_name == "main":
            return self._channel
        if not config.FC_SPAWN_THREADS:
            return self._channel
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None or self._bot is None:
            return None
        thread_id = discord_state(session).spawn_threads.get(session_name)
        if not thread_id:
            return None
        return self._bot.get_channel(thread_id)

    async def _send_long(self, text: str, session: str = "") -> None:
        from axi.agents import send_long

        target = self._resolve_target(session)
        if target is None:
            msg = await send_long(self._channel, f"[{session}] {text}")
        else:
            msg = await send_long(target, text)
        # _last_flushed_* feeds _on_query_result's timing-suffix edit, which is
        # parent-channel-only. A child's thread message must never overwrite it:
        # if a child drain is the last send before the parent's QueryResult, the
        # suffix would try to edit the child's thread and fail (posting a stray
        # timing line in the parent channel). Record parent sends only.
        if msg is not None and not session:
            self._last_flushed_msg_id = str(msg.id)
            self._last_flushed_channel_id = self._channel.id
            self._last_flushed_content = msg.content

    async def _send_system(self, text: str, session: str = "") -> None:
        from axi.discord_wire import audited_channel_send

        target = self._resolve_target(session)
        if target is None:
            target = self._channel
            text = f"[{session}] {text}"
        try:
            await audited_channel_send(
                target, text, operation="stream_renderer"
            )
        except Exception:
            log.debug("Failed to send system message for '%s'", self._agent_name)

    async def _send_child(self, session_name: str, text: str) -> None:
        """Flush buffered child text into its thread (or prefixed fallback)."""
        from axi.agents import send_long

        target = self._resolve_target(session_name)
        if target is None:
            await send_long(self._channel, f"[{session_name}] {text}")
            return
        await send_long(target, text)

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
