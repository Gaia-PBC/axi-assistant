"""Frontend-agnostic slash-command logic (Phase 8).

Each function implements one command's behavior with plain arguments + explicit targets and
returns a CommandResult — NO Discord objects, no `interaction`. The Discord slash handlers
(axi/main.py) and the HTTP API (axi/http_api.py) are thin wrappers over these functions, so
every command works identically over Discord and REST.

Batch 8a: read/info commands (ping, list-agents, status, claude-usage, flowchart-list).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from axi import agents, config
from axi.axi_types import tool_display

if TYPE_CHECKING:
    from axi.axi_types import AgentSession


@dataclass(slots=True)
class CommandResult:
    """The result of a command, consumed by any frontend.

    message: human-readable text (Discord posts it; HTTP returns it as `message`).
    data:    structured payload (HTTP returns it as JSON; Discord may format from it).
    ok:      False for user/validation errors.
    ephemeral: Discord hint — post as an ephemeral/system reply.
    """

    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    ephemeral: bool = False


# ---------------------------------------------------------------------------
# Shared frontend-agnostic formatting helpers (relocated from main.py)
# ---------------------------------------------------------------------------


def fmt_uptime(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def agent_state_summary(session: AgentSession) -> str:
    """Short state string for an agent (e.g. 'sleeping (5m)', 'thinking...')."""
    now = datetime.now(UTC)
    if session.client is None:
        idle = int((now - session.last_activity).total_seconds())
        return f"sleeping ({agents.format_time_remaining(idle)})"
    if session.bridge_busy:
        return "busy (running in bridge)"
    if not session.query_lock.locked():
        idle = int((now - session.last_activity).total_seconds())
        return f"idle ({agents.format_time_remaining(idle)})"

    activity = session.activity
    if activity.phase == "thinking":
        status = "thinking..."
    elif activity.phase == "writing":
        status = "writing response..."
    elif activity.phase == "tool_use" and activity.tool_name:
        status = tool_display(activity.tool_name)
    elif activity.phase == "waiting":
        status = "processing tool results..."
    elif activity.phase == "starting":
        status = "starting query..."
    else:
        status = f"busy ({activity.phase})"
    return status


def format_agent_status(name: str, session: AgentSession) -> str:
    """Detailed multi-line status for a single agent."""
    now = datetime.now(UTC)
    lines = [f"**{name}**"]

    if session.agent_type == "flowcoder":
        lines.append("Type: flowcoder")
        lines.append(f"cwd: `{session.cwd}`")
        return "\n".join(lines)

    if session.client is None:
        lines.append("State: sleeping")
        idle = int((now - session.last_activity).total_seconds())
        lines.append(f"Last active: {agents.format_time_remaining(idle)} ago")
    elif session.bridge_busy:
        lines.append("State: **busy** (running in bridge)")
    elif not session.query_lock.locked():
        lines.append("State: awake, idle")
        idle = int((now - session.last_activity).total_seconds())
        lines.append(f"Idle for: {agents.format_time_remaining(idle)}")
    else:
        activity = session.activity
        if activity.phase == "thinking":
            lines.append("State: **thinking** (extended thinking)")
        elif activity.phase == "writing":
            lines.append(f"State: **writing response** ({activity.text_chars} chars so far)")
        elif activity.phase == "tool_use" and activity.tool_name:
            display = tool_display(activity.tool_name)
            lines.append(f"State: **{display}**")
            if activity.tool_name == "Bash" and activity.tool_input_preview:
                preview = agents.extract_tool_preview(activity.tool_name, activity.tool_input_preview)
                if preview:
                    lines.append(f"```\n{preview}\n```")
            elif activity.tool_name in ("Read", "Write", "Edit", "Grep", "Glob") and activity.tool_input_preview:
                preview = agents.extract_tool_preview(activity.tool_name, activity.tool_input_preview)
                if preview:
                    lines.append(f"`{preview}`")
        elif activity.phase == "waiting":
            lines.append("State: **processing tool results...**")
        elif activity.phase == "starting":
            lines.append("State: **starting query...**")
        else:
            lines.append(f"State: **busy** ({activity.phase})")

        if activity.query_started:
            elapsed = int((now - activity.query_started).total_seconds())
            lines.append(f"Query running for: {agents.format_time_remaining(elapsed)}")
        if activity.turn_count > 0:
            lines.append(f"API turns: {activity.turn_count}")
        if activity.last_event:
            since_last = int((now - activity.last_event).total_seconds())
            if since_last > 30:
                lines.append(f"No stream events for {agents.format_time_remaining(since_last)} (may be running a long tool)")

    queue_size = len(session.state.queued_turns)
    if queue_size > 0:
        lines.append(f"Queued messages: {queue_size}")
    if agents.is_rate_limited():
        remaining = agents.format_time_remaining(agents.rate_limit_remaining_seconds())
        lines.append(f"Rate limited: ~{remaining} remaining")
    if session.plan_mode:
        lines.append("📋 **Plan mode active**")
    if session.context_tokens > 0 and session.context_window > 0:
        pct = session.context_tokens / session.context_window
        lines.append(f"Context: {session.context_tokens:,}/{session.context_window:,} tokens ({pct:.0%})")
    if session.session_id:
        lines.append(f"Session: `{session.session_id[:8]}...`")
    lines.append(f"cwd: `{session.cwd}`")
    return "\n".join(lines)


def list_flowchart_commands() -> list[dict[str, Any]]:
    """Available flowchart commands as [{name, description}, ...]."""
    from axi.flowcoder import get_search_paths

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for commands_dir in get_search_paths():
        if not os.path.isdir(commands_dir):
            continue
        for fname in sorted(os.listdir(commands_dir)):
            if not fname.endswith(".json"):
                continue
            name = fname.removesuffix(".json")
            if name in seen:
                continue
            seen.add(name)
            try:
                with open(os.path.join(commands_dir, fname)) as f:
                    data = json.load(f)
                results.append({"name": data.get("name", name), "description": data.get("description", "")})
            except (OSError, json.JSONDecodeError):
                continue
    return results


def _agent_state_kind(session: AgentSession) -> str:
    if session.query_lock.locked():
        return "busy"
    if session.client is not None:
        return "awake"
    return "sleeping"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def ping(*, latency_ms: int | None = None, bot_start_time: datetime | None = None) -> CommandResult:
    """Bot/bridge health + uptime. latency_ms is a Discord gateway metric (None over HTTP)."""
    if bot_start_time is not None:
        bot_uptime_s = int((datetime.now(UTC) - bot_start_time).total_seconds())
        bot_str = fmt_uptime(bot_uptime_s)
    else:
        bot_uptime_s = None
        bot_str = "initializing"

    bridge_uptime_s: int | None = None
    bridge_conn = agents.procmux_conn
    bridge_alive = bool(bridge_conn is not None and bridge_conn.is_alive)
    if bridge_alive:
        try:
            result = await bridge_conn.send_command("status")
            if result.ok and result.uptime_seconds is not None:
                bridge_uptime_s = result.uptime_seconds
        except Exception:
            bridge_uptime_s = -1  # error sentinel

    parts = []
    if latency_ms is not None:
        parts.append(f"Pong! Latency: {latency_ms}ms")
    parts.append(f"Bot uptime: {bot_str}")
    if bridge_uptime_s is not None and bridge_uptime_s >= 0:
        parts.append(f"Bridge uptime: {fmt_uptime(bridge_uptime_s)}")
    elif bridge_uptime_s == -1:
        parts.append("Bridge uptime: error")
    elif not bridge_alive:
        parts.append("Bridge: not connected")
    return CommandResult(
        message=" | ".join(parts),
        data={
            "latency_ms": latency_ms,
            "bot_uptime_seconds": bot_uptime_s,
            "bridge_connected": bridge_alive,
            "bridge_uptime_seconds": bridge_uptime_s if (bridge_uptime_s or 0) >= 0 else None,
        },
    )


def list_agents() -> CommandResult:
    """All agent sessions with state. (Discord adds channel mentions / killed tags; those
    are Discord-category concepts kept in the Discord wrapper.)"""
    if not agents.agents:
        return CommandResult(message="No active agents.", data={"agents": [], "awake": 0}, ephemeral=True)

    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for name, session in agents.agents.items():
        idle_minutes = int((now - session.last_activity).total_seconds() / 60)
        kind = _agent_state_kind(session)
        is_master = name == config.MASTER_AGENT_NAME
        channel_id = None
        from axi.axi_types import discord_state

        channel_id = discord_state(session).channel_id
        rows.append(
            {
                "name": name,
                "state": kind,
                "idle_minutes": idle_minutes,
                "cwd": session.cwd,
                "session_id": session.session_id,
                "channel_id": channel_id,
                "is_master": is_master,
            }
        )
        protected = " [protected]" if is_master else ""
        sid = f" | sid: `{session.session_id[:8]}…`" if session.session_id else ""
        lines.append(f"- **{name}** [{kind}]{protected} | cwd: `{session.cwd}` | idle: {idle_minutes}m{sid}")

    awake = agents.count_awake_agents()
    header = f"*System:* **Agent Sessions** ({awake}/{config.MAX_AWAKE_AGENTS} awake):\n"
    return CommandResult(
        message=header + "\n".join(lines),
        data={"agents": rows, "awake": awake, "max_awake": config.MAX_AWAKE_AGENTS},
    )


def agent_status(name: str | None) -> CommandResult:
    """Detailed status for one agent, or a summary of all when name is None."""
    if name is not None:
        session = agents.agents.get(name)
        if session is None:
            return CommandResult(message=f"Agent **{name}** not found.", ok=False, ephemeral=True)
        return CommandResult(
            message=format_agent_status(name, session),
            data={"name": name, "state": _agent_state_kind(session), "session_id": session.session_id},
            ephemeral=True,
        )

    # All-agents summary
    if not agents.agents:
        return CommandResult(message="No active agents.", data={"agents": []}, ephemeral=True)
    lines: list[str] = []
    rows: list[dict[str, Any]] = []
    for aname, session in agents.agents.items():
        summary = agent_state_summary(session)
        queue = len(session.state.queued_turns)
        queue_str = f" | {queue} queued" if queue > 0 else ""
        lines.append(f"- **{aname}**: {summary}{queue_str}")
        rows.append({"name": aname, "summary": summary, "queued": queue})
    awake = agents.count_awake_agents()
    header = f"**Agent Status** ({awake}/{config.MAX_AWAKE_AGENTS} awake)"
    if agents.is_rate_limited():
        remaining = agents.format_time_remaining(agents.rate_limit_remaining_seconds())
        header += f" | rate limited (~{remaining})"
    return CommandResult(
        message=f"*System:* {header}\n" + "\n".join(lines),
        data={"agents": rows, "awake": awake},
        ephemeral=True,
    )


async def claude_usage(history: int | None = None) -> CommandResult:
    """Claude API usage for current sessions + rate-limit status (or recent history)."""
    if history is not None:
        count = max(1, min(history, 50))
        lines = [f"**Rate Limit History** (last {count} events)", ""]
        events: list[dict[str, Any]] = []
        try:
            with open(config.RATE_LIMIT_HISTORY_PATH) as f:
                all_lines = f.readlines()
            recent = all_lines[-count:]
            if not recent:
                lines.append("No history recorded yet.")
            else:
                for raw_line in recent:
                    try:
                        r = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    events.append(r)
                    ts = datetime.fromisoformat(r["ts"]).astimezone(config.SCHEDULE_TIMEZONE)
                    ts_str = ts.strftime("%-m/%-d %-I:%M %p")
                    rl_type = r.get("type", "?").replace("_", " ")
                    status = r.get("status", "?")
                    util = r.get("utilization")
                    if status == "rejected":
                        icon = "\U0001f6ab"
                    elif status == "allowed_warning":
                        icon = "⚠️"
                    else:
                        icon = "✅"
                    util_str = f" ({int(util * 100)}%)" if util is not None else ""
                    lines.append(f"`{ts_str}` {icon} {rl_type}: {status}{util_str}")
        except FileNotFoundError:
            lines.append("No history file yet — events are recorded on API calls.")
        return CommandResult(message="\n".join(lines), data={"history": events})

    lines = ["**Claude Usage — Current Sessions**", ""]
    total_cost = 0.0
    total_queries = 0
    sessions_data: list[dict[str, Any]] = []
    if agents.session_usage:
        for sid, usage in sorted(
            agents.session_usage.items(),
            key=lambda x: x[1].last_query or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        ):
            total_cost += usage.total_cost_usd
            total_queries += usage.queries
            duration_s = usage.total_duration_ms // 1000
            duration_str = agents.format_time_remaining(duration_s) if duration_s > 0 else "0s"
            active_str = ""
            if usage.first_query:
                age_s = int((datetime.now(UTC) - usage.first_query).total_seconds())
                active_str = f" | Active since {agents.format_time_remaining(age_s)} ago"
            token_str = ""
            if usage.total_input_tokens or usage.total_output_tokens:
                token_str = f" | Tokens: {usage.total_input_tokens:,}in / {usage.total_output_tokens:,}out"
            lines.append(f"**{usage.agent_name}** (`{sid[:8]}`)")
            lines.append(
                f"  Cost: **${usage.total_cost_usd:.2f}** | Queries: {usage.queries} | Turns: {usage.total_turns}{token_str}"
            )
            lines.append(f"  API time: {duration_str}{active_str}")
            lines.append("")
            sessions_data.append(
                {
                    "session_id": sid,
                    "agent_name": usage.agent_name,
                    "cost_usd": usage.total_cost_usd,
                    "queries": usage.queries,
                    "turns": usage.total_turns,
                    "input_tokens": usage.total_input_tokens,
                    "output_tokens": usage.total_output_tokens,
                }
            )
        lines.append(f"**Total: ${total_cost:.2f}** across {total_queries} queries")
    else:
        lines.append("No usage recorded yet.")
    lines.append("")

    quotas_data: list[dict[str, Any]] = []
    if agents.rate_limit_quotas:
        now = datetime.now(UTC)
        lines.append("**Rate Limits**")
        display_order = ["five_hour", "seven_day"]
        sorted_keys = [k for k in display_order if k in agents.rate_limit_quotas]
        sorted_keys += [k for k in agents.rate_limit_quotas if k not in display_order]
        for key in sorted_keys:
            q = agents.rate_limit_quotas[key]
            resets_str = agents.format_time_remaining(int((q.resets_at - now).total_seconds())) if q.resets_at else "?"
            local_reset = q.resets_at.astimezone(config.SCHEDULE_TIMEZONE) if q.resets_at else None
            reset_time_str = "?"
            if local_reset is not None:
                reset_time_str = local_reset.strftime("%-I:%M %p")
                local_now = now.astimezone(config.SCHEDULE_TIMEZONE)
                if local_reset.date() != local_now.date():
                    reset_time_str = local_reset.strftime("%-I:%M %p %a")
            if q.status == "rejected":
                status_str = (
                    f"\U0001f6ab Rate limited ({int(q.utilization * 100)}% used)"
                    if q.utilization is not None
                    else "\U0001f6ab Rate limited"
                )
            elif q.status == "allowed_warning" and q.utilization is not None:
                status_str = f"⚠️ {int(q.utilization * 100)}% used"
            else:
                status_str = "✅ OK (< 80%)"
            label = q.rate_limit_type.replace("_", " ")
            lines.append(f"  {label}: {status_str} — resets at {reset_time_str} (in {resets_str})")
            quotas_data.append(
                {"type": q.rate_limit_type, "status": q.status, "utilization": q.utilization}
            )
        latest_update = max(q.updated_at for q in agents.rate_limit_quotas.values())
        age_s = int((now - latest_update).total_seconds())
        age_str = agents.format_time_remaining(age_s) if age_s > 0 else "just now"
        lines.append(f"  Last checked: {age_str} ago")
    elif agents.rate_limited_until:
        remaining = agents.format_time_remaining(agents.rate_limit_remaining_seconds())
        lines.append(f"**Rate Limit**: \U0001f6ab Rate limited (~{remaining} remaining)")
    else:
        lines.append("**Rate Limit**: No data yet (updates on next API call)")

    return CommandResult(
        message="\n".join(lines),
        data={
            "sessions": sessions_data,
            "total_cost_usd": total_cost,
            "total_queries": total_queries,
            "rate_limits": quotas_data,
        },
    )


def flowchart_list() -> CommandResult:
    """Available flowchart commands."""
    commands = list_flowchart_commands()
    if not commands:
        return CommandResult(message="No flowchart commands found.", data={"commands": []}, ephemeral=True)
    fc_lines = [f"• `{c['name']}`" + (f" — {c['description']}" if c["description"] else "") for c in commands]
    return CommandResult(
        message=f"*System:* **Available flowcharts** ({len(commands)}):\n" + "\n".join(fc_lines),
        data={"commands": commands},
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Turn control + lifecycle (Phase 8b)
# ---------------------------------------------------------------------------


def _require_busy(name: str) -> tuple[Any, CommandResult | None]:
    session = agents.agents.get(name)
    if session is None:
        return None, CommandResult(message=f"Agent **{name}** not found.", ok=False, ephemeral=True)
    if session.client is None or not session.query_lock.locked():
        return session, CommandResult(message=f"Agent **{name}** is not busy.", ok=False, ephemeral=True)
    return session, None


async def stop(name: str) -> CommandResult:
    """Interrupt an agent's current query and clear its queued turns (the frontend-agnostic
    core of /stop; Discord additionally cancels plan/question prompts + reactions)."""
    session, err = _require_busy(name)
    if err is not None:
        return err
    cleared = 0
    if agents.hub is not None:
        result = await agents.hub.request_stop(name, clear_queue=True)
        cleared = result.cleared
    else:
        cleared = len(session.state.queued_turns)
        session.state.stop_requested = True
        session.state.queued_turns.clear()
    parts = [f"*System:* Interrupt signal sent to **{name}**."]
    if cleared:
        parts.append(f"Cleared {cleared} queued message{'s' if cleared != 1 else ''}.")
    return CommandResult(message=" ".join(parts), data={"status": "stopping", "cleared": cleared})


async def skip(name: str) -> CommandResult:
    """Interrupt the current query but keep processing queued turns."""
    session, err = _require_busy(name)
    if err is not None:
        return err
    queued = len(session.state.queued_turns)
    activity = session.activity
    tool_suffix = ""
    if activity.phase == "waiting" and activity.tool_name:
        tool_suffix = f" (was {tool_display(activity.tool_name)})"
    if agents.hub is not None:
        await agents.hub.request_skip(name)
    if queued:
        noun = "message" if queued == 1 else "messages"
        msg = f"*System:* Skipped current query for **{name}**{tool_suffix}. Latest {noun} will continue processing."
    else:
        msg = f"*System:* Skipped current query for **{name}**{tool_suffix}. No queued messages."
    return CommandResult(message=msg, data={"status": "skipping", "queued": queued})


def validate_spawn(name: str, cwd: str | None, model: str | None, provider: str | None = None) -> CommandResult:
    """Synchronous validation for spawn (name/master/exists/cwd/model/provider). On ok, data carries
    the resolved {cwd, model, provider}."""
    agent_name = (name or "").strip()
    if not agent_name:
        return CommandResult(message="Agent name cannot be empty.", ok=False, ephemeral=True)
    if agent_name == config.MASTER_AGENT_NAME:
        return CommandResult(
            message=f"Cannot spawn agent with reserved name '{config.MASTER_AGENT_NAME}'.", ok=False, ephemeral=True
        )
    default_cwd = os.path.join(config.AXI_USER_DATA, "agents", agent_name)
    agent_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else default_cwd
    agent_model = config.normalize_model(model) if model else None
    if agent_model:
        error = config.validate_model(agent_model)
        if error:
            return CommandResult(message=f"*System:* {error}", ok=False, ephemeral=True)
    if not any(agent_cwd == d or agent_cwd.startswith(d + os.sep) for d in config.ALLOWED_CWDS):
        return CommandResult(message="Error: cwd is not in allowed directories.", ok=False, ephemeral=True)
    if provider is not None and config.get_provider(provider) is None:
        return CommandResult(message=f"*System:* Unknown provider '{provider}'.", ok=False, ephemeral=True)
    return CommandResult(message="", data={"cwd": agent_cwd, "model": agent_model, "provider": provider})


async def spawn(
    name: str, prompt: str, *, cwd: str | None = None, resume: str | None = None,
    model: str | None = None, provider: str | None = None,
) -> CommandResult:
    """Spawn (or resume-replace) an agent. Validates, reclaims the name on resume, then
    agents.spawn_agent (which creates the channel via the frontend on_spawn)."""
    agent_name = (name or "").strip()
    if agent_name in agents.agents and not resume:
        return CommandResult(
            message=f"Agent **{agent_name}** already exists. Kill it first or use `resume` to replace it.",
            ok=False,
            ephemeral=True,
        )
    valid = validate_spawn(agent_name, cwd, model, provider=provider)
    if not valid.ok:
        return valid
    agent_cwd = valid.data["cwd"]
    agent_model = valid.data["model"]
    agent_provider = valid.data["provider"]
    if agent_name in agents.agents and resume:
        await agents.reclaim_agent_name(agent_name)
    await agents.spawn_agent(agent_name, agent_cwd, prompt, resume=resume, model=agent_model, provider=agent_provider)
    model_suffix = f" using **{agent_model}**" if agent_model else ""
    if agent_provider:
        model_suffix += f" on **{agent_provider}**"
    return CommandResult(
        message=f"*System:* Spawned agent **{agent_name}** in `{agent_cwd}`{model_suffix}.",
        data={"name": agent_name, "cwd": agent_cwd, "model": agent_model, "provider": agent_provider},
    )


async def kill_agent(name: str) -> CommandResult:
    """Terminate an agent (sleep + on_kill move-to-Killed + registry pop via hub.remove_agent)."""
    session = agents.agents.get(name)
    if session is None:
        return CommandResult(message=f"Agent **{name}** not found.", ok=False, ephemeral=True)
    if name == config.MASTER_AGENT_NAME:
        return CommandResult(message="Cannot kill the axi-master session.", ok=False, ephemeral=True)
    session_id = session.session_id
    await agents.hub.remove_agent(name)
    if session_id:
        msg = f"*System:* Agent **{name}** moved to Killed.\nSession ID: `{session_id}` — use this to resume later."
    else:
        msg = f"*System:* Agent **{name}** moved to Killed."
    return CommandResult(message=msg, data={"name": name, "session_id": session_id})


async def restart_agent(name: str) -> CommandResult:
    """Restart an agent's CLI with a fresh system prompt, preserving session context."""
    if name not in agents.agents:
        return CommandResult(message=f"Agent **{name}** not found.", ok=False, ephemeral=True)
    if name == config.MASTER_AGENT_NAME:
        return CommandResult(
            message="Cannot restart axi-master this way. Use `/restart` instead.", ok=False, ephemeral=True
        )
    session = await agents.restart_agent(name)
    return CommandResult(
        message=(
            f"*System:* Agent **{name}** restarted. "
            f"System prompt refreshed, session `{session.session_id or 'none'}` preserved."
        ),
        data={"name": name, "session_id": session.session_id},
    )


