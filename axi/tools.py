"""MCP tool definitions and server assembly for the Axi bot.

All @tool-decorated functions and MCP server objects live here.
This module imports from agents.py (one-way: agents.py does NOT import tools.py).
"""

from __future__ import annotations

__all__ = [
    "axi_master_mcp_server",
    "axi_mcp_server",
    "discord_mcp_server",
    "utils_mcp_server",
]

import asyncio
import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

import arrow
from claude_agent_sdk import create_sdk_mcp_server, tool
from opentelemetry import trace

from axi import agents, channels, config, worktrees
from axi.discord_stream import get_streaming_agent
from axi.log_context import set_agent_context, set_trigger

_MCP_TEXT_MAX_BYTES = 50 * 1024


def _truncate_mcp_text(text: str, total_messages: int) -> str:
    encoded = text.encode()
    if len(encoded) <= _MCP_TEXT_MAX_BYTES:
        return text
    body_budget = _MCP_TEXT_MAX_BYTES - 256
    kept = encoded[-body_budget:].decode("utf-8", errors="replace")
    newline = kept.find("\n")
    if newline != -1:
        kept = kept[newline + 1:]
    kept_lines = kept.count("\n") + 1
    notice = f"[Truncated: showing {kept_lines} of {total_messages} messages. Use 'before' parameter to paginate for older messages.]\n"
    return notice + kept


def _get_router():
    from axi.hub_wiring import router
    return router


def _get_caller() -> str | None:
    return get_streaming_agent()


if TYPE_CHECKING:
    from axi.axi_types import McpArgs, McpResult

log = logging.getLogger("axi")
_tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Master agent tools
# ---------------------------------------------------------------------------


