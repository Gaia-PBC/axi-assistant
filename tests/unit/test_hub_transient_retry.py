"""Regression tests for R1 — transient API errors never retried after the hub refactor.

The pre-hub path (discord_stream.py:1598-1663) retried up to
API_ERROR_MAX_RETRIES with exponential backoff, posting
"⚠️ API error, retrying in Ns... (attempt N/3)" each round and
"❌ API error persisted after 3 retries" on exhaustion, and bailed out if the
client went away mid-retry (:1631-1645).

After the refactor, runtime.py set TurnOutcome.RETRY_EXHAUSTED on the *first*
TransientError with no retry loop at all, and nothing was posted — so the turn
died silently. The enum name encoded an intent the code never implemented.

The retry lives inside the turn rather than re-submitting one, so after_turn
fires exactly once with the final outcome.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from agenthub.runtime import AgentHub
from agenthub.types import TurnOutcome


class _Client:
    """Client whose stream yields TransientError for the first N queries."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.queries: list[Any] = []

    async def query(self, content: Any) -> None:
        self.queries.append(content)


class _State:
    def __init__(self) -> None:
        self.stop_requested = False


class _Session:
    def __init__(self, client: _Client) -> None:
        self.name = "agent"
        self.client = client
        self.state = _State()


def _make_hub(*, max_attempts: int, fail_times: int) -> tuple[AgentHub, list[tuple[str, str]]]:
    """Hub whose stream consumption yields RETRY_EXHAUSTED `fail_times` times.

    ``_consume_stream`` is stubbed rather than driven through a fake session:
    what is under test here is the retry *policy* in ``_query_with_retry``.
    Stream consumption itself is covered by the agenthub stream tests.
    """
    posted: list[tuple[str, str]] = []
    calls = {"n": 0}

    hub = AgentHub(
        create_client=lambda *a, **k: None,
        disconnect_client=lambda *a, **k: None,
        make_agent_options=lambda *a, **k: None,
        max_attempts=max_attempts,
        retry_base_delay=0.0,  # keep tests fast; backoff maths covered separately
    )

    async def _consume(session: Any, turn_id: str) -> TurnOutcome:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            return TurnOutcome.RETRY_EXHAUSTED
        return TurnOutcome.COMPLETED

    async def _broadcast(method: str, *args: Any) -> None:
        if method == "post_system":
            posted.append((args[0], args[1]))

    hub._consume_stream = _consume  # type: ignore[assignment]
    hub._frontend_broadcast = _broadcast  # type: ignore[assignment]
    return hub, posted


class _Turn:
    turn_id = "t1"
    content = "hello"
    metadata: ClassVar[dict[str, Any]] = {}


@pytest.mark.asyncio
async def test_transient_error_is_retried_and_can_succeed() -> None:
    hub, posted = _make_hub(max_attempts=3, fail_times=1)
    session = _Session(_Client(fail_times=1))

    outcome = await hub._query_with_retry(session, _Turn(), "hello")

    assert outcome is TurnOutcome.COMPLETED
    # original query + one resume query
    assert len(session.client.queries) == 2
    assert session.client.queries[1] == hub.retry_resume_prompt
    assert any("retrying in" in text for _, text in posted)
    assert not any("persisted" in text for _, text in posted)


@pytest.mark.asyncio
async def test_retries_are_bounded_and_report_exhaustion() -> None:
    hub, posted = _make_hub(max_attempts=3, fail_times=99)
    session = _Session(_Client(fail_times=99))

    outcome = await hub._query_with_retry(session, _Turn(), "hello")

    assert outcome is TurnOutcome.RETRY_EXHAUSTED
    assert len(session.client.queries) == 3, "must stop at max_attempts"
    retry_notices = [t for _, t in posted if "retrying in" in t]
    assert len(retry_notices) == 2, retry_notices
    assert any("attempt 2/3" in t for t in retry_notices)
    assert any("attempt 3/3" in t for t in retry_notices)
    assert any("persisted after 3 attempts" in t for _, t in posted)


@pytest.mark.asyncio
async def test_single_attempt_still_reports_the_error() -> None:
    """The core regression: with no retries configured the turn must not die silently."""
    hub, posted = _make_hub(max_attempts=1, fail_times=99)
    session = _Session(_Client(fail_times=99))

    outcome = await hub._query_with_retry(session, _Turn(), "hello")

    assert outcome is TurnOutcome.RETRY_EXHAUSTED
    assert len(session.client.queries) == 1
    assert posted, "a transient error must never be silent"
    assert any("API error" in t for _, t in posted)
    assert not any("persisted after" in t for _, t in posted)