# ---------------------------------------------------------------------------
# Context + scope commands (Phase 8c)
# ---------------------------------------------------------------------------


def _parse_toggle(mode: str | None, current: bool, usage: str) -> tuple[bool | None, str | None]:
    """Resolve an on/off/toggle mode. Returns (new_state, error). error is set for bad input."""
    if mode is None:
        return (not current), None
    m = mode.strip().lower()
    if m == "on":
        return True, None
    if m == "off":
        return False, None
    return None, usage


async def reset_context(agent: str, cwd: str | None = None) -> CommandResult:
    """Reset an agent's context (fresh session), optionally moving its cwd."""
    if agent not in agents.agents:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    session = await agents.reset_session(agent, cwd=cwd)
    return CommandResult(
        message=f"*System:* Context reset for **{agent}**. Working directory: `{session.cwd}`",
        data={"agent": agent, "cwd": session.cwd},
    )


async def set_model(agent: str | None, model: str | None, provider: str | None = None) -> CommandResult:
    """Get or set the model. agent=None targets the global default; a name targets that agent
    (which is saved + restarted with a fresh session). ``provider:model`` is parsed internally;
    an explicit ``provider`` argument overrides the parsed provider when set."""
    if model is None:  # view
        if agent and agent in agents.agents:
            session = agents.agents[agent]
            current = session.model or config.get_model()
            if session.provider:
                current = f"{session.provider}:{current}"
            return CommandResult(message=f"Current model for **{agent}**: **{current}**", data={"agent": agent, "model": current, "provider": session.provider})
        current = config.get_model()
        return CommandResult(message=f"Current default model: **{current}**", data={"agent": None, "model": current})

    parsed_provider, bare_model = config.parse_provider_model(model)
    if provider is None:
        provider = parsed_provider
    error = config.validate_model(bare_model)
    if error:
        return CommandResult(message=f"*System:* {error}", ok=False, ephemeral=True)
    normalized = config.normalize_model(bare_model)
    if agent and agent in agents.agents:
        session = agents.agents[agent]
        session.model = normalized
        session.provider = provider
        agent_cfg = agents._load_agent_config(agent)
        agents._save_agent_config(agent, session.mcp_server_names, extensions=agent_cfg.get("extensions"), model=normalized, provider=provider)
        await agents.reset_session(agent)
        display = f"{provider}:{normalized}" if provider else normalized
        return CommandResult(
            message=f"*System:* Agent **{agent}** switched to **{display}** and restarted with a fresh session.",
            data={"agent": agent, "model": normalized, "provider": provider},
        )
    error = config.set_model(normalized)
    if error:
        return CommandResult(message=f"*System:* {error}", ok=False, ephemeral=True)
    display = f"{provider}:{normalized}" if provider else normalized
    return CommandResult(
        message=f"*System:* Default model set to **{display}**.", data={"agent": None, "model": config.get_model()}
    )