@tool(
    "axi_spawn_agent",
    "Spawn a new Axi agent session with its own Discord channel. Returns immediately with success/error message.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique short name, no spaces (e.g. 'feature-auth', 'fix-bug-123')",
            },
            "cwd": {
                "type": "string",
                "description": "Absolute path to the working directory for the agent. Defaults to a per-agent subdirectory under user data (agents/<name>/).",
            },
            "prompt": {"type": "string", "description": "Initial task instructions for the agent"},
            "resume": {"type": "string", "description": "Optional session ID to resume a previous agent session"},
            "command": {
                "type": "string",
                "description": "FlowCoder command name (only used when AXI_HARNESS=flowcoder)",
            },
            "command_args": {
                "type": "string",
                "description": "Arguments for the FlowCoder command (shell-style string, only used when AXI_HARNESS=flowcoder)",
            },
            "extensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of extension names to load into this agent's system prompt. Defaults to DEFAULT_EXTENSIONS. Pass [] to disable extensions.",
            },
            "compact_instructions": {
                "type": "string",
                "description": "Instructions for what to preserve during context compaction (e.g. 'always preserve the bug description, current fix approach, and test results')",
            },
            "no_worktree": {
                "type": "boolean",
                "description": "Skip auto-worktree creation and use cwd directly (default: false)",
            },
            "mcp_servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of custom MCP server names (from mcp_servers.json) to attach to this agent (e.g. ['todoist']).",
            },
            "excluded_commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extra commands to exclude from bash sandbox (merged with base set). E.g. ['ssh', 'docker'].",
            },
            "write_dirs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extra directories to add to sandbox write allowlist (~ expanded). E.g. ['~/.config/dynamic-radio'].",
            },
            "model": {
                "type": "string",
                "description": "Optional model override for this agent. Leave unset to use the default/global model. Only set this when the user explicitly requests a specific model.",
            },
        },
        "required": ["name", "prompt"],
    },
)
async def axi_spawn_agent(args: McpArgs) -> McpResult:
    agent_name = args.get("name", "").strip()
    set_agent_context(agent_name or "unknown")
    set_trigger("mcp_tool", detail="axi_spawn_agent")
    _tracer.start_span("tool.axi_spawn_agent", attributes={"agent.name": agent_name}).end()
    agent_prompt = args.get("prompt", "")
    agent_resume = args.get("resume")
    agent_type = config.get_default_agent_type()
    fc_command = args.get("command", "")
    fc_command_args = args.get("command_args", "")
    agent_extensions = args.get("extensions")  # None = use defaults, [] = no extensions
    no_worktree = args.get("no_worktree", False)
    compact_instructions = args.get("compact_instructions")
    mcp_server_names: list[str] = args.get("mcp_servers") or []
    excluded_commands: list[str] = args.get("excluded_commands") or []
    write_dirs: list[str] = [os.path.expanduser(d) for d in (args.get("write_dirs") or [])]
    agent_model: str | None = args.get("model")
    if agent_model is not None:
        agent_model = config.normalize_model(agent_model)
        error = config.validate_model(agent_model)
        if error:
            return {"content": [{"type": "text", "text": f"Error: {error}"}], "is_error": True}

    # Respawn detection: if a channel already exists for this agent, it's a
    # respawn (kill + re-create).  Default cwd to the previous session's cwd
    # (stored in channel topic) and default no_worktree=True (the agent is
    # replacing itself, not competing with another agent for the same cwd).
    # Search killed categories too — killed agents' channels live there.
    is_respawn = False
    previous_cwd: str | None = None
    if agent_name:
        existing_channel = await channels.get_agent_channel(
            agent_name, include_killed=True
        )
        if existing_channel is not None:
            is_respawn = True
            prev_cwd, _, _, _ = channels.parse_channel_topic(existing_channel.topic)
            if prev_cwd:
                previous_cwd = prev_cwd

    if is_respawn and "no_worktree" not in args:
        no_worktree = True

    cwd_explicit = "cwd" in args
    if cwd_explicit:
        agent_cwd = os.path.realpath(os.path.expanduser(args["cwd"]))
    elif previous_cwd:
        agent_cwd = previous_cwd
    else:
        default_cwd = os.path.join(config.AXI_USER_DATA, "agents", agent_name) if agent_name else config.AXI_USER_DATA
        agent_cwd = os.path.realpath(os.path.expanduser(default_cwd))

    # Resolve custom MCP servers from config
    extra_mcp_servers = config.load_mcp_servers(mcp_server_names) if mcp_server_names else None

    # --- Validate name/agent before any side effects ---
    if not agent_name:
        return {
            "content": [{"type": "text", "text": "Error: 'name' is required and cannot be empty."}],
            "is_error": True,
        }
    if agent_name == config.MASTER_AGENT_NAME:
        return {
            "content": [
                {"type": "text", "text": f"Error: cannot spawn agent with reserved name '{config.MASTER_AGENT_NAME}'."}
            ],
            "is_error": True,
        }
    if agent_name in agents.agents and not agent_resume:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error: agent '{agent_name}' already exists. Kill it first or use 'resume' to replace it.",
                }
            ],
            "is_error": True,
        }

    if agent_type == "flowcoder" and not config.FLOWCODER_ENABLED:
        return {
            "content": [{"type": "text", "text": "Error: flowcoder integration is disabled."}],
            "is_error": True,
        }
    if agent_type != "flowcoder" and (fc_command or fc_command_args):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: command and command_args are only supported when AXI_HARNESS=flowcoder.",
                }
            ],
            "is_error": True,
        }

    # Auto-create worktree when cwd is a git repo and another agent
    # already uses the same cwd (prevents concurrent edits to the same tree).
    if not no_worktree and agent_name and worktrees.is_git_repo(agent_cwd):
        cwd_conflict = any(
            s.cwd == agent_cwd
            for name, s in agents.agents.items()
            if name != agent_name
        )
        if cwd_conflict:
            worktree_path = worktrees.create_worktree(agent_name, source_repo=agent_cwd)
            if worktree_path:
                agent_cwd = worktree_path
                log.info("Auto-created worktree for '%s' at %s (cwd conflict)", agent_name, worktree_path)
            else:
                log.warning("Failed to create worktree for '%s', using original cwd", agent_name)

    # Use global ALLOWED_CWDS (which includes ALLOWED_CWDS and ADMIN_ALLOWED_CWDS from .env)
    if not any(agent_cwd == d or agent_cwd.startswith(d + os.sep) for d in config.ALLOWED_CWDS):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: cwd is not in allowed directories. Check ALLOWED_CWDS or ADMIN_ALLOWED_CWDS in .env.",
                }
            ],
            "is_error": True,
        }

    async def _do_spawn() -> None:
        try:
            if agent_name in agents.agents and agent_resume:
                await agents.reclaim_agent_name(agent_name)
            await agents.spawn_agent(
                agent_name,
                agent_cwd,
                agent_prompt,
                resume=agent_resume,
                agent_type=agent_type,
                command=fc_command,
                command_args=fc_command_args,
                extensions=agent_extensions,
                compact_instructions=compact_instructions,
                extra_mcp_servers=extra_mcp_servers,
                excluded_commands=excluded_commands,
                write_dirs=write_dirs,
                model=agent_model,
            )
        except Exception:
            channels.bot_creating_channels.discard(agents.normalize_channel_name(agent_name))
            log.exception("Error in background spawn of agent '%s'", agent_name)
            try:
                channel = await agents.get_agent_channel(agent_name)
                if channel:
                    await agents.send_system(
                        channel, f"Failed to spawn agent **{agent_name}**. Check logs for details."
                    )
            except Exception:
                pass

    if is_respawn:
        log.info(
            "Respawn detected for '%s': no_worktree=%s, cwd_source=%s, cwd=%s",
            agent_name,
            no_worktree,
            "explicit" if cwd_explicit else ("previous" if previous_cwd else "default"),
            agent_cwd,
        )
    log.info(
        "Spawning agent '%s' via MCP tool (type=%s, cwd=%s, resume=%s, extensions=%s)",
        agent_name,
        agent_type,
        agent_cwd,
        agent_resume,
        agent_extensions,
    )
    # Guard against on_guild_channel_create race: mark channel as bot-created
    # BEFORE the background task runs, so the guard is already set when the
    # gateway event fires.  spawn_agent will discard it after agents[name] is set.
    channels.bot_creating_channels.add(agents.normalize_channel_name(agent_name))
    agents.fire_and_forget(_do_spawn())
    return {
        "content": [
            {
                "type": "text",
                "text": f"Agent '{agent_name}' ({agent_type}) spawn initiated in {agent_cwd}. The agent's channel will be notified when it's ready.",
            }
        ]
    }


