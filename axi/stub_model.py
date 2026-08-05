"""In-process deterministic stub model for the e2e B-tier test suite.

Active only when ``AXI_STUB_MODEL=1``.  It replaces the LLM/flowcoder client
(the thing that talks to a real model over the network) with a fast,
deterministic, in-process stub, while leaving every other part of the real bot
untouched: the hub queue, spawn/kill, lifecycle, permissions, channel
management and reactions all still run for real.  This lets the ~48 "B-tier"
plumbing tests run instantly and deterministically with no engine, no CLI, no
network and no token cost.

Design
------
The stub is a drop-in ``ClaudeSDKClient`` replacement.  Rather than
reimplementing the whole ``StreamOutput`` protocol, the stub yields *raw SDK
message dicts* (the same shape ``parse_message`` accepts) from
``client._query.receive_messages()``.  The real
:func:`agenthub.streaming.stream_response` then parses and normalises them
exactly as it does for a real model, so a single stub client works for both the
hub turn path and the legacy ``/clear``/``/compact`` path — no duplicated
streaming logic.

For each turn the stub inspects the (possibly ``/soul``-wrapped) content and:

* ``Say exactly: <X>``  -> echoes ``<X>``.
* ``Spawn an agent named ...`` -> calls the real ``axi_spawn_agent`` handler.
* ``Kill the agent named ...`` -> calls the real ``axi_kill_agent`` handler.
* ``Restart the agent named ...`` -> calls the real ``axi_kill_agent`` (respawn
  is driven by the follow-up spawn in the same test).
* ``Send a message to the agent ...`` -> calls the real ``axi_send_message``.
* anything else -> a plain ``ACK``.

Every turn also emits the ``awaiting input`` sentinel the e2e harness waits on
so ``send_and_wait`` returns immediately instead of blocking for the full
timeout.  A configurable ``AXI_STUB_DELAY_MS`` busy window (interruptible via
``interrupt()``) gives the busy -> queue / keep-latest / interrupt tests a real
window to race against.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING, Any, cast

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agenthub.types import AgentSession

log = logging.getLogger("axi.stub_model")

# The e2e harness (tests/helpers.py) treats this string as the end-of-turn
# marker.  The real /soul flowchart never emits it (it is a test-only sentinel),
# so the stub emits it explicitly at the end of every turn.
SENTINEL = "awaiting input"

# ``_wrap_content_with_flowchart`` sanitises the embedded user message by
# swapping quotes/backslashes for unicode look-alikes so they survive shell
# quoting.  Restoring the double quote lets the command regexes below match a
# wrapped message; the single quote is left as-is so a real shell-closing quote
# still terminates the echo capture.
_SANITISED_DQUOTE = chr(0x201C)  # left double quotation mark (was ")
_SANITISED_SQUOTE = chr(0x2019)  # right single quotation mark (was ')
_SANITISED_BSLASH = chr(0x2216)  # set minus (was backslash)

_SAY_EXACTLY = re.compile(r"Say exactly[:\s]+([^'\n]+)")
# Alternate echo phrasing (test_unicode_emoji) and content-shaping prompts.
_REPEAT = re.compile(r"Repeat (?:these characters )?exactly[:\s]+([^'\n]+)")
_CODE_BLOCK = re.compile(r"code block containing[:\s]+([^'\n]+)")
_NUMBERS = re.compile(r"numbers?\s+1\s+(?:through|to)\s+(\d+)", re.IGNORECASE)
_SPAWN = re.compile(
    r'Spawn an agent named "(?P<name>[^"]+)" with cwd "(?P<cwd>[^"]*)"'
    r' and prompt "(?P<prompt>[^"]*)"'
    r'(?: and resume="(?P<resume>[^"]*)")?'
    r'(?: and command="(?P<command>[^"]*)")?'
    r'(?: and command_args="(?P<command_args>[^"]*)")?'
    r'(?: and packs=(?P<packs>\[[^\]]*\]))?',
    re.DOTALL,
)
_KILL = re.compile(r'Kill the agent named "(?P<name>[^"]+)"')
_RESTART = re.compile(r'Restart the agent named "(?P<name>[^"]+)"')
_SEND_TO = re.compile(
    r'Send a message to the agent "(?P<name>[^"]+)" saying: "(?P<msg>.*)"',
    re.DOTALL,
)

# Intent-based spawn fallback for phrasings the strict pattern misses (e.g. the
# empty-name validation test: `Spawn an agent with an empty name (name="")`).
# Routing every spawn phrasing to the real handler lets ITS validation run.
_SPAWN_INTENT = re.compile(r"Spawn an agent", re.IGNORECASE)
_ARG_NAMED = re.compile(r'named "([^"]*)"')
_ARG_NAME_EQ = re.compile(r'name="([^"]*)"')
_ARG_CWD = re.compile(r'cwd "([^"]*)"')
_ARG_PROMPT = re.compile(r'prompt "([^"]*)"')
_ARG_RESUME = re.compile(r'resume="([^"]*)"')
_ARG_COMMAND = re.compile(r'(?<!command_args=") and command="([^"]*)"')
_ARG_COMMAND_ARGS = re.compile(r'command_args="([^"]*)"')
_ARG_PACKS = re.compile(r"packs=(\[[^\]]*\])")


def _spawn_args(content: str) -> dict[str, Any] | None:
    """Extract ``axi_spawn_agent`` args from a spawn instruction, or None.

    Prefers the strict ``named "X" with cwd "Y" and prompt "Z"`` shape; falls
    back to a loose field scan for alternate phrasings so the real handler's
    validation (empty name, reserved name, disallowed cwd) always runs.
    """
    m = _SPAWN.search(content)
    if m:
        args: dict[str, Any] = {
            "name": _unsanitise(m.group("name")),
            "cwd": _unsanitise(m.group("cwd")),
            "prompt": _unsanitise(m.group("prompt")),
        }
        for key in ("resume", "command", "command_args"):
            if m.group(key):
                args[key] = _unsanitise(m.group(key))
        if m.group("packs"):
            args["extensions"] = _parse_packs(m.group("packs"))
        return args

    if not _SPAWN_INTENT.search(content):
        return None

    name_m = _ARG_NAMED.search(content) or _ARG_NAME_EQ.search(content)
    cwd_m = _ARG_CWD.search(content)
    prompt_m = _ARG_PROMPT.search(content)
    args = {
        "name": _unsanitise(name_m.group(1)) if name_m else "",
        "cwd": _unsanitise(cwd_m.group(1)) if cwd_m else "",
        "prompt": _unsanitise(prompt_m.group(1)) if prompt_m else "",
    }
    resume_m = _ARG_RESUME.search(content)
    if resume_m:
        args["resume"] = _unsanitise(resume_m.group(1))
    cmd_m = _ARG_COMMAND.search(content)
    if cmd_m:
        args["command"] = _unsanitise(cmd_m.group(1))
    cmd_args_m = _ARG_COMMAND_ARGS.search(content)
    if cmd_args_m:
        args["command_args"] = _unsanitise(cmd_args_m.group(1))
    packs_m = _ARG_PACKS.search(content)
    if packs_m:
        args["extensions"] = _parse_packs(packs_m.group(1))
    return args


def is_enabled() -> bool:
    """Whether the deterministic stub model is active for this process."""
    return os.environ.get("AXI_STUB_MODEL") == "1"


def suppress_completion_ping() -> bool:
    """In stub mode, skip the bare end-of-turn ``@user`` completion ping.

    The ping is a user notification with no bearing on the deterministic test
    suite.  Posting it on every fast stub turn trips Discord's per-channel
    message rate limit; discord.py then blocks the send ~5s waiting for the
    bucket to refill (pre-emptively, so no 429 is logged), and because the ping
    is sent inside the turn's ``query_lock`` the agent looks "busy" to the next
    rapid test message.  Suppressing it keeps turns from bleeding into each
    other without changing any behaviour a test asserts on.
    """
    return is_enabled()


# A turn only gets a busy window if its prompt asks the model to take its time.
# The queue/keep-latest/interrupt tests use these exact phrasings to create a
# real busy window; every other prompt stays instant so fast FIFO tests
# (test_queue_stress) and busy-then-command tests (test_clear_while_busy, which
# sends a bare "explanation" prompt and needs /clear to run, not queue) aren't
# slowed or forced to drop messages.  "essay" covers the status/inter-agent
# busy tests; "explanation" is deliberately excluded.
_BUSY_SIGNAL = re.compile(r"Think for a while|Do not answer immediately|essay", re.IGNORECASE)
_DEFAULT_BUSY_DELAY_MS = 8000.0


def _busy_delay_seconds() -> float:
    """Busy-window length in seconds; ``AXI_STUB_DELAY_MS`` overrides the default."""
    raw = os.environ.get("AXI_STUB_DELAY_MS")
    try:
        ms = float(raw) if raw else _DEFAULT_BUSY_DELAY_MS
    except ValueError:
        ms = _DEFAULT_BUSY_DELAY_MS
    return max(0.0, ms / 1000.0)


def _restore_double_quotes(text: str) -> str:
    """Undo the double-quote sanitisation so command regexes can match."""
    return text.replace(_SANITISED_DQUOTE, '"')


def _unsanitise(text: str) -> str:
    """Fully restore a captured fragment to the user's original characters."""
    return (
        text.replace(_SANITISED_DQUOTE, '"')
        .replace(_SANITISED_SQUOTE, "'")
        .replace(_SANITISED_BSLASH, "\\")
    )