def set_verbose(agent: str, mode: str | None = None) -> CommandResult:
    """Toggle/set the Discord verbose-rendering flag for an agent."""
    session = agents.agents.get(agent)
    if session is None:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    from axi.axi_types import discord_state

    ds = discord_state(session)
    new, err = _parse_toggle(mode, ds.verbose, "Usage: `/verbose` (toggle), `/verbose on`, `/verbose off`")
    if err:
        return CommandResult(message=err, ok=False, ephemeral=True)
    ds.verbose = new
    state = "on" if new else "off"
    return CommandResult(message=f"*System:* Verbose output **{state}** for **{agent}**.", data={"agent": agent, "verbose": new})


def set_debug(agent: str, mode: str | None = None) -> CommandResult:
    """Toggle/set the Discord debug (stderr) rendering flag for an agent."""
    session = agents.agents.get(agent)
    if session is None:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    from axi.axi_types import discord_state

    ds = discord_state(session)
    new, err = _parse_toggle(mode, ds.debug, "Usage: `/debug` (toggle), `/debug on`, `/debug off`")
    if err:
        return CommandResult(message=err, ok=False, ephemeral=True)
    ds.debug = new
    state = "on" if new else "off"
    return CommandResult(message=f"*System:* Debug output **{state}** for **{agent}**.", data={"agent": agent, "debug": new})


