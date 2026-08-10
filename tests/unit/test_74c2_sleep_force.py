"""Phase 7.4c-2 — hub.sleep force param + busy-agent guard + on_sleep status refresh.

- non-force hub.sleep (auto-sleep + scheduler eviction) skips an agent whose
  query_lock is held (mid-turn), so eviction never sleeps a busy agent
- force hub.sleep (default) sleeps regardless
- hub.sleep sets lifecycle SLEEPING (consistency with hub.wake's IDLE)
- DiscordFrontend.on_sleep refreshes the channel status (moved from sleep_agent)
"""

from __future__ import annotations

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.types import AgentSession, LifecycleState


def _hub() -> AgentHub:
    async def _create(_session: object, _options: object) -> object:
        return object()

    async def _disconnect(_client: object, _name: str) -> None:
        return None

    def _opts(_session: object, resume_id: str | None) -> dict[str, object]:
        return {"resume": resume_id}

    return AgentHub(
        frontends=[FrontendRouter()],
        create_client=_create,
        disconnect_client=_disconnect,
        make_agent_options=_opts,
        max_awake=8,
    )


@pytest.mark.asyncio
async def test_nonforce_sleep_skips_busy_agent() -> None:
    hub = _hub()
    session = AgentSession(name="s1", cwd="")
    hub.sessions["s1"] = session
    await hub.wake("s1")
    assert session.client is not None

    await session.query_lock.acquire()  # simulate mid-turn
    try:
        await hub.sleep("s1", force=False)
        assert session.client is not None  # busy -> NOT slept
    finally:
        session.query_lock.release()

    await hub.sleep("s1", force=False)  # now idle -> sleeps
    assert session.client is None
    assert session.state.lifecycle is LifecycleState.SLEEPING


@pytest.mark.asyncio
async def test_force_sleep_ignores_busy_agent() -> None:
    hub = _hub()
    session = AgentSession(name="s2", cwd="")
    hub.sessions["s2"] = session
    await hub.wake("s2")

    await session.query_lock.acquire()
    try:
        await hub.sleep("s2")  # default force=True -> sleeps despite busy
        assert session.client is None
        assert session.state.lifecycle is LifecycleState.SLEEPING
    finally:
        session.query_lock.release()


@pytest.mark.asyncio
async def test_on_sleep_refreshes_channel_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import channels
    from axi.discord_frontend import DiscordFrontend

    called: list[int] = []
    monkeypatch.setattr(channels, "schedule_status_update", lambda: called.append(1))

    await DiscordFrontend(bot=object()).on_sleep("x")

    assert called == [1]
