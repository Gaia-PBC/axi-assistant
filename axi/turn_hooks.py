"""Axi's concrete TurnHooks — preserve ``process_message``'s per-turn behavior on the hub.

Maps the :class:`agenthub.TurnHooks` extension points onto the Axi-specific behaviors
that ``agents.process_message`` performs today, so that a turn driven by the hub's
``submit_user_message`` behaves like the legacy path.  Imports from ``axi.agents`` are
lazy to avoid a circular import (``agents.py`` builds the hub via ``hub_wiring``).

NOTE: this is not yet wired into ``create_hub``.  Phase 7.2 attaches it when the live
inbound/turn path switches from ``agents.process_message`` to ``hub.submit_user_message``.
``before_turn`` sources the log-context channel id from the session's frontend state, so
it is ``None`` (harmless) under a non-Discord frontend.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from agenthub.turn_hooks import TurnHooks

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agenthub.types import AgentSession, MessageContent, TurnOutcome, TurnRequest


def _channel_id_of(session: AgentSession) -> int | None:
    """Best-effort Discord channel id for the log context (None under a non-Discord frontend)."""
    from axi.axi_types import discord_state

    return discord_state(session).channel_id


class AxiTurnHooks(TurnHooks):
    """Preserve FlowCoder wrapping, OTEL tracing, buffer drains, log context, and auto-compaction."""

    async def before_turn(self, session: AgentSession, turn: TurnRequest) -> None:
        from axi.agents import _reset_session_activity, drain_sdk_buffer, drain_stderr
        from axi.log_context import set_agent_context

        set_agent_context(session.name, channel_id=_channel_id_of(session))
        _reset_session_activity(session)
        session.bridge_busy = False
        drain_stderr(session)
        drain_sdk_buffer(session)

    async def transform_content(self, session: AgentSession, content: MessageContent) -> MessageContent:
        from axi.agents import _wrap_content_with_flowchart

        return _wrap_content_with_flowchart(content, session)

    async def after_turn(self, session: AgentSession, turn: TurnRequest, outcome: TurnOutcome) -> None:
        # Post-turn: initial-prompt finished-notice (7.3), then compaction handling (7.5b).
        # Compaction is fully hub-driven now: a CLI auto-compaction during this turn records
        # _pending_compact (via the hub stream factory), and the post-compaction "Continue
        # from where you left off." auto-resume + the proactive /compact are queued as
        # follow-up hub turns rather than nested client re-queries.
        from agenthub.types import TurnOutcome
        from axi.agents import _get_router, _handle_pending_compact, _maybe_compact, _user_mentions

        meta = turn.metadata if (turn is not None and isinstance(turn.metadata, dict)) else {}

        # B2 (7.3): initial/startup prompts now run through the hub. run_initial_prompt
        # tags its turn with metadata["initial_prompt"]; post the completion notice here
        # (the legacy run_initial_prompt posted it after process_message returned).
        if meta.get("initial_prompt"):
            if outcome == TurnOutcome.COMPLETED:
                await _get_router().post_system(
                    session.name, f"Agent **{session.name}** finished initial task. {_user_mentions()}"
                )
            elif outcome in (
                TurnOutcome.ERROR,
                TurnOutcome.TIMEOUT,
                TurnOutcome.RATE_LIMIT,
                TurnOutcome.RETRY_EXHAUSTED,
            ):
                await _get_router().post_system(
                    session.name,
                    f"Agent **{session.name}** encountered an error during initial task. {_user_mentions()}",
                )

        # 7.5b: this was a self-triggered /compact turn — clear the flag now its stream is done.
        if meta.get("compaction"):
            from axi.discord_stream import _self_compacting

            _self_compacting.discard(session.name)

        # 7.5b: if the CLI compacted during this turn, post the completion summary and queue
        # the "Continue from where you left off." auto-resume as a follow-up hub turn.
        resumed = await _handle_pending_compact(session)

        # Proactive threshold compaction — queues a /compact hub turn. Skip right after a
        # /compact turn or when an auto-resume was just queued, to avoid re-compaction loops.
        if not meta.get("compaction") and not resumed:
            await _maybe_compact(session)

    def turn_scope(self, session: AgentSession, turn: TurnRequest) -> Any:
        return self._span_scope(session, turn)

    @contextlib.asynccontextmanager
    async def _span_scope(self, session: AgentSession, turn: TurnRequest) -> AsyncIterator[None]:
        from axi.agents import _active_trace_ids, _tracer

        with _tracer.start_as_current_span(
            "process_message",
            attributes={
                "agent.name": session.name,
                "agent.type": session.agent_type or "claude_code",
            },
        ) as span:
            sc = span.get_span_context()
            if sc and sc.trace_id:
                _active_trace_ids[session.name] = f"[trace={format(sc.trace_id, '032x')[:16]}]"
            try:
                yield
            finally:
                _active_trace_ids.pop(session.name, None)