def set_debug_all(mode: str | None = None) -> CommandResult:
    """Toggle/set the debug flag for ALL agents (the global variant of /debug)."""
    from axi.axi_types import discord_state

    if mode is not None:
        m = mode.strip().lower()
        if m == "on":
            new = True
        elif m == "off":
            new = False
        else:
            return CommandResult(
                message="Usage: `/debug-all` (toggle), `/debug-all on`, `/debug-all off`", ok=False, ephemeral=True
            )
    else:
        on_count = sum(1 for s in agents.agents.values() if discord_state(s).debug)
        new = on_count <= len(agents.agents) // 2
    for session in agents.agents.values():
        discord_state(session).debug = new
    state = "on" if new else "off"
    return CommandResult(
        message=f"*System:* Debug output **{state}** for all **{len(agents.agents)}** agents.",
        data={"debug": new, "count": len(agents.agents)},
    )


async def set_plan(agent: str) -> CommandResult:
    """Toggle plan mode for an agent (and its CLI permission mode if awake)."""
    session = agents.agents.get(agent)
    if session is None:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    new_mode = not session.plan_mode
    session.plan_mode = new_mode
    if session.client is not None:
        try:
            await session.client.set_permission_mode("plan" if new_mode else "default")
        except Exception as e:
            session.plan_mode = not new_mode
            return CommandResult(message=f"Failed to set plan mode for **{agent}**: {e}", ok=False, ephemeral=True)
    if new_mode:
        msg = f"📋 **Plan mode ON** for **{agent}** — next query will plan before implementing."
    else:
        msg = f"🔧 **Plan mode OFF** for **{agent}** — back to normal execution."
    return CommandResult(message=msg, data={"agent": agent, "plan_mode": new_mode})


