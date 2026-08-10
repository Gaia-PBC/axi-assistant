"""Phase 7.4c — lifecycle repoint: cwd-check in hub wake + hub.remove_agent for kill.

The main.py wake/kill call-site swaps are exercised by the 7.4e live smoke; here we
verify the underlying hub behaviors the repoint now relies on:
  - hub.wake refuses an agent whose cwd is gone (moved from agents.wake_agent)
  - hub.remove_agent sleeps + broadcasts on_kill + pops the session (what kill_agent
    used to do via pop + sleep_agent(force) + move_channel_to_killed)
"""

from __future__ import annotations

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.stub_frontend import StubFrontend
from agenthub.types import AgentSession


def _hub(stub: StubFrontend | None = None) -> AgentHub:
    router = FrontendRouter()
    if stub is not None:
        router.add(stub)

    async def _create(_session: object, _options: object) -> object:
        return object()

    async def _disconnect(_client: object, _name: str) -> None:
        return None

    def _opts(_session: object, resume_id: str | None) -> dict[str, object]:
        return {"resume": resume_id}

    return AgentHub(
        frontends=[router],
        create_client=_create,
        disconnect_client=_disconnect,
        make_agent_options=_opts,
        max_awake=8,
    )


@pytest.mark.asyncio
async def test_wake_refuses_missing_cwd() -> None:
    hub = _hub()
    session = AgentSession(name="c1", cwd="/no/such/dir/xyz123")
    hub.sessions["c1"] = session

    with pytest.raises(ValueError, match="working directory no longer exists"):
        await hub.wake("c1")

    assert session.client is None  # never created a client


@pytest.mark.asyncio
async def test_wake_allows_empty_cwd() -> None:
    hub = _hub()
    session = AgentSession(name="c2", cwd="")  # no cwd -> no check
    hub.sessions["c2"] = session

    await hub.wake("c2")

    assert session.client is not None


@pytest.mark.asyncio
async def test_remove_agent_sleeps_broadcasts_on_kill_and_pops() -> None:
    stub = StubFrontend()
    hub = _hub(stub=stub)
    session = AgentSession(name="k1", cwd="")
    hub.sessions["k1"] = session

    await hub.wake("k1")  # acquire slot + client
    assert session.client is not None

    await hub.remove_agent("k1")

    assert "k1" not in hub.sessions  # popped from registry
    assert session.client is None  # slept
    assert any(c.method == "on_kill" and c.args.get("agent_name") == "k1" for c in stub.log), stub.log