def _content_to_text(content: Any) -> str:
    """Flatten hub message content (str or image/text blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(cast("dict[str, Any]", block).get("text", ""))
            for block in cast("list[Any]", content)
            if isinstance(block, dict) and cast("dict[str, Any]", block).get("type") == "text"
        ]
        return " ".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Raw SDK message builders (shapes accepted by parse_message)
# ---------------------------------------------------------------------------


def _assistant_msg(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "stub-model",
            "content": [{"type": "text", "text": text}],
        },
        "error": None,
    }


def _result_msg(session_id: str) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 0,
        "duration_api_ms": 0,
        "is_error": False,
        "num_turns": 1,
        "session_id": session_id,
        "total_cost_usd": 0.0,
        "usage": {},
        "result": "",
    }


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------


class _NullReceive:
    """Stand-in for the SDK's internal receive stream.

    ``agents.drain_sdk_buffer`` calls ``receive_nowait`` on this; the stub never
    buffers stale messages, so it always signals "nothing to drain".
    """

    def receive_nowait(self) -> Any:
        raise anyio.WouldBlock


class _StubQuery:
    """Mimics ``ClaudeSDKClient._query`` for the streaming engine."""

    def __init__(self, client: StubClient) -> None:
        self._client = client
        # discord_stream.drain_sdk_buffer reads this attribute directly.
        self._message_receive = _NullReceive()

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        async for msg in self._client.stream_messages():
            yield msg


class StubClient:
    """Deterministic in-process replacement for ``ClaudeSDKClient``.

    Only the surface the hub and the legacy stream path actually touch is
    implemented: ``__aenter__``/``__aexit__``, ``query``, ``interrupt`` and a
    ``_query`` exposing ``receive_messages``.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session
        self._content: str = ""
        self._interrupted = asyncio.Event()
        self._query = _StubQuery(self)
        self._session_id = f"stub-{session.name}"

    async def __aenter__(self) -> StubClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, content: Any, session_id: str = "default") -> None:
        """Capture the turn's content; the response is produced on consume."""
        self._content = _content_to_text(content)
        # A fresh event per turn so a stale interrupt never leaks forward.
        self._interrupted = asyncio.Event()

    async def interrupt(self) -> None:
        """Cut a delayed (busy) turn short."""
        self._interrupted.set()

    # -- streaming ---------------------------------------------------------

    async def _sleep_busy(self) -> None:
        """Hold a busy window open for take-your-time prompts; interruptible.

        Only prompts matching :data:`_BUSY_SIGNAL` block, so the queue /
        keep-latest / interrupt tests get a real window while every other turn
        stays instant.  ``interrupt()`` (and thus /skip and busy inter-agent
        delivery) cuts the window short.
        """
        if not _BUSY_SIGNAL.search(self._content):
            return
        delay = _busy_delay_seconds()
        if delay <= 0:
            return
        try:
            await asyncio.wait_for(self._interrupted.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def stream_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield the raw SDK message dicts for the current turn."""
        log.debug("stub-stream[%s]: begin", self._session.name)
        await self._sleep_busy()
        if self._interrupted.is_set():
            # Interrupted mid-turn: end cleanly with no echo so keep-latest /
            # interrupt tests do not see the superseded message's output.
            log.debug("stub-stream[%s]: interrupted, ending", self._session.name)
            yield _result_msg(self._session_id)
            return
        texts = await self._respond(self._content)
        log.debug("stub-stream[%s]: responding with %d msg(s)", self._session.name, len(texts))
        for text in texts:
            yield _assistant_msg(text)
        yield _assistant_msg(SENTINEL)
        yield _result_msg(self._session_id)
        log.debug("stub-stream[%s]: end", self._session.name)

    # -- intent handling ---------------------------------------------------

    async def _respond(self, raw_content: str) -> list[str]:
        """Map the (possibly wrapped) content to the response text(s)."""
        content = _restore_double_quotes(raw_content)

        spawn_args = _spawn_args(content)
        if spawn_args is not None:
            return [await self._call_tool("axi_spawn_agent", spawn_args)]
        m = _KILL.search(content)
        if m:
            return [await self._do_kill(_unsanitise(m.group("name")))]
        m = _RESTART.search(content)
        if m:
            return [await self._do_kill(_unsanitise(m.group("name")))]
        m = _SEND_TO.search(content)
        if m:
            return [
                await self._do_send(
                    _unsanitise(m.group("name")), _unsanitise(m.group("msg"))
                )
            ]
        m = _CODE_BLOCK.search(content)
        if m:
            code = _unsanitise(m.group(1)).strip()
            return [f"```python\n{code}\n```"]
        m = _NUMBERS.search(content)
        if m:
            return _number_lines(int(m.group(1)))
        m = _SAY_EXACTLY.search(content) or _REPEAT.search(content)
        if m:
            return [_unsanitise(m.group(1)).strip()]
        return ["ACK"]

    async def _do_kill(self, name: str) -> str:
        return await self._call_tool("axi_kill_agent", {"name": name})

    async def _do_send(self, name: str, message: str) -> str:
        return await self._call_tool(
            "axi_send_message",
            {"agent_name": name, "content": message, "sender": self._session.name},
        )

    async def _call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Invoke a real MCP tool handler and return its text payload."""
        from axi import tools

        tool_def = getattr(tools, tool_name)
        try:
            result = await tool_def.handler(args)
        except Exception:
            log.exception("stub: %s handler raised for %s", tool_name, args.get("name"))
            return f"Error running {tool_name}."
        return _result_text(result)


def _number_lines(n: int) -> list[str]:
    """Numbers ``1..n`` as ~100-per-message chunks.

    Returning several strings makes the real stream engine render several
    Discord messages, so test_long_output_splitting's multi-message / per-message
    length assertions are satisfied deterministically (300 numbers on their own
    lines is only ~1.1k chars — a single message otherwise).
    """
    n = max(1, min(n, 1000))
    chunk = 100
    return [
        "\n".join(str(i) for i in range(start, min(start + chunk, n + 1)))
        for start in range(1, n + 1, chunk)
    ]


def _parse_packs(raw: str) -> list[str]:
    """Parse a ``['a', 'b']``-style packs literal into a list of names."""
    inner = raw.strip().lstrip("[").rstrip("]")
    return re.findall(r"""['"]([^'"]+)['"]""", inner)


def _result_text(result: Any) -> str:
    """Extract the concatenated text payload from an MCP tool result."""
    if not isinstance(result, dict):
        return str(result)
    blocks = cast("dict[str, Any]", result).get("content", [])
    parts = [
        str(cast("dict[str, Any]", block).get("text", ""))
        for block in cast("list[Any]", blocks)
        if isinstance(block, dict) and cast("dict[str, Any]", block).get("type") == "text"
    ]
    return "\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Hub factory hooks (swapped in by hub_wiring when AXI_STUB_MODEL=1)
# ---------------------------------------------------------------------------


async def create_client(session: AgentSession, options: Any) -> Any:
    """Create the deterministic stub client (drop-in for ``_create_client``)."""
    log.info("stub-model: creating stub client for '%s'", session.name)
    client = StubClient(session)
    await client.__aenter__()
    return client


async def disconnect_client(client: Any, name: str) -> None:
    """Tear down a stub client (drop-in for ``_disconnect_client``)."""
    log.info("stub-model: disconnecting stub client for '%s'", name)
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        log.debug("stub-model: disconnect for '%s' raised", name, exc_info=True)