@tool(
    "axi_kill_agent",
    "Kill an Axi agent session and move its Discord channel to the Killed category. "
    "Returns the session ID (for resuming later) or an error message.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the agent to kill"},
        },
        "required": ["name"],
    },
)
async def axi_kill_agent(args: McpArgs) -> McpResult:
    agent_name = args.get("name", "").strip()
    set_agent_context(agent_name or "unknown")
    set_trigger("mcp_tool", detail="axi_kill_agent")
    _tracer.start_span("tool.axi_kill_agent", attributes={"agent.name": agent_name}).end()

    if not agent_name:
        return {
            "content": [{"type": "text", "text": "Error: 'name' is required and cannot be empty."}],
            "is_error": True,
        }
    if agent_name == config.MASTER_AGENT_NAME:
        return {
            "content": [{"type": "text", "text": f"Error: cannot kill reserved agent '{config.MASTER_AGENT_NAME}'."}],
            "is_error": True,
        }
    if agent_name not in agents.agents:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' not found."}], "is_error": True}

    session = agents.agents[agent_name]
    session_id = session.session_id

    # Remove from agents dict immediately so the name is freed for respawn
    agents.agents.pop(agent_name, None)

    agent_cwd = session.cwd

    async def _do_kill() -> None:
        try:
            agent_ch = await agents.get_agent_channel(agent_name)
            if agent_ch:
                if session_id:
                    await agents.send_system(
                        agent_ch,
                        f"Agent **{agent_name}** moved to Killed.\n"
                        f"Session ID: `{session_id}` — use this to resume later.",
                    )
                else:
                    await agents.send_system(agent_ch, f"Agent **{agent_name}** moved to Killed.")

            # 7.5d: stays a direct sleep_agent on the captured session object — the agent was
            # already popped from the registry above (to free the name for respawn), so the
            # name-based hub.sleep/hub.remove_agent would KeyError/no-op. The kill lifecycle
            # event is emitted separately via move_channel_to_killed (on_kill) below.
            await agents.sleep_agent(session, force=True)

            # Stop any test-instance systemd service for this agent BEFORE
            # cleaning up the worktree. Otherwise unlink-while-open semantics
            # leave a zombie service bound to the bot token slot.
            try:
                result = subprocess.run(
                    ["uv", "run", "python", "axi_test.py", "down", agent_name],
                    cwd=config.BOT_DIR,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    if "No reservation found" not in stderr:
                        log.warning(
                            "axi_test.py down '%s' failed (rc=%d): %s",
                            agent_name, result.returncode, stderr,
                        )
            except Exception:
                log.exception("Error running axi_test.py down for '%s'", agent_name)

            # Auto-merge if agent was in an auto-created worktree
            if worktrees.is_auto_worktree(agent_cwd):
                loop = asyncio.get_running_loop()
                status, detail = await loop.run_in_executor(
                    None, worktrees.try_merge_and_cleanup, agent_cwd
                )
                if agent_ch:
                    if status == "merged":
                        await agents.send_system(agent_ch, f"Auto-merged worktree as `{detail}`.")
                    elif status == "no_commits":
                        await agents.send_system(agent_ch, "Worktree had no commits — cleaned up.")
                    elif status in ("conflict", "needs_rebase"):
                        await agents.send_system(
                            agent_ch,
                            f"Could not auto-merge worktree: {detail}\n"
                            f"Worktree kept at `{agent_cwd}` for manual resolution.",
                        )
                    elif status == "error":
                        await agents.send_system(agent_ch, f"Auto-merge error: {detail}")
                log.info("Auto-merge for '%s': %s — %s", agent_name, status, detail)

            await agents.move_channel_to_killed(agent_name)
        except Exception:
            log.exception("Error in background kill of agent '%s'", agent_name)

    log.info("Killing agent '%s' via MCP tool (session=%s)", agent_name, session_id)
    agents.fire_and_forget(_do_kill())

    if session_id:
        return {"content": [{"type": "text", "text": f"Agent '{agent_name}' killed. Session ID: {session_id}"}]}
    return {"content": [{"type": "text", "text": f"Agent '{agent_name}' killed (no session ID available)."}]}


@tool(
    "axi_restart_agent",
    "Restart a single agent's CLI process with a fresh system prompt. "
    "Preserves session context (conversation history). The agent will pick up "
    "any changes to SYSTEM_PROMPT.md, extensions, or the core prompt on next wake.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the agent to restart"},
        },
        "required": ["name"],
    },
)
async def axi_restart_agent(args: McpArgs) -> McpResult:
    agent_name = args.get("name", "").strip()
    set_agent_context(agent_name or "unknown")
    set_trigger("mcp_tool", detail="axi_restart_agent")
    _tracer.start_span("tool.axi_restart_agent", attributes={"agent.name": agent_name}).end()

    if not agent_name:
        return {"content": [{"type": "text", "text": "Error: 'name' is required."}], "is_error": True}
    if agent_name == config.MASTER_AGENT_NAME:
        return {
            "content": [{"type": "text", "text": "Error: use axi_restart to restart the master agent."}],
            "is_error": True,
        }
    if agent_name not in agents.agents:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' not found."}], "is_error": True}

    session = await agents.restart_agent(agent_name)
    return {
        "content": [
            {
                "type": "text",
                "text": f"Agent '{agent_name}' restarted. System prompt refreshed, session '{session.session_id or 'none'}' preserved.",
            }
        ]
    }