async def _run_raw_cli_command(agent: str, command: str, verb: str) -> CommandResult:
    """Run a raw CLI slash command (e.g. /compact, /clear) on an agent via a hub 'raw' turn.

    The raw turn skips the flowchart wrap (so the CLI gets the literal command) but still uses
    the hub's turn accounting + streams the reply to all frontends (Discord renders it; an HTTP
    caller triggers it fire-and-forget). Rejects if the agent is busy, matching the slash UX.
    """
    session = agents.agents.get(agent)
    if session is None:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    if session.query_lock.locked():
        return CommandResult(message=f"Agent **{agent}** is busy.", ok=False, ephemeral=True)
    if agents.hub is None:
        return CommandResult(message="Hub unavailable.", ok=False, ephemeral=True)
    await agents.hub.submit_user_message(agent, command, metadata={"raw": True})
    return CommandResult(message=f"*System:* {verb} context for **{agent}**...", data={"agent": agent, "command": command})


async def compact(agent: str) -> CommandResult:
    """Compact an agent's conversation context (raw /compact with its compact_instructions)."""
    session = agents.agents.get(agent)
    command = "/compact"
    if session is not None and session.compact_instructions:
        command = f"/compact {session.compact_instructions}"
    return await _run_raw_cli_command(agent, command, "Compacting")


