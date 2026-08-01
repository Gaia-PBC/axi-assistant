"""Whole-migration acceptance test — the 6-point criterion via StubFrontend (headless).

Point 1: hub.spawn_agent works.
Point 2: hub.submit_user_message processes + streams.
Point 3: hub.remove_agent cleans up.
Point 4: all MCP tools resolve to StubFrontend.          }  Phase 9 (MCP-tool-abstraction),
Point 5: no discord_* tool names in agent prompts.       }  owned by the PARALLEL agent's deck
Point 6: import axi.config works without the discord package.

Points 1-3 + 6 are this deck's Phase 7-8 work and pass here. Points 4-5 depend on Phase 9
(deck ms9dc99inc0n6ucdcp), which has NOT landed on this branch — they are SKIPPED with a guard
so full acceptance is not claimed until they pass.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from agenthub import AgentHub, FrontendRouter
from agenthub.stream_types import QueryResult, StreamEnd, StreamStart
from agenthub.stub_frontend import StubFrontend


class _FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: list[Any] = []

    async def query(self, content: Any) -> None:
        self.queries.append(content)

    async def interrupt(self) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


async def _ok_stream(session: Any, **kwargs: Any) -> Any:
    yield StreamStart()
    yield QueryResult(session_id=f"sid-{session.name}", cost_usd=0.0, num_turns=1, duration_ms=1)
    yield StreamEnd(elapsed_s=0.0, msg_count=1, flush_count=0)


def _patch_axi_deps(monkeypatch: pytest.MonkeyPatch, stub: StubFrontend) -> None:
    monkeypatch.setattr("axi.agents._reset_session_activity", lambda s: None)
    monkeypatch.setattr("axi.agents.drain_stderr", lambda s: [])
    monkeypatch.setattr("axi.agents.drain_sdk_buffer", lambda s: 0)
    monkeypatch.setattr("axi.agents._wrap_content_with_flowchart", lambda content, session: content)
    monkeypatch.setattr("axi.turn_hooks._channel_id_of", lambda s: None)
    monkeypatch.setattr("axi.agents._get_router", lambda: stub)


def _build_hub(stub: StubFrontend, box: dict[str, _FakeClient]) -> AgentHub:
    from axi.turn_hooks import AxiTurnHooks

    async def create_client(session: Any, options: Any) -> _FakeClient:
        c = _FakeClient(session.name)
        box[session.name] = c
        return c

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    return AgentHub(
        frontends=[stub],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda s, i: {},
        max_awake=4,
        query_timeout=2.0,
        stream_factory=_ok_stream,
        turn_hooks=AxiTurnHooks(),
    )


async def _wait(pred: Any, tries: int = 150) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_acceptance_points_1_2_3_hub_lifecycle_via_stubfrontend(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubFrontend()
    box: dict[str, _FakeClient] = {}
    _patch_axi_deps(monkeypatch, stub)
    hub = _build_hub(stub, box)
    monkeypatch.setattr("axi.agents.hub", hub)

    # POINT 1 — hub.spawn_agent works (broadcasts on_spawn, registers the session).
    await hub.spawn_agent(name="acc", cwd=tempfile.mkdtemp(prefix="acc_"))
    assert any(c.method == "on_spawn" for c in stub.log), "point 1: spawn did not broadcast"
    assert "acc" in hub.sessions

    # POINT 2 — hub.submit_user_message processes + streams (client queried, on_stream_event fired).
    await hub.submit_user_message("acc", "hello")
    await _wait(lambda: any(c.method == "on_stream_event" for c in stub.log))
    assert box["acc"].queries == ["hello"], "point 2: client did not receive the query"
    assert any(c.method == "on_stream_event" for c in stub.log), "point 2: stream not rendered to frontend"
    await _wait(lambda: any(c.method == "on_sleep" for c in stub.log))

    # POINT 3 — hub.remove_agent cleans up (on_kill + session removed).
    await hub.remove_agent("acc")
    assert any(c.method == "on_kill" for c in stub.log), "point 3: remove did not broadcast on_kill"
    assert "acc" not in hub.sessions, "point 3: session not removed"

    # Zero Discord: only a StubFrontend was ever attached.
    assert hub.frontends == [stub]


def test_acceptance_point_6_config_imports_without_discord() -> None:
    # The criterion is that axi.config IMPORTS SUCCESSFULLY when the discord package is not
    # installed. Simulate that by blocking discord/discordquery imports in a fresh interpreter;
    # config must still import (via its ImportError re-export fallback).
    env = {**os.environ, "DISCORD_TOKEN": "dummy", "ALLOWED_USER_IDS": "1", "DISCORD_GUILD_ID": "1"}
    code = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'discord' or name.startswith('discord.') or name == 'discordquery':\n"
        "            raise ImportError('blocked ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import axi.config\n"
        "assert 'discord' not in sys.modules, 'discord got imported despite the block'\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, (
        f"point 6: axi.config failed to import without the discord package.\nstderr:\n{r.stderr}"
    )


@pytest.mark.skip(
    reason="Points 4-5 (all MCP tools resolve to StubFrontend / no discord_* tool names in agent "
    "prompts) are Phase 9 MCP-tool-abstraction, owned by the parallel agent's deck "
    "ms9dc99inc0n6ucdcp. Not landed on this branch — full acceptance is not claimed until this passes."
)
def test_acceptance_points_4_5_mcp_tools_frontend_agnostic() -> None:
    # When Phase 9 lands: MCP tools should route through the FrontendRouter (resolving to whatever
    # frontend, incl. StubFrontend), and no discord_* tool names should remain in agent prompts
    # except the documented Discord-only carve-outs.
    from axi import tools

    discord_tools = [n for n in dir(tools) if n.startswith("discord_")]
    assert not discord_tools, f"discord_* MCP tools still present (Phase 9 pending): {discord_tools}"
