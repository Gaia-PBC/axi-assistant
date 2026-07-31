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
        await commands_api.spawn(req.name, req.prompt, cwd=req.cwd, resume=req.resume, model=req.model)
    )


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