async def clear(agent: str) -> CommandResult:
    """Clear an agent's conversation context (raw /clear)."""
    return await _run_raw_cli_command(agent, "/clear", "Clearing")


# ---------------------------------------------------------------------------
# Runners + process (Phase 8d)
# ---------------------------------------------------------------------------


async def run_flowchart(agent: str, name: str, args: str | None = None) -> CommandResult:
    """Run a flowchart command inline on a flowcoder agent (drives it through the hub so the
    flowchart engine wraps + runs the command)."""
    session = agents.agents.get(agent)
    if session is None:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    if session.agent_type != "flowcoder":
        return CommandResult(message="Flowcharts are only available for **flowcoder** agents.", ok=False, ephemeral=True)
    if session.query_lock.locked():
        return CommandResult(message=f"Agent **{agent}** is busy. Wait for it to finish.", ok=False, ephemeral=True)
    fc_name = name.lstrip("/")
    fc_args = args or ""
    slash_content = f"/{fc_name}" + (f" {fc_args}" if fc_args else "")
    if agents.hub is not None:
        await agents.hub.submit_user_message(agent, slash_content)
    return CommandResult(
        message=f"*System:* Flowchart `{fc_name}` started on **{agent}**.",
        data={"agent": agent, "flowchart": fc_name, "args": fc_args},
    )


