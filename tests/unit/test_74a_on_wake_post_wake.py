"""Phase 7.4a — post-wake logic moved from agents.wake_agent into DiscordFrontend.on_wake.

Verifies on_wake (which the hub broadcasts from _ensure_awake, closing the 7.2 gap):
  - posts the system prompt on first wake, once (first-wake guard)
  - detects a prompt change on resume and updates the stored hash
  - treats a resume that fell back to fresh (session_id cleared, last_failed_resume_id set)
    as a resume
  - skips posting when there is no channel, and is safe for an unknown agent
"""

from __future__ import annotations

import pytest

from axi import agents, prompts
from axi.axi_types import AgentSession, discord_state
from axi.discord_frontend import DiscordFrontend


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    agents.agents.clear()

    async def _noop_model_warning(_session: object) -> None:
        return None

    # The real model warning needs the live Discord bot; stub it out.
    monkeypatch.setattr(agents, "_post_model_warning", _noop_model_warning)


def _fe() -> DiscordFrontend:
    return DiscordFrontend(bot=object())


def _capture_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    posts: list[dict] = []

    async def fake_post(agent_name: str, _sp: object, **kw: object) -> None:
        posts.append({"agent": agent_name, **kw})

    monkeypatch.setattr(prompts, "post_system_prompt_to_channel", fake_post)
    return posts


@pytest.mark.asyncio
async def test_on_wake_posts_system_prompt_once_on_first_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _capture_posts(monkeypatch)
    session = AgentSession(
        name="w1", system_prompt={"type": "preset", "preset": "claude_code", "append": "hello"}
    )
    discord_state(session).channel_id = 555
    agents.agents["w1"] = session

    await _fe().on_wake("w1")

    assert len(posts) == 1
    assert posts[0]["agent"] == "w1"
    assert posts[0]["is_resume"] is False
    assert discord_state(session).system_prompt_posted is True

    # Second wake must NOT re-post (first-wake guard).
    await _fe().on_wake("w1")
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_on_wake_detects_prompt_change_on_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _capture_posts(monkeypatch)
    monkeypatch.setattr(prompts, "compute_prompt_hash", lambda _sp: "NEWHASH")

    session = AgentSession(
        name="w2", system_prompt={"type": "preset", "preset": "claude_code", "append": "new"}
    )
    session.session_id = "sess-abc"  # resume
    session.system_prompt_hash = "OLDHASH"
    discord_state(session).channel_id = 555
    agents.agents["w2"] = session

    await _fe().on_wake("w2")

    assert len(posts) == 1
    assert posts[0]["is_resume"] is True
    assert posts[0]["prompt_changed"] is True
    assert session.system_prompt_hash == "NEWHASH"  # hash updated in place


@pytest.mark.asyncio
async def test_on_wake_resume_fallback_still_treated_as_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _capture_posts(monkeypatch)
    monkeypatch.setattr(prompts, "compute_prompt_hash", lambda _sp: "H")

    session = AgentSession(name="w3", system_prompt={"append": "x"})
    session.session_id = None
    session.last_failed_resume_id = "failed-sess"  # resume attempted, fell back to fresh
    session.system_prompt_hash = "H"
    discord_state(session).channel_id = 555
    agents.agents["w3"] = session

    await _fe().on_wake("w3")

    assert len(posts) == 1
    assert posts[0]["is_resume"] is True


@pytest.mark.asyncio
async def test_on_wake_no_channel_skips_post(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _capture_posts(monkeypatch)
    session = AgentSession(name="w4", system_prompt={"append": "x"})
    agents.agents["w4"] = session  # no channel_id -> discord_state.channel_id is None

    await _fe().on_wake("w4")

    assert posts == []


@pytest.mark.asyncio
async def test_on_wake_unknown_agent_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _capture_posts(monkeypatch)
    await _fe().on_wake("does-not-exist")  # must not raise
    assert posts == []
