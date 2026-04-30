"""Unit tests for axi.main._fire_schedules — reset_context handling.

Covers the bug where _fire_schedules ignored entry["reset_context"] when routing
to existing sessions, causing scheduled prompts to land in an agent that still
remembered its prior firing's responses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axi import agents as agents_mod
from axi import main as main_mod


@pytest.fixture
def stubbed_main(monkeypatch):
    """Replace _fire_schedules' collaborators with recording stubs."""
    calls: list[tuple[str, tuple, dict]] = []

    def record(label: str):
        async def _async(*args, **kwargs):
            calls.append((label, args, kwargs))

        return _async

    def record_sync(label: str):
        def _sync(*args, **kwargs):
            calls.append((label, args, kwargs))

        return _sync

    monkeypatch.setattr(agents_mod, "agents", {})
    monkeypatch.setattr(agents_mod, "schedule_last_fired", {})
    monkeypatch.setattr(agents_mod, "send_prompt_to_agent", record("send_prompt"))
    monkeypatch.setattr(agents_mod, "spawn_agent", record("spawn"))
    monkeypatch.setattr(agents_mod, "reclaim_agent_name", record("reclaim"))
    monkeypatch.setattr(agents_mod, "reset_session", record("reset"))

    async def _no_channel(_name):
        return None

    monkeypatch.setattr(agents_mod, "get_agent_channel", _no_channel)
    monkeypatch.setattr(main_mod, "audited_channel_send", record("channel_send"))
    monkeypatch.setattr(main_mod, "append_history", record_sync("append_history"))
    monkeypatch.setattr(main_mod, "check_skip", lambda _key: False)
    return calls


def labels(calls):
    return [label for label, _args, _kwargs in calls]


# ---------------------------------------------------------------------------
# Recurring fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurring_existing_session_with_reset_context(stubbed_main):
    """reset_context=true + existing session → reset_session called before send."""
    agents_mod.agents["daily-summary"] = object()
    # Last fired far in the past so cron "* * * * *" definitely advances.
    now_utc = datetime.now(UTC)
    agents_mod.schedule_last_fired["axi-master/daily"] = now_utc - timedelta(hours=24)

    entry = {
        "name": "daily",
        "owner": "axi-master",
        "session": "daily-summary",
        "schedule": "* * * * *",
        "prompt": "do the thing",
        "reset_context": True,
    }
    await main_mod._fire_schedules([entry], now_utc, now_utc)

    seq = labels(stubbed_main)
    assert "reset" in seq, seq
    assert "send_prompt" in seq, seq
    assert seq.index("reset") < seq.index("send_prompt")
    assert "spawn" not in seq
    reset_args = next(args for label, args, _ in stubbed_main if label == "reset")
    assert reset_args == ("daily-summary",)


@pytest.mark.asyncio
async def test_recurring_existing_session_without_reset_context(stubbed_main):
    """reset_context missing/false + existing session → no reset, only send."""
    agents_mod.agents["daily-summary"] = object()
    now_utc = datetime.now(UTC)
    agents_mod.schedule_last_fired["axi-master/daily"] = now_utc - timedelta(hours=24)

    entry = {
        "name": "daily",
        "owner": "axi-master",
        "session": "daily-summary",
        "schedule": "* * * * *",
        "prompt": "do the thing",
        # no reset_context field
    }
    await main_mod._fire_schedules([entry], now_utc, now_utc)

    seq = labels(stubbed_main)
    assert "reset" not in seq, seq
    assert "send_prompt" in seq, seq

    # Same expectation when reset_context is explicitly false.
    stubbed_main.clear()
    now_utc = datetime.now(UTC)
    agents_mod.schedule_last_fired["axi-master/daily"] = now_utc - timedelta(hours=24)
    entry["reset_context"] = False
    await main_mod._fire_schedules([entry], now_utc, now_utc)
    seq = labels(stubbed_main)
    assert "reset" not in seq, seq
    assert "send_prompt" in seq, seq


@pytest.mark.asyncio
async def test_recurring_no_existing_session_spawns(stubbed_main):
    """No existing session → spawn path; reset_session not called regardless of flag."""
    # agents dict empty — agent_name not in agents.agents
    now_utc = datetime.now(UTC)
    agents_mod.schedule_last_fired["axi-master/daily"] = now_utc - timedelta(hours=24)

    entry = {
        "name": "daily",
        "owner": "axi-master",
        "session": "daily-summary",
        "schedule": "* * * * *",
        "prompt": "do the thing",
        "reset_context": True,  # set, but session doesn't exist yet
    }
    await main_mod._fire_schedules([entry], now_utc, now_utc)

    seq = labels(stubbed_main)
    assert "reset" not in seq, seq
    assert "send_prompt" not in seq, seq
    assert "reclaim" in seq, seq
    assert "spawn" in seq, seq
    assert seq.index("reclaim") < seq.index("spawn")


# ---------------------------------------------------------------------------
# One-off fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_off_existing_session_with_reset_context(stubbed_main):
    """One-off reset_context=true + existing session → reset before send."""
    agents_mod.agents["daily-summary"] = object()
    now_utc = datetime.now(UTC)
    fire_at = (now_utc - timedelta(seconds=10)).isoformat()

    entry = {
        "name": "one-shot",
        "owner": "axi-master",
        "session": "daily-summary",
        "at": fire_at,
        "prompt": "now",
        "reset_context": True,
    }
    await main_mod._fire_schedules([entry], now_utc, now_utc)

    seq = labels(stubbed_main)
    assert "reset" in seq, seq
    assert "send_prompt" in seq, seq
    assert seq.index("reset") < seq.index("send_prompt")
    assert "spawn" not in seq


@pytest.mark.asyncio
async def test_one_off_existing_session_without_reset_context(stubbed_main):
    """One-off reset_context missing → no reset, only send."""
    agents_mod.agents["daily-summary"] = object()
    now_utc = datetime.now(UTC)
    fire_at = (now_utc - timedelta(seconds=10)).isoformat()

    entry = {
        "name": "one-shot",
        "owner": "axi-master",
        "session": "daily-summary",
        "at": fire_at,
        "prompt": "now",
    }
    await main_mod._fire_schedules([entry], now_utc, now_utc)

    seq = labels(stubbed_main)
    assert "reset" not in seq, seq
    assert "send_prompt" in seq, seq


@pytest.mark.asyncio
async def test_one_off_no_existing_session_spawns(stubbed_main):
    """One-off with no existing session → spawn path even with reset_context=true."""
    now_utc = datetime.now(UTC)
    fire_at = (now_utc - timedelta(seconds=10)).isoformat()

    entry = {
        "name": "one-shot",
        "owner": "axi-master",
        "session": "daily-summary",
        "at": fire_at,
        "prompt": "now",
        "reset_context": True,
    }
    await main_mod._fire_schedules([entry], now_utc, now_utc)

    seq = labels(stubbed_main)
    assert "reset" not in seq, seq
    assert "send_prompt" not in seq, seq
    assert "reclaim" in seq, seq
    assert "spawn" in seq, seq
