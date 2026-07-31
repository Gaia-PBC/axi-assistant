"""Phase 7.5d — lifecycle sleep callers routed through the hub.

restart_agent, reclaim_agent_name, and the ShutdownCoordinator sleep_fn now sleep via
hub.sleep(name, force=True) instead of the direct agents.sleep_agent, so the hub owns the
lifecycle transition (lifecycle=SLEEPING + on_sleep broadcast). reclaim keeps its manual
pop (not hub.remove_agent) so it does not emit on_kill / move the channel to Killed — the
name+channel are recycled by the incoming scheduled run.

(tools.py axi_kill_agent stays a direct sleep_agent: the session is popped from the registry
before the async teardown, so the name-based hub calls would KeyError/no-op; that path emits
its kill lifecycle event via move_channel_to_killed separately.)
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from axi.axi_types import AgentSession


class _FakeHub:
    def __init__(self) -> None:
        self.slept: list[tuple[str, bool]] = []

    async def sleep(self, name: str, *, force: bool = True) -> None:
        self.slept.append((name, force))


class _FakeRouter:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def post_system(self, name: str, text: str) -> None:
        self.posts.append((name, text))


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    from axi import agents

    agents.agents.clear()
    yield
    agents.agents.clear()


# ---------------------------------------------------------------------------
# reclaim_agent_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reclaim_agent_name_sleeps_via_hub_and_pops(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    router = _FakeRouter()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: router)

    agents.agents["old"] = AgentSession(name="old")

    await agents.reclaim_agent_name("old")

    assert hub.slept == [("old", True)]  # slept through the hub, force
    assert "old" not in agents.agents  # name freed (manual pop preserved)
    assert any("Recycled" in text for _, text in router.posts)


@pytest.mark.asyncio
async def test_reclaim_agent_name_absent_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    monkeypatch.setattr("axi.agents.hub", hub)

    await agents.reclaim_agent_name("ghost")  # not registered

    assert hub.slept == []


# ---------------------------------------------------------------------------
# restart_agent
# ---------------------------------------------------------------------------


def _stub_restart_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("axi.agents._load_agent_config", lambda name: {})
    monkeypatch.setattr("axi.agents.make_spawned_agent_system_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr("axi.agents.compute_prompt_hash", lambda p: "HASH")
    monkeypatch.setattr("axi.agents._save_agent_config", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_restart_agent_sleeps_via_hub_when_awake(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents.is_awake", lambda s: True)
    _stub_restart_config(monkeypatch)

    session = AgentSession(name="ra")
    agents.agents["ra"] = session

    out = await agents.restart_agent("ra")

    assert hub.slept == [("ra", True)]  # awake -> slept through the hub
    assert out is session
    assert session.system_prompt == "PROMPT"  # fresh prompt applied
    assert session.system_prompt_hash == "HASH"


@pytest.mark.asyncio
async def test_restart_agent_skips_sleep_when_asleep(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents.is_awake", lambda s: False)
    _stub_restart_config(monkeypatch)

    agents.agents["ra2"] = AgentSession(name="ra2")

    await agents.restart_agent("ra2")

    assert hub.slept == []  # already asleep -> no hub.sleep


# ---------------------------------------------------------------------------
# ShutdownCoordinator sleep_fn wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_coordinator_sleep_fn_routes_to_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    monkeypatch.setattr("axi.agents.hub", hub)

    coordinator = agents.make_shutdown_coordinator(
        close_bot_fn=lambda: None,
        kill_fn=lambda *a, **k: None,
        goodbye_fn=lambda *a, **k: None,
        bridge_mode=False,
    )

    # sleep_all only sleeps agents with a live client; register one in the shared registry.
    session = AgentSession(name="sd")
    session.client = object()
    agents.agents["sd"] = session

    await coordinator.sleep_all()

    assert hub.slept == [("sd", True)]  # shutdown sleep routed through the hub, force