async def _run_interview(agent: str, md_path: str, query: str, label: str) -> CommandResult:
    """Inject an interview instruction prompt into an agent as a raw turn (direct prompt,
    no flowchart wrap), streamed via the hub."""
    session = agents.agents.get(agent)
    if session is None:
        return CommandResult(message=f"Agent **{agent}** not found.", ok=False, ephemeral=True)
    if session.query_lock.locked():
        return CommandResult(message=f"Agent **{agent}** is busy. Wait for it to finish.", ok=False, ephemeral=True)
    if agents.hub is not None:
        await agents.hub.submit_user_message(agent, query, metadata={"raw": True})
    return CommandResult(message=f"*System:* {label} started for **{agent}**.", data={"agent": agent})


async def build_user_profile(agent: str) -> CommandResult:
    """Start the conversational user-profile interview on an agent."""
    md_path = os.path.join(config.BOT_DIR, ".claude", "commands", "build_user_profile.md")
    try:
        with open(md_path) as f:
            instructions = f.read()
    except OSError as e:
        return CommandResult(message=f"*System:* Could not read build_user_profile.md: {e}", ok=False, ephemeral=True)
    instructions = instructions.replace("%(axi_user_data)s", config.AXI_USER_DATA)
    query = (
        "The user has triggered the profile interview. "
        "Please conduct the interview now, following the instructions below exactly.\n\n"
        "--- PROFILE INTERVIEW INSTRUCTIONS ---\n\n" + instructions
    )
    return await _run_interview(agent, md_path, query, "Profile interview")


