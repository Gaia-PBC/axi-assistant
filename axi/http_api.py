"""Minimal HTTP API for triggering agent sessions from external processes.

Provides a single POST /v1/trigger endpoint that spawns or routes to an agent.
Started as an asyncio task inside the bot's event loop when HTTP_API_PORT != 0.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from axi import agents, commands_api, config
from axi.metrics import metrics_content_type, observe_http_request, render_latest_metrics


def _result_json(result: commands_api.CommandResult) -> dict:
    """Serialize a CommandResult for a JSON response."""
    return {"ok": result.ok, "message": result.message, "data": result.data}

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger("axi")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def prometheus_http_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    started_at = time.monotonic()
    route_label = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        observe_http_request(route_label, request.method, 500, time.monotonic() - started_at)
        raise
    observe_http_request(route_label, request.method, response.status_code, time.monotonic() - started_at)
    return response


class TriggerRequest(BaseModel):
    session: str
    prompt: str
    cwd: str | None = None
    extensions: list[str] | None = None
    mcp_servers: list[str] | None = None


async def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    # Empty HTTP_API_TOKEN is only safe because main.py refuses to start on non-loopback without it.
    expected = config.HTTP_API_TOKEN
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = authorization[len("Bearer ") :]
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=render_latest_metrics(), media_type=metrics_content_type())


# ---------------------------------------------------------------------------
# Read/info commands (Phase 8a) — GET endpoints mirroring the Discord slash commands.
# ---------------------------------------------------------------------------


@app.get("/v1/ping")
async def api_ping(_: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.ping())


@app.get("/v1/agents")
async def api_list_agents(_: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.list_agents())


@app.get("/v1/status")
async def api_all_status(_: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.agent_status(None))


@app.get("/v1/agents/{name}/status")
async def api_agent_status(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.agent_status(name))


@app.get("/v1/usage")
async def api_usage(history: int | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.claude_usage(history))


@app.get("/v1/flowcharts")
async def api_flowcharts(_: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.flowchart_list())


# ---------------------------------------------------------------------------
# Turn control + lifecycle commands (Phase 8b) — POST endpoints.
# ---------------------------------------------------------------------------


class SpawnRequest(BaseModel):
    name: str
    prompt: str
    cwd: str | None = None
    resume: str | None = None
    model: str | None = None
    provider: str | None = None


@app.post("/v1/agents/{name}/stop")
async def api_stop(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.stop(name))


@app.post("/v1/agents/{name}/skip")
async def api_skip(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.skip(name))


@app.post("/v1/agents/{name}/kill")
async def api_kill(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.kill_agent(name))


@app.post("/v1/agents/{name}/restart")
async def api_restart_agent(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.restart_agent(name))


@app.post("/v1/spawn")
async def api_spawn(req: SpawnRequest, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(
        await commands_api.spawn(req.name, req.prompt, cwd=req.cwd, resume=req.resume, model=req.model, provider=req.provider)
    )


# ---------------------------------------------------------------------------
# Context + scope commands (Phase 8c). Scope commands take an explicit agent
# target in the body (null = global/default); /debug-all is the all-agents variant.
# ---------------------------------------------------------------------------


class ResetRequest(BaseModel):
    cwd: str | None = None


class ModelRequest(BaseModel):
    agent: str | None = None
    model: str | None = None
    provider: str | None = None


class ModeRequest(BaseModel):
    mode: str | None = None


@app.post("/v1/agents/{name}/reset")
async def api_reset(name: str, req: ResetRequest | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.reset_context(name, cwd=(req.cwd if req else None)))


@app.post("/v1/model")
async def api_model(req: ModelRequest, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.set_model(req.agent, req.model, provider=req.provider))


@app.post("/v1/agents/{name}/verbose")
async def api_verbose(name: str, req: ModeRequest | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.set_verbose(name, req.mode if req else None))


@app.post("/v1/agents/{name}/debug")
async def api_debug(name: str, req: ModeRequest | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.set_debug(name, req.mode if req else None))


@app.post("/v1/debug-all")
async def api_debug_all(req: ModeRequest | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(commands_api.set_debug_all(req.mode if req else None))


@app.post("/v1/agents/{name}/plan")
async def api_plan(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.set_plan(name))


@app.post("/v1/agents/{name}/compact")
async def api_compact(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.compact(name))


@app.post("/v1/agents/{name}/clear")
async def api_clear(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.clear(name))


# ---------------------------------------------------------------------------
# Runners + process control (Phase 8d). Voice (vc-join/vc-leave) is Discord-only.
# restart / restart-including-bridge are a powerful surface, gated by the bearer token.
# ---------------------------------------------------------------------------


class FlowchartRequest(BaseModel):
    agent: str
    flowchart: str
    args: str | None = None


class ForceRequest(BaseModel):
    force: bool = False


@app.post("/v1/flowchart")
async def api_flowchart(req: FlowchartRequest, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.run_flowchart(req.agent, req.flowchart, req.args))


@app.post("/v1/agents/{name}/build-profile")
async def api_build_profile(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.build_user_profile(name))


@app.post("/v1/agents/{name}/build-music")
async def api_build_music(name: str, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.build_music_preferences(name))


@app.post("/v1/restart")
async def api_restart(req: ForceRequest | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.restart(force=(req.force if req else False)))


@app.post("/v1/restart-including-bridge")
async def api_restart_bridge(req: ForceRequest | None = None, _: None = Depends(require_bearer_token)) -> dict:
    return _result_json(await commands_api.restart_including_bridge(force=(req.force if req else False)))


@app.post("/v1/trigger")
async def trigger(req: TriggerRequest, _: None = Depends(require_bearer_token)):
    agent_name = req.session
    agent_cwd = req.cwd or os.path.join(config.AXI_USER_DATA, "agents", agent_name)

    try:
        if agent_name in agents.agents:
            log.info("HTTP trigger: routing to existing session '%s'", agent_name)
            await agents.send_prompt_to_agent(agent_name, req.prompt)
            return {"status": "ok", "action": "routed"}

        log.info("HTTP trigger: spawning new session '%s'", agent_name)
        await agents.reclaim_agent_name(agent_name)
        extra_mcp = config.load_mcp_servers(req.mcp_servers) if req.mcp_servers else None
        await agents.spawn_agent(
            agent_name,
            agent_cwd,
            req.prompt,
            extensions=req.extensions,
            extra_mcp_servers=extra_mcp,
        )
        return {"status": "ok", "action": "spawned"}
    except Exception:
        log.exception("HTTP trigger failed for session '%s'", agent_name)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to trigger session '{agent_name}'"},
        )
