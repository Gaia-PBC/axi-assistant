"""Turn lifecycle hooks for the AgentHub runtime.

Extension points that let a wiring/frontend layer decorate the hub's turn loop
with behavior the core runtime must not know about: a content transform, pre/post
steps, and a scope (async context manager) wrapping the query+consume.  The base
class is a pure no-op, so an :class:`~agenthub.runtime.AgentHub` constructed without
hooks behaves exactly as it did before hooks existed.

The runtime never imports a concrete implementation — it only calls these methods
on whatever object it was handed, exactly as it does with frontends.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agenthub.types import AgentSession, MessageContent, TurnOutcome, TurnRequest


@contextlib.asynccontextmanager
async def _noop_scope() -> AsyncIterator[None]:
    yield


class TurnHooks:
    """No-op turn hooks. Subclass and override only the points you need.

    Every method is safe for the runtime to call unconditionally; the defaults do
    nothing, and ``transform_content`` returns its input unchanged.
    """

    async def before_turn(self, session: AgentSession, turn: TurnRequest) -> None:
        """Run after wake, immediately before ``client.query()`` — per-turn setup."""

    async def transform_content(self, session: AgentSession, content: MessageContent) -> MessageContent:
        """Transform the outgoing turn content. Returns it unchanged by default."""
        return content

    async def after_turn(self, session: AgentSession, turn: TurnRequest, outcome: TurnOutcome) -> None:
        """Run after the stream is fully consumed, still inside the turn timeout."""

    def turn_scope(self, session: AgentSession, turn: TurnRequest) -> Any:
        """Return an async context manager wrapping the whole query+consume.

        Default is a no-op scope. Override to wrap the turn in e.g. a tracing span.
        The returned object must support ``async with``.
        """
        return _noop_scope()