@pytest.mark.asyncio
async def test_success_first_time_posts_nothing_and_queries_once() -> None:
    hub, posted = _make_hub(max_attempts=3, fail_times=0)
    session = _Session(_Client(fail_times=0))

    outcome = await hub._query_with_retry(session, _Turn(), "hello")

    assert outcome is TurnOutcome.COMPLETED
    assert len(session.client.queries) == 1
    assert posted == []


@pytest.mark.asyncio
async def test_stop_during_backoff_aborts_the_retry() -> None:
    hub, _posted = _make_hub(max_attempts=3, fail_times=99)
    hub.retry_base_delay = 5.0  # long enough that the stop must cut it short
    session = _Session(_Client(fail_times=99))

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        session.state.stop_requested = True

    task = asyncio.create_task(_stop_soon())
    outcome = await asyncio.wait_for(
        hub._query_with_retry(session, _Turn(), "hello"), timeout=5
    )
    await task

    assert outcome is TurnOutcome.INTERRUPTED
    assert len(session.client.queries) == 1, "must not re-query after a stop"


@pytest.mark.asyncio
async def test_client_killed_mid_retry_bails_out() -> None:
    hub, posted = _make_hub(max_attempts=3, fail_times=99)
    session = _Session(_Client(fail_times=99))

    original = hub._sleep_unless_stopped

    async def _kill_during_backoff(sess: Any, delay: float) -> bool:
        result = await original(sess, delay)
        sess.client = None
        return result

    hub._sleep_unless_stopped = _kill_during_backoff  # type: ignore[assignment]

    outcome = await hub._query_with_retry(session, _Turn(), "hello")

    assert outcome is TurnOutcome.RETRY_EXHAUSTED
    assert any("killed mid-retry" in t for _, t in posted)


@pytest.mark.asyncio
async def test_backoff_doubles_each_attempt() -> None:
    hub, posted = _make_hub(max_attempts=4, fail_times=99)
    hub.retry_base_delay = 5.0
    session = _Session(_Client(fail_times=99))

    slept: list[float] = []

    async def _record(sess: Any, delay: float) -> bool:
        slept.append(delay)
        return True

    hub._sleep_unless_stopped = _record  # type: ignore[assignment]
    await hub._query_with_retry(session, _Turn(), "hello")

    # Mirrors the old formula: base * 2**(attempt-2) -> 5, 10, 20
    assert slept == [5.0, 10.0, 20.0]


@pytest.mark.asyncio
async def test_stop_before_first_retry_short_circuits() -> None:
    hub, posted = _make_hub(max_attempts=3, fail_times=99)
    session = _Session(_Client(fail_times=99))
    session.state.stop_requested = True

    outcome = await hub._query_with_retry(session, _Turn(), "hello")

    assert outcome is TurnOutcome.INTERRUPTED
    assert posted == [], "a stopped turn should not announce a retry"


@pytest.mark.asyncio
async def test_max_attempts_floor_is_one() -> None:
    hub, _ = _make_hub(max_attempts=0, fail_times=0)
    assert hub.max_attempts == 1


@pytest.mark.asyncio
async def test_after_turn_fires_once_despite_retries() -> None:
    """The whole reason the retry lives inside the turn.

    Re-submitting a turn per attempt would run AxiTurnHooks.after_turn — and so
    its compaction pass — on every failed attempt, and would queue the retry
    behind any user message that arrived meanwhile.
    """
    from agenthub.turn_hooks import TurnHooks

    hub, _posted = _make_hub(max_attempts=3, fail_times=2)
    session = _Session(_Client(fail_times=2))

    events: list[str] = []

    class _Hooks(TurnHooks):
        async def before_turn(self, s: Any, t: Any) -> None:
            events.append("before")

        async def after_turn(self, s: Any, t: Any, outcome: Any) -> None:
            events.append(f"after:{outcome}")

    hub.turn_hooks = _Hooks()

    outcome = await hub._run_turn_with_timeout(session, _Turn())

    assert outcome is TurnOutcome.COMPLETED
    assert events == ["before", f"after:{TurnOutcome.COMPLETED}"], events
    assert len(session.client.queries) == 3, "two resume queries inside one turn"