@tool(
    "axi_restart",
    "Restart the Axi bot. Waits for busy agents to finish first (graceful). "
    "Only use when the user explicitly asks you to restart.",
    {"type": "object", "properties": {}, "required": []},
)
async def axi_restart(args: McpArgs) -> McpResult:
    set_agent_context(config.MASTER_AGENT_NAME)
    set_trigger("mcp_tool", detail="axi_restart")
    _tracer.start_span("tool.axi_restart").end()
    log.info("Restart requested via MCP tool")
    if agents.shutdown_coordinator is None:
        return {"content": [{"type": "text", "text": "Bot is not fully initialized yet."}]}
    agents.fire_and_forget(
        agents.shutdown_coordinator.graceful_shutdown("MCP tool", skip_agent=config.MASTER_AGENT_NAME)
    )
    return {"content": [{"type": "text", "text": "Graceful restart initiated. Waiting for busy agents to finish..."}]}


@tool(
    "axi_send_message",
    "Send a message to a spawned agent. The message appears in the agent's Discord channel "
    "(with your name as sender) and is processed like a user message. If the agent is busy, "
    "its current query is interrupted and your message is processed next. If sleeping, the "
    "agent wakes up. User-queued messages are preserved and process after yours.",
    {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the target agent to message",
            },
            "content": {
                "type": "string",
                "description": "The message content to send to the agent",
            },
            "sender": {
                "type": "string",
                "description": "Your agent name (defaults to master agent name)",
            },
        },
        "required": ["agent_name", "content"],
    },
)
async def axi_send_message(args: McpArgs) -> McpResult:
    target_name = args.get("agent_name", "").strip()
    set_agent_context(target_name or "unknown")
    set_trigger("mcp_tool", detail="axi_send_message")
    _tracer.start_span("tool.axi_send_message", attributes={"target.agent": target_name}).end()
    content = args.get("content", "").strip()

    if not target_name:
        return {
            "content": [{"type": "text", "text": "Error: agent_name is required."}],
            "is_error": True,
        }
    if not content:
        return {
            "content": [{"type": "text", "text": "Error: content is required."}],
            "is_error": True,
        }

    sender_name = args.get("sender", "").strip() or config.MASTER_AGENT_NAME
    if target_name == sender_name:
        return {
            "content": [{"type": "text", "text": "Error: cannot send messages to yourself."}],
            "is_error": True,
        }

    target_session = agents.agents.get(target_name)
    if target_session is None:
        return {
            "content": [{"type": "text", "text": f"Error: agent '{target_name}' not found."}],
            "is_error": True,
        }
    log.info("Inter-agent message: '%s' -> '%s': %s", sender_name, target_name, content[:200])

    result = await agents.deliver_inter_agent_message(sender_name, target_session, content)
    return {"content": [{"type": "text", "text": result}]}


