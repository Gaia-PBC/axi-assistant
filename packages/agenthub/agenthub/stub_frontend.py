"""StubFrontend — no-op/logging Frontend for integration testing.

Records all protocol calls in an in-memory log so tests can assert
on what the hub/router sent to frontends. Every method is a no-op
that appends a (method_name, kwargs) tuple to `self.log`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agenthub.frontend import PlanApprovalResult

if TYPE_CHECKING:
    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

_log = logging.getLogger(__name__)


@dataclass
class StubCall:
    """One recorded protocol call."""

    method: str
    args: dict[str, Any]


class StubFrontend:
    """Frontend that records all calls for test assertions.

    Usage::

        stub = StubFrontend()
        router.add(stub)
        # ... run code under test ...
        assert any(c.method == "post_message" for c in stub.log)
    """

    def __init__(self) -> None:
        self.log: list[StubCall] = []
        self._spawn_seq = 0

    def _record(self, method: str, **kwargs: Any) -> None:
        self.log.append(StubCall(method=method, args=kwargs))
        _log.debug("StubFrontend.%s(%s)", method, kwargs)

    def clear(self) -> None:
        self.log.clear()

    @property
    def name(self) -> str:
        return "stub"

    # --- Lifecycle ---

    async def start(self) -> None:
        self._record("start")

    async def stop(self) -> None:
        self._record("stop")

    # --- Outbound: hub -> frontend ---

    async def post_message(self, agent_name: str, text: str) -> None:
        self._record("post_message", agent_name=agent_name, text=text)

    async def post_system(self, agent_name: str, text: str) -> None:
        self._record("post_system", agent_name=agent_name, text=text)

    async def broadcast(self, text: str) -> None:
        self._record("broadcast", text=text)

    async def post_file(
        self, agent_name: str, filename: str, data: bytes, description: str = ""
    ) -> None:
        self._record(
            "post_file",
            agent_name=agent_name,
            filename=filename,
            data_len=len(data),
            description=description,
        )

    async def post_embed(self, agent_name: str, embed_data: dict[str, Any]) -> None:
        self._record("post_embed", agent_name=agent_name, embed_data=embed_data)

    # --- Typing / status ---

    async def set_typing(self, agent_name: str, is_typing: bool) -> None:
        self._record("set_typing", agent_name=agent_name, is_typing=is_typing)

    async def set_status(
        self, agent_name: str, status_text: str, emoji: str | None = None
    ) -> None:
        self._record("set_status", agent_name=agent_name, status_text=status_text, emoji=emoji)

    # --- Reactions ---

    async def post_reaction(self, agent_name: str, message_ref: Any, emoji: str) -> None:
        self._record("post_reaction", agent_name=agent_name, message_ref=message_ref, emoji=emoji)

    async def remove_reaction(
        self, agent_name: str, message_ref: Any, emoji: str
    ) -> None:
        self._record(
            "remove_reaction", agent_name=agent_name, message_ref=message_ref, emoji=emoji
        )

    # --- Agent lifecycle events ---

    async def on_wake(self, agent_name: str) -> None:
        self._record("on_wake", agent_name=agent_name)

    async def on_sleep(self, agent_name: str) -> None:
        self._record("on_sleep", agent_name=agent_name)

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        self._record("on_spawn", agent_name=agent_name)

    async def spawn_context(self, agent_name: str, session: Any) -> dict[str, Any]:
        self._record("spawn_context", agent_name=agent_name)
        self._spawn_seq += 1
        channel_id = 900_000_000_000_000_000 + self._spawn_seq
        return {
            "placeholders": {
                "channel_id": str(channel_id),
                "channel_name": agent_name,
                "guild_id": "0",
                "guild_name": "stub",
            },
            "routing_id": channel_id,
        }

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        self._record("on_kill", agent_name=agent_name, session_id=session_id)

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        self._record("on_session_id", agent_name=agent_name, session_id=session_id)

    async def on_channel_ready(self, agent_name: str) -> None:
        self._record("on_channel_ready", agent_name=agent_name)

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        self._record("on_idle_reminder", agent_name=agent_name, idle_minutes=idle_minutes)

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        self._record("on_reconnect", agent_name=agent_name, was_mid_task=was_mid_task)

    # --- Stream rendering ---

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        self._record("on_stream_event", agent_name=agent_name, event_type=type(event).__name__)

    # --- Interactive gates ---

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        self._record("request_plan_approval", agent_name=agent_name)
        return PlanApprovalResult(approved=True)

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        self._record("ask_question", agent_name=agent_name, questions=questions)
        return {}

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        self._record("update_todo", agent_name=agent_name, todos=todos)

    async def receive_input(self, agent_name: str) -> str:
        self._record("receive_input", agent_name=agent_name)
        return ""

    # --- Message history ---

    async def read_messages(
        self, agent_name: str, limit: int = 50, before: Any = None
    ) -> list[dict[str, Any]]:
        self._record("read_messages", agent_name=agent_name, limit=limit, before=before)
        return []

    async def search_messages(
        self, query: str, agent_name: str | None = None
    ) -> list[dict[str, Any]]:
        self._record("search_messages", query=query, agent_name=agent_name)
        return []

    # --- Event log integration ---

    async def on_log_event(self, event: LogEvent) -> None:
        self._record("on_log_event", event_type=type(event).__name__)
