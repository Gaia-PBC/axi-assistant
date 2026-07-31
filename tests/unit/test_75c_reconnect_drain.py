"""Phase 7.5c — reconnect draining routed through the hub.

_drain_reconnect_queue replaces the legacy process_message_queue on the reconnect path:
messages buffered in session.message_queue during the reconnect window are flushed into the
hub via submit_user_message (FIFO), the queued 📨 reaction is cleared, and the frontend
routing id is carried in metadata. The mid-task in-flight stream drain stays a dedicated
transport-level consumer (_drain_inflight_stream) — verified here only by name/rename so the
7.5f deletion knows to keep it.
"""

from __future__ import annotations

import os
import types
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

import pytest

from axi.axi_types import AgentSession, discord_state


class _FakeHub:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, Any, dict | None]] = []

    async def submit_user_message(self, name: str, content: Any, metadata: dict | None = None) -> None:
        self.submitted.append((name, content, metadata))


class _FakeRouter:
    def __init__(self) -> None:
        self.removed: list[tuple[str, Any, str]] = []

    async def remove_reaction(self, name: str, message: Any, emoji: str) -> None:
        self.removed.append((name, message, emoji))


def _session(name: str = "rc", channel_id: int | None = 999) -> AgentSession:
    s = AgentSession(name=name)
    if channel_id is not None:
        discord_state(s).channel_id = channel_id
    return s


@pytest.mark.asyncio
async def test_drain_submits_all_via_hub_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    router = _FakeRouter()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: router)

    session = _session()
    ch = types.SimpleNamespace(id=555)
    # main.py appends 4-tuples: (content, channel, orig_message, raw_content)
    session.message_queue.append(("first", ch, "msg1", "first"))
    session.message_queue.append(("second", ch, "msg2", "second"))
    session.message_queue.append(("third", ch, "msg3", "third"))

    await agents._drain_reconnect_queue(session)

    # FIFO order preserved, content forwarded, channel id carried in metadata
    assert [c for _, c, _ in hub.submitted] == ["first", "second", "third"]
    assert all(m == {"channel_id": 555} for _, _, m in hub.submitted)
    # queued 📨 reaction cleared for each original message
    assert [msg for _, msg, _ in router.removed] == ["msg1", "msg2", "msg3"]
    assert all(emoji == "\U0001f4e8" for _, _, emoji in router.removed)
    # queue fully drained
    assert len(session.message_queue) == 0


@pytest.mark.asyncio
async def test_drain_empty_queue_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: _FakeRouter())

    await agents._drain_reconnect_queue(_session())

    assert hub.submitted == []


@pytest.mark.asyncio
async def test_drain_without_hub_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    monkeypatch.setattr("axi.agents.hub", None)
    monkeypatch.setattr("axi.agents._get_router", lambda: _FakeRouter())

    session = _session()
    session.message_queue.append(("x", types.SimpleNamespace(id=1), "m", "x"))

    await agents._drain_reconnect_queue(session)  # must not raise

    # With no hub, the buffer is left intact (safety-net recovery handles it later).
    assert len(session.message_queue) == 1


@pytest.mark.asyncio
async def test_drain_handles_3tuple_and_none_orig_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from axi import agents

    hub = _FakeHub()
    router = _FakeRouter()
    monkeypatch.setattr("axi.agents.hub", hub)
    monkeypatch.setattr("axi.agents._get_router", lambda: router)

    session = _session(channel_id=42)
    # 3-tuple entry (content, channel, orig_message) with channel=None, orig=None
    session.message_queue.append(("only", None, None))

    await agents._drain_reconnect_queue(session)

    assert len(hub.submitted) == 1
    name, content, metadata = hub.submitted[0]
    assert content == "only"
    # channel had no .id -> falls back to the agent's discord_state channel_id
    assert metadata == {"channel_id": 42}
    # orig_message is None -> no reaction removal attempted
    assert router.removed == []


def test_drain_inflight_stream_kept_and_renamed() -> None:
    """The mid-task in-flight drainer survived the rename (dedicated transport consumer,
    kept past 7.5f); the old _stream_via_router name is gone."""
    from axi import agents

    assert hasattr(agents, "_drain_inflight_stream")
    assert not hasattr(agents, "_stream_via_router")
