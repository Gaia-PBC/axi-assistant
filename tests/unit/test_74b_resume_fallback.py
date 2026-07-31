"""Phase 7.4b — resume-with-fresh-fallback in the hub wake path (_ensure_awake).

Closes the latent 7.2 gap: a hub-driven wake whose resume fails now retries once
with a fresh session (session_id cleared, last_failed_resume_id retained) instead
of failing the wake. Mirrors the legacy agents.wake_agent fallback.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.types import AgentSession


def _build_hub(create_client: Callable[..., Any]) -> AgentHub:
    async def _disconnect(_client: object, _name: str) -> None:
        return None

    def _make_options(_session: object, resume_id: str | None) -> dict[str, Any]:
        return {"resume": resume_id}

    return AgentHub(
        frontends=[FrontendRouter()],
        create_client=create_client,
        disconnect_client=_disconnect,
        make_agent_options=_make_options,
        max_awake=8,
    )


@pytest.mark.asyncio
async def test_resume_failure_retries_fresh_and_clears_session_id() -> None:
    calls: list[str | None] = []

    async def _create(_session: object, options: dict[str, Any]) -> object:
        calls.append(options["resume"])
        if options["resume"] is not None:
            raise RuntimeError("resume rejected")
        return object()

    hub = _build_hub(_create)
    session = AgentSession(name="r1")
    session.session_id = "old-sess"
    hub.sessions["r1"] = session

    await hub.wake("r1")

    assert session.client is not None  # agent woke
    assert session.session_id is None  # resume cleared after fresh fallback
    assert session.last_failed_resume_id == "old-sess"
    assert calls == ["old-sess", None]  # resume attempt, then fresh retry


@pytest.mark.asyncio
async def test_successful_resume_keeps_session_id() -> None:
    async def _create(_session: object, _options: dict[str, Any]) -> object:
        return object()

    hub = _build_hub(_create)
    session = AgentSession(name="r2")
    session.session_id = "sess2"
    hub.sessions["r2"] = session

    await hub.wake("r2")

    assert session.client is not None
    assert session.session_id == "sess2"  # unchanged
    assert session.last_failed_resume_id is None


@pytest.mark.asyncio
async def test_fresh_wake_failure_raises_without_retry() -> None:
    calls: list[str | None] = []

    async def _create(_session: object, options: dict[str, Any]) -> object:
        calls.append(options["resume"])
        raise RuntimeError("boom")

    hub = _build_hub(_create)
    session = AgentSession(name="r3")  # no session_id -> fresh wake, no fallback
    hub.sessions["r3"] = session

    with pytest.raises(RuntimeError):
        await hub.wake("r3")

    assert calls == [None]  # single attempt, no retry
    assert session.client is None