# ---------------------------------------------------------------------------
# Utility tools (available to all agents)
# ---------------------------------------------------------------------------


@tool(
    "get_date_and_time",
    "Get the current date and time with logical day/week calculations. "
    "Accounts for the user's configured day boundary (the hour when a new 'day' starts). "
    "Always call this first to orient yourself before working with plans.",
    {"type": "object", "properties": {}, "required": []},
)
async def get_date_and_time(args: McpArgs) -> McpResult:
    tz = os.environ.get("SCHEDULE_TIMEZONE", "UTC")
    boundary = config.DAY_BOUNDARY_HOUR

    now = arrow.now(tz)

    # Logical date: if before boundary hour, it's still "yesterday"
    if now.hour < boundary:
        logical = now.shift(days=-1)
    else:
        logical = now

    # Logical week start (Sunday)
    # arrow weekday(): Monday=0 ... Sunday=6
    days_since_sunday = (logical.weekday() + 1) % 7
    week_start = logical.shift(days=-days_since_sunday).floor("day")
    week_end = week_start.shift(days=6)

    # Format day boundary display
    if boundary == 0:
        boundary_display = "12:00 AM (midnight)"
    elif boundary < 12:
        boundary_display = f"{boundary}:00 AM"
    elif boundary == 12:
        boundary_display = "12:00 PM (noon)"
    else:
        boundary_display = f"{boundary - 12}:00 PM"

    result = {
        "now": now.isoformat(),
        "now_display": now.format("dddd, MMM D, YYYY h:mm A"),
        "logical_date": logical.format("YYYY-MM-DD"),
        "logical_date_display": logical.format("dddd, MMM D, YYYY"),
        "logical_day_of_week": logical.format("dddd"),
        "logical_week_start": week_start.format("YYYY-MM-DD"),
        "logical_week_display": f"Week of {week_start.format('MMM D')} \u2013 {week_end.format('MMM D, YYYY')}",
        "timezone": tz,
        "day_boundary": boundary_display,
    }

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


