"""Test Phase 8 (capstone) — every slash command works over Discord AND REST, one code path.

The per-batch suites (8a-d, 55 tests) verify each command's function + endpoint behave. This
ties them together structurally:
  - all 25 slash commands are registered on bot.tree;
  - all 23 API-exposed commands have a /v1 route (voice is Discord-only, route-less);
  - every API-exposed Discord handler DELEGATES to commands_api (thin wrapper), and voice does not;
  - a representative Discord-vs-HTTP parity check goes through the same commands_api core.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

# command name -> the /v1 route that exposes it (23 API-exposed commands).
COMMAND_ROUTES = {
    "ping": "/v1/ping",
    "claude-usage": "/v1/usage",
    "list-agents": "/v1/agents",
    "status": "/v1/status",
    "flowchart-list": "/v1/flowcharts",
    "stop": "/v1/agents/{name}/stop",
    "skip": "/v1/agents/{name}/skip",
    "spawn": "/v1/spawn",
    "kill-agent": "/v1/agents/{name}/kill",
    "restart-agent": "/v1/agents/{name}/restart",
    "reset-context": "/v1/agents/{name}/reset",
    "compact": "/v1/agents/{name}/compact",
    "clear": "/v1/agents/{name}/clear",
    "model": "/v1/model",
    "verbose": "/v1/agents/{name}/verbose",
    "debug": "/v1/agents/{name}/debug",
    "debug-all": "/v1/debug-all",
    "plan": "/v1/agents/{name}/plan",
    "flowchart": "/v1/flowchart",
    "build-user-profile": "/v1/agents/{name}/build-profile",
    "build-music-preferences": "/v1/agents/{name}/build-music",
    "restart": "/v1/restart",
    "restart-including-bridge": "/v1/restart-including-bridge",
}
VOICE = {"vc-join", "vc-leave"}  # Discord-only, no HTTP


def _slash_names() -> set[str]:
    import axi.main as m

    return {c.name for c in m.bot.tree.get_commands()}


def _v1_paths() -> set[str]:
    from axi import http_api

    return {r.path for r in http_api.app.routes if getattr(r, "path", "").startswith("/v1")}


def test_all_25_slash_commands_registered() -> None:
    expected = set(COMMAND_ROUTES) | VOICE
    assert len(expected) == 25
    missing = expected - _slash_names()
    assert not missing, f"slash commands not registered: {missing}"


def test_api_exposed_commands_have_v1_routes() -> None:
    paths = _v1_paths()
    for cmd, route in COMMAND_ROUTES.items():
        assert route in paths, f"{cmd} missing its REST route {route}"


def test_voice_commands_are_discord_only() -> None:
    from axi import commands_api

    # registered as slash commands...
    assert VOICE <= _slash_names()
    # ...but no /v1 route and no shared function
    assert not any("/vc" in p or "voice" in p for p in _v1_paths())
    assert not hasattr(commands_api, "vc_join")
    assert not hasattr(commands_api, "vc_leave")


def test_api_handlers_delegate_to_commands_api() -> None:
    import axi.main as m

    for cmd in m.bot.tree.get_commands():
        src = inspect.getsource(cmd.callback)
        if cmd.name in COMMAND_ROUTES:
            assert "commands_api" in src, f"/{cmd.name} handler should delegate to commands_api"
        elif cmd.name in VOICE:
            assert "commands_api" not in src, f"/{cmd.name} is Discord-only, should not use commands_api"


def test_every_route_maps_to_a_known_command_or_trigger() -> None:
    # No orphan /v1 routes: each is a Phase-8 command route, a status sub-route, /metrics, or /v1/trigger.
    known = set(COMMAND_ROUTES.values()) | {"/v1/agents/{name}/status", "/v1/trigger"}
    for p in _v1_paths():
        assert p in known, f"unexpected /v1 route: {p}"


@pytest.mark.asyncio
async def test_discord_http_parity_via_shared_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read command over HTTP goes through the exact commands_api core the Discord handler uses."""
    from fastapi.testclient import TestClient

    from axi import agents, commands_api
    from axi import config as cfg
    from axi import http_api

    monkeypatch.setattr(cfg, "HTTP_API_TOKEN", "")
    saved = dict(agents.agents)
    agents.agents.clear()
    try:
        direct = commands_api.list_agents()  # what the Discord handler calls
        client = TestClient(http_api.app)
        via_http = client.get("/v1/agents").json()
        assert via_http["message"] == direct.message
        assert via_http["data"] == direct.data
    finally:
        agents.agents.clear()
        agents.agents.update(saved)
