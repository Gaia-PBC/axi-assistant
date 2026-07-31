"""Phase 7.3 (B2): the initial/startup prompt runs through the hub, channel-independent.

Verifies:
  - run_initial_prompt routes through hub.submit_user_message with NO Discord channel
    object (routing id comes from frontend_state), tagging metadata["initial_prompt"],
    and posts the "Initial prompt" notice via the frontend-agnostic router.
  - AxiTurnHooks.after_turn posts the "finished initial task" notice for a metadata-tagged
    initial-prompt turn (and NOT for a regular turn).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.stub_frontend import StubFrontend
from agenthub.types import SubmissionResult, TurnKind, TurnOutcome, TurnRequest
from axi import agents, hub_wiring
from axi.axi_types import AgentSession, discord_state


@pytest.fixture(autouse=True)
def _reset() -> None:
    agents.agents.clear()
    agents.channel_to_agent.clear()


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
async def test_run_initial_prompt_routes_through_hub_without_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, stub = _make_hub(monkeypatch)
    submit = AsyncMock(return_value=SubmissionResult(status="started", turn_id="t1"))
    monkeypatch.setattr(hub, "submit_user_message", submit)

    session = AgentSession(name="fresh-agent")
    # Routing id from frontend_state (set at spawn via G2's spawn_context) — NOT a channel object.
    discord_state(session).channel_id = 777
    agents.agents[session.name] = session

    # No Discord channel object exists for this agent (get_agent_channel would return None);
    # the pre-7.3 code dereferenced channel.id here and crashed.
    await agents.run_initial_prompt(session, "Say exactly: HI")

    submit.assert_awaited_once()
    call = submit.call_args
    assert call.args[0] == "fresh-agent"
    assert call.args[1] == "Say exactly: HI"
    assert call.kwargs.get("metadata", {}).get("initial_prompt") is True
    # "Initial prompt" notice posted via the frontend-agnostic router (recorded on the stub).
    assert any(
        c.method == "post_system" and "Initial prompt" in c.args.get("text", "") for c in stub.log
    ), stub.log


@pytest.mark.asyncio
async def test_run_initial_prompt_reports_when_hub_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub, stub = _make_hub(monkeypatch)
    monkeypatch.setattr(agents, "hub", None)

    session = AgentSession(name="fresh-agent")
    agents.agents[session.name] = session
    await agents.run_initial_prompt(session, "hi")  # must not raise

    assert any(
        c.method == "post_system" and "hub unavailable" in c.args.get("text", "") for c in stub.log
    ), stub.log


@pytest.mark.asyncio
async def test_after_turn_posts_finished_notice_for_initial_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub, stub = _make_hub(monkeypatch)
    monkeypatch.setattr(agents, "_maybe_compact", AsyncMock())
    from axi.turn_hooks import AxiTurnHooks

    session = AgentSession(name="fin-agent")
    agents.agents[session.name] = session
    turn = TurnRequest(
        turn_id="t1", kind=TurnKind.USER, content="Say hi", metadata={"initial_prompt": True, "turn_id": "t1"}
    )

    await AxiTurnHooks().after_turn(session, turn, TurnOutcome.COMPLETED)

    assert any(
        c.method == "post_system" and "finished initial task" in c.args.get("text", "") for c in stub.log
    ), stub.log


@pytest.mark.asyncio
async def test_after_turn_no_finished_notice_for_regular_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub, stub = _make_hub(monkeypatch)
    monkeypatch.setattr(agents, "_maybe_compact", AsyncMock())
    from axi.turn_hooks import AxiTurnHooks

    session = AgentSession(name="reg-agent")
    agents.agents[session.name] = session
    turn = TurnRequest(turn_id="t2", kind=TurnKind.USER, content="hi", metadata={"turn_id": "t2"})

    await AxiTurnHooks().after_turn(session, turn, TurnOutcome.COMPLETED)

    assert not any(
        c.method == "post_system" and "finished initial task" in c.args.get("text", "") for c in stub.log
    ), stub.log