@tool(
    "post_file",
    "Send a file as a message attachment to your own channel or another agent's channel. "
    "If no target is specified, the file is sent to your own agent channel.",
    {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file to upload"},
            "content": {"type": "string", "description": "Optional text message to include with the file"},
            "agent_name": {
                "type": "string",
                "description": "Target agent name. Omit to send to your own channel.",
            },
            "channel_id": {
                "type": "string",
                "description": "Target channel ID (backward compat). Prefer agent_name.",
            },
        },
        "required": ["file_path"],
    },
)
async def post_file(args: McpArgs) -> McpResult:
    from axi.egress_filter import is_path_blocked

    file_path = args["file_path"]
    _tracer.start_span("tool.post_file", attributes={"file.path": file_path}).end()

    if is_path_blocked(file_path):
        return {"content": [{"type": "text", "text": f"Access denied: uploading {file_path} is blocked (sensitive file)"}], "is_error": True}

    if not os.path.isfile(file_path):
        return {"content": [{"type": "text", "text": f"Error: file not found: {file_path}"}], "is_error": True}

    target = args.get("agent_name") or _get_caller()
    channel_ref = args.get("channel_id")
    if not target and not channel_ref:
        return {
            "content": [{"type": "text", "text": "Error: could not determine target. Provide agent_name or channel_id."}],
            "is_error": True,
        }

    description = args.get("content", "")
    filename = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
        router = _get_router()
        await router.post_file(target or "", filename, file_data, description, channel_ref=channel_ref)
        return {"content": [{"type": "text", "text": f"File '{filename}' sent."}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


# ---------------------------------------------------------------------------
# Discord REST tools (cross-channel messaging)
# ---------------------------------------------------------------------------


@tool(
    "discord_list_guilds",
    "List Discord guilds (servers) the bot is a member of. Returns guild id and name. "
    "Discord-only — not available with non-Discord frontends.",
    {"type": "object", "properties": {}, "required": []},
)
async def discord_list_guilds(args: McpArgs) -> McpResult:
    _tracer.start_span("tool.discord_list_guilds").end()
    try:
        guilds = await config.discord_client.list_guilds()
        result = [{"id": str(g["id"]), "name": g["name"]} for g in guilds]
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "discord_list_channels",
    "List text channels in a Discord guild/server. Returns channel id, name, and category. "
    "Discord-only — not available with non-Discord frontends.",
    {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "The Discord guild (server) ID. Defaults to the bot's home guild if omitted."},
        },
        "required": [],
    },
)
async def discord_list_channels(args: McpArgs) -> McpResult:
    guild_id = args.get("guild_id") or str(config.DISCORD_GUILD_ID)
    _tracer.start_span("tool.discord_list_channels", attributes={"discord.guild_id": guild_id}).end()
    try:
        text_channels = await config.discord_client.list_channels(guild_id)
        return {"content": [{"type": "text", "text": json.dumps(text_channels, indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


# ---------------------------------------------------------------------------
# Frontend-agnostic communication tools (routed through FrontendRouter)
# ---------------------------------------------------------------------------


@tool(
    "read_messages",
    "Read recent messages from a channel. Returns formatted message history.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "Channel ID or guild_id:channel_name (backward compat). Prefer agent_name."},
            "agent_name": {"type": "string", "description": "Read messages from this agent's channel. Omit to read your own."},
            "limit": {"type": "integer", "description": "Number of messages to fetch (default 20, max 200)"},
            "before": {
                "type": "string",
                "description": "Fetch messages before this point (message ID or ISO datetime)",
            },
            "after": {
                "type": "string",
                "description": "Fetch messages after this point (message ID or ISO datetime)",
            },
        },
        "required": [],
    },
)
async def read_messages(args: McpArgs) -> McpResult:
    limit = min(args.get("limit", 20), 200)
    before_raw = args.get("before")
    after_raw = args.get("after")
    target = args.get("agent_name") or _get_caller() or ""
    channel_ref = args.get("channel_id")
    _tracer.start_span("tool.read_messages", attributes={"agent": target, "limit": limit}).end()
    try:
        router = _get_router()
        messages = await router.read_messages(
            target, limit, before=before_raw, after=after_raw, channel_ref=channel_ref,
        )
        if not messages:
            return {"content": [{"type": "text", "text": "No messages found."}]}
        formatted = []
        for msg in messages:
            ts = msg.get("timestamp", "")
            author = msg.get("author", "unknown")
            content = msg.get("content", "")
            formatted.append(f"[{ts}] {author}: {content}")
        text = "\n".join(formatted)
        text = _truncate_mcp_text(text, len(messages))
        return {"content": [{"type": "text", "text": text}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "post_message",
    "Send a message to a channel OTHER than your own. Your text responses are automatically "
    "delivered to your own channel — do NOT use this tool for that. This tool is only for "
    "cross-channel messaging. To communicate with other agents, use axi_send_message instead "
    "— it goes through the agent's message handler and can wake sleeping agents.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "Channel ID or guild_id:channel_name (backward compat). Prefer agent_name."},
            "agent_name": {"type": "string", "description": "Target agent whose channel to post in."},
            "content": {"type": "string", "description": "The message content to send"},
        },
        "required": ["content"],
    },
)
async def post_message(args: McpArgs) -> McpResult:
    content = args["content"]
    target = args.get("agent_name") or ""
    channel_ref = args.get("channel_id")
    if not target and channel_ref:
        try:
            ch_id_int = int(channel_ref)
            resolved = agents.channel_to_agent.get(ch_id_int)
            if resolved:
                target = resolved
        except (ValueError, TypeError):
            pass
    if not target and not channel_ref:
        return {"content": [{"type": "text", "text": "Error: provide agent_name or channel_id."}], "is_error": True}
    _tracer.start_span("tool.post_message", attributes={"agent": target}).end()
    try:
        router = _get_router()
        await router.post_message(target, content, channel_ref=channel_ref)
        return {"content": [{"type": "text", "text": "Message sent."}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "search_messages",
    "Search messages across channels using full-text search. "
    "Searches the entire history, not just recent messages.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search text (full-text search)"},
            "agent_name": {"type": "string", "description": "Limit search to this agent's channel (optional)"},
            "channel_id": {"type": "string", "description": "Limit search to this channel ID (backward compat, optional)"},
            "limit": {"type": "integer", "description": "Max results to return (default 25, max 100)"},
        },
        "required": ["query"],
    },
)
async def search_messages(args: McpArgs) -> McpResult:
    query = args["query"]
    limit = min(args.get("limit", 25), 100)
    target = args.get("agent_name")
    channel_ref = args.get("channel_id")
    _tracer.start_span("tool.search_messages", attributes={"query": query}).end()
    try:
        router = _get_router()
        results = await router.search_messages(
            query, agent_name=target, limit=limit, channel_ref=channel_ref,
        )
        if not results:
            return {"content": [{"type": "text", "text": "No messages found."}]}
        formatted = []
        for msg in results:
            ts = msg.get("timestamp", "")
            ch_id = msg.get("channel_id", "")
            author = msg.get("author", "unknown")
            content = msg.get("content", "")
            formatted.append(f"[{ts}] #{ch_id} {author}: {content}")
        text = "\n".join(formatted)
        text = _truncate_mcp_text(text, len(results))
        return {"content": [{"type": "text", "text": text}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "wait_for_message",
    "Poll a channel and wait for a new message to appear. "
    "Returns when a non-system message arrives. Useful for test automation.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "Channel to watch (ID or guild_id:channel_name). Prefer agent_name."},
            "agent_name": {"type": "string", "description": "Agent whose channel to watch."},
            "after": {
                "type": "string",
                "description": "Wait for messages after this message ID. If omitted, uses the latest message as baseline.",
            },
            "timeout": {"type": "number", "description": "Max seconds to wait (default 120, max 300)"},
        },
        "required": [],
    },
)
async def wait_for_message(args: McpArgs) -> McpResult:
    timeout = min(args.get("timeout", 120), 300)
    after_id = args.get("after")
    target = args.get("agent_name") or _get_caller() or ""
    channel_ref = args.get("channel_id")
    if not target and not channel_ref:
        return {"content": [{"type": "text", "text": "Error: provide agent_name or channel_id."}], "is_error": True}
    _tracer.start_span("tool.wait_for_message", attributes={"agent": target}).end()
    try:
        router = _get_router()
        messages = await router.wait_for_message(
            target, timeout=timeout, after=after_id, channel_ref=channel_ref,
        )
        if not messages:
            return {"content": [{"type": "text", "text": f"Timed out after {timeout}s waiting for message."}]}
        formatted = []
        for msg in messages:
            ts = msg.get("timestamp", "")
            author = msg.get("author", "unknown")
            formatted.append(f"[{ts}] {author}: {msg.get('content', '')}")
        cursor = messages[-1].get("cursor") or messages[-1].get("id", "")
        if cursor:
            formatted.append(f"cursor: {cursor}")
        return {"content": [{"type": "text", "text": "\n".join(formatted)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


# ---------------------------------------------------------------------------
# Channel status tools (available to all agents)
# ---------------------------------------------------------------------------


@tool(
    "set_status",
    "Set the user's to-do type emoji on your agent channel. "
    "The emoji represents what type of action the user needs to take next. "
    "Pass an empty string to clear.",
    {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": (
                    "A single emoji representing the user's to-do type. "
                    "Examples: \u2753 (awaiting direction), "
                    "\U0001f4ac (user should respond), \U0001f4da (user should read), "
                    "\U0001f52c (user should test). Never use \u2705 (checkmark). "
                    "Pass empty string to clear."
                ),
            },
        },
        "required": ["emoji"],
    },
)
async def set_status(args: McpArgs) -> McpResult:
    emoji = args.get("emoji", "").strip()
    _tracer.start_span("tool.set_status", attributes={"emoji": emoji}).end()
    if not config.CHANNEL_STATUS_ENABLED:
        return {"content": [{"type": "text", "text": "Channel status prefixes are not enabled."}]}

    caller = get_streaming_agent()
    if not caller:
        return {"content": [{"type": "text", "text": "Error: could not determine calling agent."}], "is_error": True}

    from axi.hub_wiring import router

    if emoji:
        await router.set_status(caller, "", emoji)
        return {"content": [{"type": "text", "text": f"Status set to '{emoji}'. Channel will update shortly."}]}
    else:
        await router.set_status(caller, "", None)
        return {"content": [{"type": "text", "text": "Custom status cleared. Channel will revert to auto-detected status."}]}


