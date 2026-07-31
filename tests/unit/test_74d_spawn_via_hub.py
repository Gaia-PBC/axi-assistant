"""Phase 7.4d — agents.spawn_agent routes session-creation through hub.spawn_agent.

Verifies the hub.spawn_agent contract the repoint relies on:
  - carries the generic AgentSession fields (system_prompt_hash, mcp_server_names,
    startup_command(+args), extra_excluded_commands, extra_write_dirs, model, session_id)
  - registers the session in the shared sessions dict
  - broadcasts on_spawn exactly once (no double broadcast)
  - system_prompt_hash is set BEFORE on_spawn fires (so a frontend can format its
    channel topic from it)
"""

from __future__ import annotations

from typing import Any

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.stub_frontend import StubFrontend


def _hub(*frontends: Any) -> AgentHub:
    router = FrontendRouter()
    for fe in frontends:
        router.add(fe)

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
async def test_spawn_agent_carries_fields_and_registers() -> None:
    stub = StubFrontend()
    hub = _hub(stub)

    session = await hub.spawn_agent(
        name="sp1",
        cwd="",
        agent_type="flowcoder",
        system_prompt={"append": "x"},
        system_prompt_hash="HASH123",
        session_id="resume-abc",
        mcp_server_names=["a"],
        startup_command="build",
        startup_command_args="--foo",
        extra_excluded_commands=["ssh"],
        extra_write_dirs=["/tmp/x"],
        model="haiku",
    )

    assert session.system_prompt_hash == "HASH123"
    assert session.session_id == "resume-abc"
    assert session.agent_type == "flowcoder"
    assert session.mcp_server_names == ["a"]
    assert session.startup_command == "build"
    assert session.startup_command_args == "--foo"
    assert session.extra_excluded_commands == ["ssh"]
    assert session.extra_write_dirs == ["/tmp/x"]
    assert session.model == "haiku"
    assert hub.sessions["sp1"] is session

    on_spawns = [c for c in stub.log if c.method == "on_spawn"]
    assert len(on_spawns) == 1  # broadcast exactly once


class _CapturingFrontend:
    """Captures session.system_prompt_hash at the moment on_spawn fires."""

    def __init__(self) -> None:
        self.hash_at_spawn: str | None = "UNSET"

    @property
    def name(self) -> str:
        return "cap"

    async def on_spawn(self, _agent_name: str, session: Any) -> None:
        self.hash_at_spawn = session.system_prompt_hash


@pytest.mark.asyncio
async def test_system_prompt_hash_set_before_on_spawn() -> None:
    cap = _CapturingFrontend()
    hub = _hub(cap)

    await hub.spawn_agent(name="sp2", cwd="", system_prompt_hash="HASHXYZ")

    assert cap.hash_at_spawn == "HASHXYZ"  # available when on_spawn ran