async def build_music_preferences(agent: str) -> CommandResult:
    """Start the music-preferences interview on an agent."""
    md_path = os.path.join(config.BOT_DIR, ".claude", "commands", "build_music_preferences.md")
    prefs_path = os.path.join(config.AXI_USER_DATA, "profile", "refs", "music-preferences.md")
    try:
        with open(md_path) as f:
            instructions = f.read()
    except OSError as e:
        return CommandResult(message=f"*System:* Could not read build_music_preferences.md: {e}", ok=False, ephemeral=True)
    instructions = instructions.replace("%(axi_user_data)s", config.AXI_USER_DATA)
    query = (
        "The user has triggered the music preferences interview. "
        "Please conduct the interview now, following the instructions below exactly. "
        f"Write results to `{prefs_path}` as you go.\n\n"
        "--- MUSIC PREFERENCES INTERVIEW INSTRUCTIONS ---\n\n" + instructions
    )
    return await _run_interview(agent, md_path, query, "Music preferences interview")


# Process control. restart-including-bridge needs Discord/process-specific wiring (bot.close +
# a goodbye post), so main.py registers a handler that commands_api triggers.
_full_restart_handler: Any = None


def set_full_restart_handler(fn: Any) -> None:
    global _full_restart_handler
    _full_restart_handler = fn


async def restart(force: bool = False) -> CommandResult:
    """Hot-reload the bot process (bridge stays alive) via the shutdown coordinator. The
    shutdown is fired in the background so the response is delivered before the process exits."""
    if agents.shutdown_coordinator is None:
        return CommandResult(message="Bot is not fully initialized yet.", ok=False, ephemeral=True)
    if force:
        agents.fire_and_forget(agents.shutdown_coordinator.force_shutdown("/restart force"))
        return CommandResult(message="*System:* Force restarting (hot reload)...", data={"force": True, "mode": "hot-reload"})
    agents.fire_and_forget(agents.shutdown_coordinator.graceful_shutdown("/restart command"))
    return CommandResult(
        message="*System:* Initiating graceful restart (hot reload)...", data={"force": False, "mode": "hot-reload"}
    )


async def restart_including_bridge(force: bool = False) -> CommandResult:
    """Full restart — kills the bridge + all agents (via the main.py-registered handler)."""
    if _full_restart_handler is None:
        return CommandResult(message="Full restart is not available yet.", ok=False, ephemeral=True)
    result = await _full_restart_handler(force)
    if isinstance(result, CommandResult):
        return result
    return CommandResult(message=str(result), data={"force": force, "mode": "full"})