@tool(
    "clear_status",
    "Clear your custom status and revert to auto-detected status.",
    {"type": "object", "properties": {}, "required": []},
)
async def clear_status(args: McpArgs) -> McpResult:
    _tracer.start_span("tool.clear_status").end()
    if not config.CHANNEL_STATUS_ENABLED:
        return {"content": [{"type": "text", "text": "Channel status prefixes are not enabled."}]}

    caller = get_streaming_agent()
    if not caller:
        return {"content": [{"type": "text", "text": "Error: could not determine calling agent."}], "is_error": True}

    from axi.hub_wiring import router

    await router.set_status(caller, "", None)
    return {"content": [{"type": "text", "text": "Custom status cleared. Channel will revert to auto-detected status."}]}


# toggle_plan_mode is intentionally NOT routed through the Frontend protocol.
# It controls the Claude SDK client's permission mode (session.client.set_permission_mode),
# which is a runtime concern — no frontend interaction involved.
@tool(
    "toggle_plan_mode",
    "Toggle plan mode on an agent. When plan mode is ON, the agent plans "
    "before implementing. When OFF, the agent executes normally. "
    "Returns the new plan mode state.",
    {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the agent to toggle plan mode on",
            },
        },
        "required": ["agent_name"],
    },
)
async def toggle_plan_mode(args: McpArgs) -> McpResult:
    agent_name = args.get("agent_name", "").strip()
    _tracer.start_span("tool.toggle_plan_mode", attributes={"agent.name": agent_name}).end()

    if not agent_name:
        return {"content": [{"type": "text", "text": "Error: agent_name is required."}], "is_error": True}

    session = agents.agents.get(agent_name)
    if session is None:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' not found."}], "is_error": True}

    new_mode = not session.plan_mode
    session.plan_mode = new_mode

    if session.client is not None:
        try:
            mode_str = "plan" if new_mode else "default"
            await session.client.set_permission_mode(mode_str)
            log.info("Agent '%s' permission mode set to '%s'", agent_name, mode_str)
        except Exception as e:
            log.exception("Failed to set permission mode for '%s'", agent_name)
            session.plan_mode = not new_mode
            return {
                "content": [{"type": "text", "text": f"Error: failed to set plan mode for '{agent_name}': {e}"}],
                "is_error": True,
            }

    state = "ON" if new_mode else "OFF"
    return {"content": [{"type": "text", "text": f"Plan mode {state} for '{agent_name}'."}]}


# ---------------------------------------------------------------------------
# MCP server assembly
# ---------------------------------------------------------------------------

# Utility tools (shared by all agents) — includes frontend-agnostic communication tools
utils_mcp_server = create_sdk_mcp_server(
    name="utils",
    version="1.0.0",
    tools=[
        get_date_and_time,
        post_file,
        set_status,
        clear_status,
        toggle_plan_mode,
        read_messages,
        post_message,
        search_messages,
        wait_for_message,
    ],
)

# Spawned agents get spawn+kill+restart-agent (no bot restart — they tell the parent)
axi_mcp_server = create_sdk_mcp_server(
    name="axi",
    version="1.0.0",
    tools=[axi_spawn_agent, axi_kill_agent, axi_send_message],
)

# Master agent gets the full set including bot restart + send_message
axi_master_mcp_server = create_sdk_mcp_server(
    name="axi",
    version="1.0.0",
    tools=[axi_spawn_agent, axi_kill_agent, axi_send_message],
)

# Discord-only tools (guild/channel listing — only available when Discord frontend is active)
discord_mcp_server = create_sdk_mcp_server(
    name="discord",
    version="1.0.0",
    tools=[
        discord_list_guilds,
        discord_list_channels,
    ],
)
