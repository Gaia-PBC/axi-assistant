"""G2 + G4: frontend-agnostic spawn context and inter-agent hub routing.

Verifies under StubFrontend (no Discord):
  G2 — a spawned agent's prompt has all placeholders substituted and the session
       routing id (channel_id) is set (not None), driven by the generic spawn
       path (router.spawn_context -> agents._apply_spawn_context).
  G4 — an inter-agent message is delivered through hub.submit_inter_agent_message
       (landing in the hub's queued_turns, NOT the legacy message_queue), the
       delivery notification is recorded on the stub, and there is no
       "No Discord channel found" early-return.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.stub_frontend import StubFrontend
from agenthub.types import TurnKind, TurnRequest
from axi import agents, config, hub_wiring
from axi.axi_types import AgentSession, discord_state


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    agents.agents.clear()
    agents.channel_to_agent.clear()


# --------------------------------------------------------------------------- #
# G2 — spawn-prompt substitution + routing id via the generic path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_g2_spawn_context_substitutes_placeholders_under_stub() -> None:
    stub = StubFrontend()
    router = FrontendRouter()
    router.add(stub)

    session = AgentSession(
        name="tester",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "Channel #{channel_name} (ID: {channel_id}) — server {guild_name} ({guild_id})",
        },
    )

    ctx = await router.spawn_context("tester", session)
    agents._apply_spawn_context(session, ctx)

    append = session.system_prompt["append"]
    for placeholder in ("{channel_id}", "{guild_id}", "{channel_name}", "{guild_name}"):
        assert placeholder not in append, f"{placeholder} survived: {append!r}"
    # Routing id assigned generically (not None) even without a real Discord channel.
    assert discord_state(session).channel_id is not None
    # The stub was actually consulted for the context.
    assert any(c.method == "spawn_context" for c in stub.log)


@pytest.mark.asyncio
async def test_g2_spawn_context_default_is_safe_without_frontend() -> None:
    router = FrontendRouter()  # no frontends registered
    ctx = await router.spawn_context("x", AgentSession(name="x"))
    assert ctx == {"placeholders": {}, "routing_id": None}


def test_g2_apply_spawn_context_ignores_none_routing_and_empty() -> None:
    session = AgentSession(
        name="x",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "keep {channel_id}"},
    )
    agents._apply_spawn_context(session, {"placeholders": {}, "routing_id": None})
    # Nothing supplied -> prompt unchanged, channel_id stays None (no crash).
    assert session.system_prompt["append"] == "keep {channel_id}"
    assert discord_state(session).channel_id is None


# --------------------------------------------------------------------------- #
# G4 — inter-agent delivery through the hub (no Discord channel required)
# --------------------------------------------------------------------------- #


def _make_hub(monkeypatch: pytest.MonkeyPatch) -> tuple[AgentHub, StubFrontend]:
    stub = StubFrontend()
    router = FrontendRouter()
    router.add(stub)
    monkeypatch.setattr(hub_wiring, "router", router)

    async def _create_client(_session: object, _options: object) -> object:
        return object()

    async def _disconnect_client(_client: object, _name: str) -> None:
        return None

    def _make_agent_options(_session: object, _session_id: object) -> dict[str, object]:
        return {}

    hub = AgentHub(
        frontends=[router],
        create_client=_create_client,
        disconnect_client=_disconnect_client,
        make_agent_options=_make_agent_options,
        max_awake=8,
    )
    hub.sessions = agents.agents
    monkeypatch.setattr(agents, "hub", hub)
    return hub, stub


@pytest.mark.asyncio
async def test_g4_inter_agent_delivers_to_busy_agent_via_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub, stub = _make_hub(monkeypatch)
    monkeypatch.setattr(agents, "graceful_interrupt", AsyncMock(return_value=True))

    target = AgentSession(name="target-agent")
    target.client = object()
    agents.agents[target.name] = target
    # Busy with an in-flight turn: the inter-agent message must queue in the hub.
    target.state.current_turn = TurnRequest(turn_id="busy", kind=TurnKind.USER, content="[busy]")

    result = await agents.deliver_inter_agent_message("sender-agent", target, "hello there")

    # No channel-dependent early return.
    assert "No Discord channel" not in result
    assert result == "delivered to busy agent 'target-agent' (interrupted, will process next)"
    # Routed through the HUB queue, not the legacy message_queue (the 7.2 orphan path).
    assert len(target.state.queued_turns) == 1
    turn = target.state.queued_turns[0]
    assert turn.kind == TurnKind.INTER_AGENT
    assert "[Inter-agent message from sender-agent] hello there" in turn.content
    assert len(target.message_queue) == 0
    # Busy target was interrupted so the hub drains the inter-agent turn next.
    agents.graceful_interrupt.assert_awaited_once()
    # Delivery notification recorded on the stub (frontend-agnostic post_system).
    assert any(
        c.method == "post_system" and "Message from sender-agent" in c.args.get("text", "")
        for c in stub.log
    ), stub.log


@pytest.mark.asyncio
async def test_g4_compacting_target_queues_without_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub, _stub = _make_hub(monkeypatch)
    interrupt = AsyncMock(return_value=True)
    monkeypatch.setattr(agents, "graceful_interrupt", interrupt)

    target = AgentSession(name="target-agent")
    agents.agents[target.name] = target
    target.state.current_turn = TurnRequest(turn_id="busy", kind=TurnKind.USER, content="[busy]")
    target.compacting = True

    result = await agents.deliver_inter_agent_message("sender-agent", target, "later")

    assert "queued, will process after compaction" in result
    assert len(target.state.queued_turns) == 1
    # Compaction must NOT be interrupted.
    interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_g4_hub_unavailable_reports_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "hub", None)
    target = AgentSession(name="target-agent")
    agents.agents[target.name] = target
    result = await agents.deliver_inter_agent_message("sender-agent", target, "hi")
    assert "hub unavailable" in result
