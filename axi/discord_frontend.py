"""DiscordFrontend — wraps existing Discord code into the Frontend protocol.

Thin adapter: delegates to agents.py, channels.py, discord_stream.py,
discord_ui.py. Allows the FrontendRouter to multiplex Discord alongside
other frontends without changing existing behavior.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from axi import config
from agenthub.frontend import PlanApprovalResult

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

log = logging.getLogger(__name__)


class DiscordFrontend:
    """Frontend adapter for Discord.

    Wraps existing module-level functions (agents.py, channels.py, etc.)
    into the Frontend protocol. This is the first step toward a fully
    self-contained Discord frontend class — for now it delegates everything.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._stream_renderers: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "discord"

    # --- Lifecycle ---

    async def start(self) -> None:
        pass  # Discord bot lifecycle is managed externally

    async def stop(self) -> None:
        pass  # Discord bot lifecycle is managed externally

    # --- Outbound: hub -> frontend ---

    async def post_message(
        self, agent_name: str, text: str, channel_ref: Any = None,
    ) -> None:
        from axi.agents import send_long
        from axi.channels import get_agent_channel

        if channel_ref:
            from axi.discord_wire import audited_channel_send
            channel_id = await self._resolve_channel_id(str(channel_ref))
            ch = self._bot.get_channel(int(channel_id))
            if ch:
                await audited_channel_send(ch, text, operation="frontend.post_message")
            return
        channel = await get_agent_channel(agent_name)
        if channel:
            await send_long(channel, text)

    async def post_system(self, agent_name: str, text: str) -> None:
        from axi.agents import send_system
        from axi.channels import get_agent_channel

        channel = await get_agent_channel(agent_name)
        if channel:
            await send_system(channel, text)

    async def broadcast(self, text: str) -> None:
        from axi.channels import get_master_channel
        from axi.discord_wire import audited_channel_send

        master_ch = await get_master_channel()
        if master_ch:
            await audited_channel_send(master_ch, text, operation="frontend.broadcast")

    async def post_file(
        self, agent_name: str, filename: str, data: bytes, description: str = "",
        channel_ref: Any = None,
    ) -> None:
        import io

        import discord

        from axi.channels import get_agent_channel
        from axi.discord_wire import audited_channel_send

        if channel_ref:
            channel_id = await self._resolve_channel_id(str(channel_ref))
            ch = self._bot.get_channel(int(channel_id))
        else:
            ch = await get_agent_channel(agent_name)
        if ch:
            file = discord.File(io.BytesIO(data), filename=filename)
            await audited_channel_send(
                ch, description, file=file, operation="frontend.post_file"
            )

    async def post_embed(self, agent_name: str, embed_data: dict[str, Any]) -> None:
        pass  # Will delegate to Discord embed rendering in Phase 4

    async def set_typing(self, agent_name: str, is_typing: bool) -> None:
        pass  # Will delegate to channel.typing() in Phase 3

    async def set_status(
        self, agent_name: str, status_text: str, emoji: str | None = None
    ) -> None:
        from axi.channels import schedule_status_update, set_status_override

        set_status_override(agent_name, emoji)
        schedule_status_update()

    async def post_reaction(self, agent_name: str, message_ref: Any, emoji: str) -> None:
        if message_ref is None:
            return
        try:
            await message_ref.add_reaction(emoji)
            log.info("Reaction +%s on message %s", emoji, message_ref.id)
        except Exception as exc:
            log.warning("Reaction +%s failed on message %s: %s", emoji, message_ref.id, exc)

    async def remove_reaction(
        self, agent_name: str, message_ref: Any, emoji: str
    ) -> None:
        if message_ref is None:
            return
        try:
            await message_ref.remove_reaction(emoji, self._bot.user)
            log.info("Reaction -%s on message %s", emoji, message_ref.id)
        except Exception as exc:
            log.warning("Reaction -%s failed on message %s: %s", emoji, message_ref.id, exc)

    # --- Agent lifecycle events ---

    async def on_wake(self, agent_name: str) -> None:
        # Post-wake logic (moved out of agents.wake_agent in 7.4a). Runs for BOTH
        # hub-driven wakes (hub's _ensure_awake broadcasts on_wake) and legacy wakes
        # (wake_agent broadcasts on_wake), closing the 7.2 gap where hub wakes skipped
        # it. resume_id mirrors wake_agent's pre-wake session_id, falling back to
        # last_failed_resume_id so a resume that fell back to fresh is still a resume.
        from axi.agents import _post_model_warning
        from axi.agents import agents as _registry
        from axi.axi_types import discord_state
        from axi.prompts import compute_prompt_hash, post_system_prompt_to_channel

        session = _registry.get(agent_name)
        if session is None:
            log.debug("Discord: on_wake for unknown agent '%s'", agent_name)
            return

        resume_id = session.session_id or session.last_failed_resume_id

        # Prompt-change detection (resumes only)
        prompt_changed = False
        if resume_id and session.system_prompt is not None:
            current_hash = compute_prompt_hash(session.system_prompt)
            if session.system_prompt_hash is not None and current_hash != session.system_prompt_hash:
                prompt_changed = True
                log.info(
                    "System prompt changed for '%s' (old=%s, new=%s)",
                    agent_name,
                    session.system_prompt_hash,
                    current_hash,
                )
            session.system_prompt_hash = current_hash

        # Post the system prompt on first wake
        ds = discord_state(session)
        if not ds.system_prompt_posted and ds.channel_id:
            ds.system_prompt_posted = True
            try:
                await post_system_prompt_to_channel(
                    agent_name,
                    session.system_prompt,
                    is_resume=bool(resume_id),
                    prompt_changed=prompt_changed,
                    session_id=session.session_id or resume_id,
                )
                log.info("on_wake: posted system prompt to '%s' on first wake", agent_name)
            except Exception:
                log.warning("Failed to post system prompt for '%s'", agent_name, exc_info=True)

        await _post_model_warning(session)
        log.debug("Discord: agent '%s' woke", agent_name)

    async def on_sleep(self, agent_name: str) -> None:
        # 7.4c-2: refresh the channel status prefix on sleep. Moved here from
        # agents.sleep_agent so hub.sleep (which broadcasts on_sleep) triggers it.
        from axi.channels import schedule_status_update

        schedule_status_update()
        log.debug("Discord: agent '%s' slept", agent_name)

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        # Channel creation + topic only. Prompt-placeholder substitution and the
        # session routing id (channel_id) are applied generically in the spawn
        # path from spawn_context() below, so a non-Discord frontend fills them
        # too.
        from axi.channels import ensure_agent_channel, format_channel_topic

        channel = await ensure_agent_channel(agent_name, cwd=session.cwd)

        desired_topic = format_channel_topic(
            session.cwd,
            session.session_id,
            getattr(session, "system_prompt_hash", None),
            agent_type=session.agent_type,
        )
        if channel.topic != desired_topic:
            log.info("Updating topic on #%s: %r -> %r", channel.name, channel.topic, desired_topic)

            async def _update_topic(ch: Any, topic: str) -> None:
                try:
                    await ch.edit(topic=topic)
                except Exception:
                    log.warning("Failed to update topic on #%s", ch.name, exc_info=True)

            import asyncio
            asyncio.get_running_loop().create_task(_update_topic(channel, desired_topic))

        log.info("Discord: agent '%s' spawned, channel=#%s (id=%d)", agent_name, channel.name, channel.id)

    async def spawn_context(self, agent_name: str, session: Any) -> dict[str, Any]:
        from axi.channels import ensure_agent_channel, strip_status_prefix

        channel = await ensure_agent_channel(agent_name, cwd=session.cwd)
        return {
            "placeholders": {
                "channel_id": str(channel.id),
                "channel_name": strip_status_prefix(channel.name),
                "guild_id": str(channel.guild.id),
                "guild_name": channel.guild.name,
            },
            "routing_id": channel.id,
        }

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        from axi.channels import move_channel_to_killed

        await move_channel_to_killed(agent_name)
        log.info("Discord: agent '%s' killed (channel moved to Killed)", agent_name)

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        log.debug("Discord: agent '%s' session_id=%s", agent_name, session_id)

    async def on_channel_ready(self, agent_name: str) -> None:
        log.debug("Discord: agent '%s' channel ready", agent_name)

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        pass  # Handled by existing idle check code

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        from axi.channels import get_agent_channel
        from axi.discord_wire import audited_channel_send

        channel = await get_agent_channel(agent_name)
        if channel:
            if was_mid_task:
                await audited_channel_send(channel, "*(reconnected after restart — resuming output)*", operation="frontend.reconnect")
            else:
                await audited_channel_send(channel, "*(reconnected after restart)*", operation="frontend.reconnect")

    # --- Stream rendering ---

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        from agenthub.stream_types import StreamEnd, StreamStart
        from axi.channels import get_agent_channel
        from axi.discord_stream_renderer import DiscordStreamRenderer

        if isinstance(event, StreamStart):
            channel = await get_agent_channel(agent_name)
            if channel:
                self._stream_renderers[agent_name] = DiscordStreamRenderer(
                    agent_name, channel, self._bot,
                )

        renderer = self._stream_renderers.get(agent_name)
        if renderer:
            await renderer.handle(event)

        if isinstance(event, StreamEnd):
            self._stream_renderers.pop(agent_name, None)

    # --- Interactive gates ---

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        import asyncio

        from axi.axi_types import discord_state
        from axi.channels import schedule_status_update

        ds = discord_state(session)
        if ds.channel_id is None:
            return PlanApprovalResult(approved=True)

        channel_id = ds.channel_id
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        header = f"\U0001f4cb **Plan from {agent_name}** — waiting for approval"

        try:
            if plan_content:
                await config.discord_client.send_file(
                    channel_id, "plan.txt", plan_content.encode("utf-8"), content=header,
                )
            else:
                await config.discord_client.send_message(
                    channel_id,
                    f"{header}\n\n*(Plan file not found — the agent should have described the plan in its messages above.)*",
                )

            resp = await config.discord_client.send_message(
                channel_id,
                f"React with ✅ to approve or ❌ to reject, or type feedback to revise the plan. {mentions}",
            )
            approval_msg_id = resp["id"]
            for emoji in ("✅", "❌"):
                await config.discord_client.add_reaction(channel_id, approval_msg_id, emoji)
            ds.plan_approval_message_id = int(approval_msg_id)
        except Exception:
            log.exception("request_plan_approval: failed to post plan for '%s'", agent_name)
            return PlanApprovalResult(approved=False, message="Could not post plan to Discord for approval.")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        ds.plan_approval_future = future  # type: ignore[assignment]
        schedule_status_update()

        try:
            result = await future
        finally:
            ds.plan_approval_future = None
            ds.plan_approval_message_id = None
            schedule_status_update()

        remove_emoji = "❌" if result.get("approved") else "✅"
        try:
            await config.discord_client.remove_reaction(channel_id, approval_msg_id, remove_emoji)
        except Exception:
            log.debug("Failed to remove reaction from plan approval message")

        if result.get("approved"):
            log.info("Agent '%s' plan approved", agent_name)
            return PlanApprovalResult(approved=True)
        else:
            message = result.get("message", "User rejected the plan.")
            log.info("Agent '%s' plan rejected: %s", agent_name, message)
            return PlanApprovalResult(approved=False, message=message if isinstance(message, str) else str(message))

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        import asyncio

        from axi.axi_types import discord_state
        from axi.discord_ui import _format_question_for_discord, _NUMBER_EMOJI

        ds = discord_state(session)
        if ds.channel_id is None:
            return {}

        channel_id = ds.channel_id
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        loop = asyncio.get_running_loop()
        answers: dict[str, str] = {}

        try:
            await config.discord_client.send_message(
                channel_id, f"❓ **{agent_name}** is asking you a question {mentions}",
            )
        except Exception:
            log.exception("ask_question: failed to post header for '%s'", agent_name)
            return {}

        for i, q in enumerate(questions):
            try:
                formatted = _format_question_for_discord(q, i, len(questions))
                msg = await config.discord_client.send_message(channel_id, formatted)
                msg_id = int(msg["id"])
            except Exception:
                log.exception("ask_question: failed to post question %d for '%s'", i, agent_name)
                return answers

            options = q.get("options", [])
            for j in range(min(len(options), len(_NUMBER_EMOJI))):
                try:
                    await config.discord_client.add_reaction(channel_id, msg_id, _NUMBER_EMOJI[j])
                except Exception:
                    log.debug("Failed to add reaction %d to question message", j + 1)

            ds.question_message_id = msg_id
            ds.question_data = q

            future: asyncio.Future[str] = loop.create_future()
            ds.question_future = future

            try:
                answer = await future
            finally:
                ds.question_future = None
                ds.question_message_id = None
                ds.question_data = None

            if not answer:
                break
            answers[q.get("question", "")] = answer

        return answers

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        pass

    async def receive_input(self, agent_name: str) -> str:
        return ""  # Will delegate to Discord message wait in Phase 4

    # --- Discord-specific helpers ---

    _DISCORD_EPOCH_MS = 1420070400000
    _MCP_TEXT_MAX_BYTES = 50 * 1024

    def _resolve_snowflake(self, value: str) -> int:
        from datetime import UTC, datetime

        if value.isdigit():
            return int(value)
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ms = int(dt.timestamp() * 1000)
        return (ms - self._DISCORD_EPOCH_MS) << 22

    async def _resolve_channel_id(self, channel_arg: str) -> str:
        if channel_arg.isdigit():
            return channel_arg
        if ":" in channel_arg:
            guild_id_str, channel_name = channel_arg.split(":", 1)
            if guild_id_str.isdigit():
                ch = await config.discord_client.find_channel(int(guild_id_str), channel_name)
                if ch:
                    return str(ch["id"])
                raise ValueError(f"No text channel named '{channel_name}' in guild {guild_id_str}")
        ch = await config.discord_client.find_channel(config.DISCORD_GUILD_ID, channel_arg)
        if ch:
            return str(ch["id"])
        raise ValueError(f"'{channel_arg}' is not a valid channel ID, guild_id:channel_name pair, or channel name in the home guild")

    async def _agent_channel_id(self, agent_name: str) -> str | None:
        from axi.channels import get_agent_channel

        channel = await get_agent_channel(agent_name)
        return str(channel.id) if channel else None

    # --- Message history ---

    async def read_messages(
        self, agent_name: str, limit: int = 50, before: Any = None,
        after: Any = None, channel_ref: Any = None,
    ) -> list[dict[str, Any]]:
        if channel_ref:
            channel_id = await self._resolve_channel_id(str(channel_ref))
        else:
            channel_id = await self._agent_channel_id(agent_name)
        if not channel_id:
            return []
        params: dict[str, Any] = {}
        if before:
            params["before"] = self._resolve_snowflake(str(before))
        if after:
            params["after"] = self._resolve_snowflake(str(after))
        use_after = "after" in params
        all_messages: list[dict[str, Any]] = []
        collected = 0
        while collected < limit:
            batch_size = min(100, limit - collected)
            batch = await config.discord_client.get_messages(channel_id, limit=batch_size, **params)
            if not batch:
                break
            all_messages.extend(batch)
            collected += len(batch)
            if len(batch) < batch_size:
                break
            if use_after:
                params["after"] = batch[-1]["id"]
            else:
                params["before"] = batch[-1]["id"]
        if not use_after:
            all_messages.reverse()
        result = []
        for msg in all_messages:
            result.append({
                "author": msg.get("author", {}).get("username", "unknown"),
                "content": msg.get("content", ""),
                "timestamp": msg.get("timestamp", ""),
                "id": msg.get("id", ""),
                "channel_id": channel_id,
            })
        return result

    async def search_messages(
        self, query: str, agent_name: str | None = None,
        limit: int = 25, channel_ref: Any = None,
    ) -> list[dict[str, Any]]:
        guild_id = str(config.DISCORD_GUILD_ID)
        params: dict[str, Any] = {"content": query}
        if channel_ref:
            params["channel_id"] = await self._resolve_channel_id(str(channel_ref))
        elif agent_name:
            ch_id = await self._agent_channel_id(agent_name)
            if ch_id:
                params["channel_id"] = ch_id
        params["sort_by"] = "timestamp"
        params["sort_order"] = "desc"
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < limit:
            params["limit"] = min(25, limit - len(results))
            params["offset"] = offset
            data = await config.discord_client.get(f"/guilds/{guild_id}/messages/search", params)
            message_groups = data.get("messages", [])
            if not message_groups:
                break
            for group in message_groups:
                for msg in group:
                    if msg.get("hit"):
                        results.append({
                            "author": msg.get("author", {}).get("username", "unknown"),
                            "content": msg.get("content", ""),
                            "timestamp": msg.get("timestamp", ""),
                            "id": msg.get("id", ""),
                            "channel_id": msg.get("channel_id", ""),
                        })
                        break
            total = data.get("total_results", 0)
            offset += 25
            if offset >= total or offset >= 9975:
                break
        return results[:limit]

    async def wait_for_message(
        self, agent_name: str, timeout: float = 120,
        after: Any = None, channel_ref: Any = None,
    ) -> list[dict[str, Any]]:
        import asyncio
        import time

        if channel_ref:
            channel_id = await self._resolve_channel_id(str(channel_ref))
        else:
            channel_id = await self._agent_channel_id(agent_name)
        if not channel_id:
            return []
        poll_interval = 2.0
        if not after:
            msgs = await config.discord_client.get_messages(channel_id, limit=1)
            if not msgs:
                return []
            after = msgs[0]["id"]
        after_id = str(after)
        deadline = time.monotonic() + timeout
        cursor = after_id
        while time.monotonic() < deadline:
            messages = await config.discord_client.get_messages(channel_id, limit=100, after=after_id)
            if messages:
                cursor = messages[0]["id"]
                matching = []
                for msg in reversed(messages):
                    content = msg.get("content", "")
                    if content.startswith("*System:*"):
                        continue
                    matching.append({
                        "author": msg.get("author", {}).get("username", "unknown"),
                        "content": content,
                        "timestamp": msg.get("timestamp", ""),
                        "id": msg.get("id", ""),
                        "channel_id": channel_id,
                        "cursor": cursor,
                    })
                if matching:
                    return matching
                after_id = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
        return []

    # --- Inbound message processing ---

    _SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    _MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

    async def extract_content(self, message: Any) -> Any:
        """Extract text + image content from a Discord message."""
        import base64

        if not message.content.strip() and message.attachments:
            for a in message.attachments:
                if a.filename == "message.txt" and a.size <= 100_000:
                    try:
                        data = await a.read()
                        message.content = data.decode("utf-8")
                        break
                    except Exception:
                        log.warning("Failed to read message.txt attachment", exc_info=True)

        ts_prefix = message.created_at.strftime("[%Y-%m-%d %H:%M:%S UTC] ")

        image_attachments = [
            a
            for a in message.attachments
            if a.content_type
            and a.content_type.split(";")[0].strip() in self._SUPPORTED_IMAGE_TYPES
            and a.size <= self._MAX_IMAGE_SIZE
        ]

        if not image_attachments:
            return ts_prefix + message.content

        blocks: list[dict[str, Any]] = []
        blocks.append({"type": "text", "text": ts_prefix + (message.content or "")})

        for attachment in image_attachments:
            try:
                data = await attachment.read()
                b64 = base64.b64encode(data).decode("utf-8")
                mime = (attachment.content_type or "application/octet-stream").split(";")[0].strip()
                blocks.append({"type": "image", "data": b64, "mimeType": mime})
            except Exception:
                log.warning("Failed to download attachment %s", attachment.filename, exc_info=True)

        return blocks or message.content

    async def try_resolve_gate(
        self, agent_name: str, content: Any, message: Any
    ) -> bool:
        """Check if a pending gate (plan approval or question) consumes this message.

        Returns True if the message was consumed by a gate, False otherwise.
        """
        import re

        from axi.axi_types import discord_state

        from axi import agents as _agents_mod

        session = _agents_mod.agents.get(agent_name)
        if session is None:
            return False

        ds = discord_state(session)

        # Plan approval gate
        if ds.plan_approval_future is not None and not ds.plan_approval_future.done():
            raw = content.strip() if isinstance(content, str) else ""
            text = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]\s*", "", raw).strip().lower()
            if text in ("approve", "approved", "yes", "y", "lgtm", "go", "proceed", "ok"):
                ds.plan_approval_future.set_result({"approved": True, "message": ""})
                await self.post_reaction(agent_name, message, "✅")
                await self.post_system(agent_name, "Plan approved — agent resuming implementation.")
            elif text in ("reject", "rejected", "no", "n", "cancel", "stop"):
                ds.plan_approval_future.set_result({"approved": False, "message": "User rejected the plan. Please revise."})
                await self.post_reaction(agent_name, message, "❌")
                await self.post_system(agent_name, "Plan rejected — agent will revise.")
            else:
                feedback = content if isinstance(content, str) else str(content)
                ds.plan_approval_future.set_result({"approved": False, "message": f"User wants changes to the plan: {feedback}"})
                await self.post_reaction(agent_name, message, "\U0001f4dd")
                await self.post_system(agent_name, "Feedback received — agent will revise the plan.")
            return True

        # Question gate
        if ds.question_future is not None and not ds.question_future.done():
            from axi.discord_ui import parse_question_answer

            raw = content.strip() if isinstance(content, str) else str(content)
            raw = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]\s*", "", raw).strip()
            q = ds.question_data or {}
            answer = parse_question_answer(raw, q)
            ds.question_future.set_result(answer)
            await self.post_reaction(agent_name, message, "✅")
            return True

        return False

    # --- Event log integration ---

    async def on_log_event(self, event: LogEvent) -> None:
        pass  # Discord doesn't use the event log (yet)
