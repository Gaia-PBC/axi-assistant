"""Phase 7.5e — separate Axi's per-agent Logger from the hub's AgentLog.

Both used to collide on session.agent_log: Axi wrote a logging.Logger there (per-agent
<name>.log file) while the hub writes its AgentLog (structured event log). The 7.4d
workaround reset session.agent_log=None after spawn, killing the hub's AgentLog. 7.5e moves
Axi's Logger to frontend_state (discord_state(session).agent_log) and removes the reset, so
the hub's AgentLog survives on session.agent_log.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub import AgentSession
from axi.axi_types import discord_state, setup_agent_log


def test_setup_agent_log_writes_to_frontend_state_not_session() -> None:
    session = AgentSession(name="al-fe")
    # Simulate the hub already having placed its AgentLog on session.agent_log.
    hub_log_sentinel = object()
    session.agent_log = hub_log_sentinel

    setup_agent_log(session)

    # Axi's Logger lands on the Discord frontend_state...
    assert isinstance(discord_state(session).agent_log, logging.Logger)
    # ...and the hub's AgentLog on session.agent_log is left untouched (no collision).
    assert session.agent_log is hub_log_sentinel


def test_setup_agent_log_configures_per_agent_file_logger() -> None:
    session = AgentSession(name="al-file")

    setup_agent_log(session)

    logger = discord_state(session).agent_log
    assert isinstance(logger, logging.Logger)
    assert logger.name == "agent.al-file"
    assert logger.level == logging.DEBUG
    assert not logger.propagate
    assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)


def test_session_agent_log_defaults_untouched_by_setup() -> None:
    """A fresh session that never had a hub AgentLog keeps session.agent_log = None
    after Axi's logger setup (Axi no longer writes there)."""
    session = AgentSession(name="al-default")
    assert session.agent_log is None

    setup_agent_log(session)

    assert session.agent_log is None  # Axi wrote to frontend_state instead
    assert isinstance(discord_state(session).agent_log, logging.Logger)


def test_spawn_agent_does_not_reset_session_agent_log() -> None:
    """Regression guard for the removed 7.4d workaround: agents.spawn_agent must no longer
    contain a `session.agent_log = None` reset (which would kill the hub's AgentLog)."""
    import inspect

    from axi import agents

    src = inspect.getsource(agents.spawn_agent)
    assert "agent_log = None" not in src
