# Flowcoder Spawn-Threads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream each flowcoder spawn-block child's output and lifecycle events into its own Discord thread (recursive ancestry naming, auto-archive after a grace delay), keeping the parent channel readable.

**Architecture:** flowcoder-engine gains session-tagged lifecycle events (`spawn_start`/`spawn_complete`, `session` on block/flowchart events). Axi's transport-parse seam attaches the session context to every parsed SDK message (`receive_response_safe`), agenthub tracks per-session streaming state and tags every `StreamOutput` event, and `DiscordStreamRenderer` routes by `event.session` to per-child threads created/archived from `discord_state`.

**Tech Stack:** Python 3.12, asyncio, discord.py 2.x (`TextChannel.create_thread`, `Thread.archive`), claude-agent-sdk message parser, agenthub streaming engine, flowcoder-engine/flowcoder-flowchart (git-pinned, external PR).

**Spec:** `docs/superpowers/specs/2026-08-16-flowcoder-spawn-threads-design.md`

## Global Constraints

- `FC_SPAWN_THREADS` env var: default enabled (`"1"`); `"0"`/`"false"` disables threads — all child output stays in the parent channel exactly as today.
- `FC_THREAD_GRACE_SECS` env var: archive grace delay after `spawn_complete`, default `"300"` (seconds).
- Thread `auto_archive_duration` is in **minutes**; use `60` (the only valid backstop value).
- Thread names: top-level = child agent name; nested = `/`-joined ancestry chain (`<parent-thread-name>/<child-agent-name>`).
- Child text rendering is flush-based (deferred send into the thread), never live-edit.
- Fallback contract: any child event whose thread is unavailable renders `[agent_name]`-prefixed into the parent channel. Never drop output, never raise.
- Engine changes ship as a PR to `Gaia-PBC/flowcoder-core`; `pyproject.toml` git rev pins (`flowcoder-engine`, `flowcoder-flowchart`) bump to the merge commit.
- Backward/forward wire compat: older engine (no `session`/`spawn_*`) → today's behavior; unknown `spawn_*` subtype → `SystemNotification` fallback.

## Deviation from spec (approved design, mechanical refinement)

The spec's Section 2.4 proposed a uuid→session registry in `flowcoder_transport.py` because it assumed `StreamEvent`/`AssistantMessage` SDK parsing drops the transport's `_session_context` stamp. Verified: `receive_response_safe` (`packages/agenthub/agenthub/streaming.py:71`) has BOTH the raw dict and the parsed object in scope, and SDK message dataclasses (`claude_agent_sdk/types.py`) are non-slotted — so the context attaches directly to the parsed object at the single parse choke point. The registry is **dropped**; `_msg_session(msg)` reads `getattr(msg, "_session_context", {})` plus the engine's `data.session` fallback for raw system events. Same observable contract (every child event carries its session), strictly less state, no eviction logic, works for the stub model. Task 3 implements this; the DI parameter `session_context_fn` from the spec is also dropped (nothing needs injecting — the context rides on the message).

---

### Task 1: agenthub stream types — SpawnStart/SpawnEnd + session fields

**Files:**
- Modify: `packages/agenthub/agenthub/stream_types.py`
- Test: `tests/unit/test_stream_types.py`

**Interfaces:**
- Consumes: nothing (pure dataclasses).
- Produces: `SpawnStart(agent_name, command_name="", model="", backend="", parent_session="", session="")`, `SpawnEnd(agent_name, status="", duration_ms=0, cost_usd=0.0, session="")`; `session: str = ""` field on `TextDelta`, `TextFlush`, `ThinkingStart`, `ThinkingEnd`, `ToolUseStart`, `ToolUseEnd`, `BlockStart`, `BlockComplete`, `FlowchartStart`, `FlowchartEnd`, `QueryResult`. `session == ""` means parent. `SpawnStart`/`SpawnEnd` join the `StreamOutput` union.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_stream_types.py`:

```python
def test_spawn_start_defaults_and_fields() -> None:
    ev = SpawnStart(agent_name="lint", command_name="lint-fix", model="opus",
                    backend="claude", parent_session="main")
    assert ev.agent_name == "lint"
    assert ev.parent_session == "main"
    assert ev.session == ""  # parent-emitted by default
    assert SpawnStart(agent_name="x").command_name == ""


def test_spawn_end_defaults_and_fields() -> None:
    ev = SpawnEnd(agent_name="lint", status="completed", duration_ms=1234,
                  cost_usd=0.042, session="lint")
    assert ev.duration_ms == 1234
    assert ev.cost_usd == 0.042
    assert SpawnEnd(agent_name="x").status == ""


def test_routed_events_have_session_defaulting_to_parent() -> None:
    assert TextDelta(text="hi").session == ""
    assert TextFlush(text="hi").session == ""
    assert BlockStart(block_name="b").session == ""
    assert BlockStart(block_name="b", session="lint").session == "lint"
    assert QueryResult().session == ""
    assert ToolUseStart().session == ""
    assert ThinkingStart().session == ""


def test_spawn_events_are_stream_output() -> None:
    members = set(StreamOutput.__args__)
    assert SpawnStart in members
    assert SpawnEnd in members
```

(`StreamOutput` is already imported in `test_stream_types.py`; `__args__` is the union's member tuple.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_stream_types.py -v`
Expected: FAIL — `SpawnStart`/`SpawnEnd` undefined; `session` attribute errors.

- [ ] **Step 3: Implement**

In `stream_types.py`:

```python
@dataclass(slots=True)
class SpawnStart:
    """A flowcoder spawn block created a child agent session."""

    agent_name: str
    command_name: str = ""
    model: str = ""
    backend: str = ""
    parent_session: str = ""  # "main" or the spawning agent's name
    session: str = ""         # emitting (parent) session


@dataclass(slots=True)
class SpawnEnd:
    """A spawned agent session finished (completed/failed/cancelled)."""

    agent_name: str
    status: str = ""
    duration_ms: int = 0
    cost_usd: float = 0.0
    session: str = ""         # emitting (parent) session
```

Add `session: str = ""` as the LAST field of each listed dataclass. Append both to the `StreamOutput` union.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_stream_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/agenthub/agenthub/stream_types.py tests/unit/test_stream_types.py
git commit -m "feat(agenthub): spawn start/end stream types + session tagging fields"
```

---

### Task 2: Attach session context at the parse seam

**Files:**
- Modify: `packages/agenthub/agenthub/streaming.py:71-107` (`receive_response_safe`)
- Test: `tests/unit/test_streaming_engine.py`

**Interfaces:**
- Consumes: transport stamp `data["_session_context"] = {session, block_id, block_name}` (already produced by `axi/flowcoder_transport.py:66-72`).
- Produces: every parsed SDK message object carries attribute `_session_context: dict` (empty dict when absent). Consumed by Task 3's `_msg_session`.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_streaming_engine.py`:

```python
class _RawReceive:
    """Yields raw dicts like the transport's read_messages."""
    def __init__(self, items):
        self._items = iter(items)
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


class _FakeQuery:
    def __init__(self, items):
        self._message_receive = _RawReceive(items)
    def receive_messages(self):
        return self._message_receive


def _raw_stream_event(uuid="u1", session="lint"):
    return {
        "type": "stream_event",
        "uuid": uuid,
        "session_id": "child-session-1",
        "event": {"type": "message_start"},
        "_session_context": {"session": session, "block_id": "b1", "block_name": "B"},
    }


def _raw_result():
    return {
        "type": "result",
        "subtype": "success",
        "uuid": "u2",
        "session_id": "flowchart",
        "duration_ms": 1,
        "duration_api_ms": 0,
        "is_error": False,
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": "done",
    }


@pytest.mark.asyncio
async def test_receive_response_safe_attaches_session_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_stream_event(), _raw_result()])))
    got = []
    async for parsed in streaming_mod.receive_response_safe(session):
        got.append(parsed)
    # The stream_event carried the stamp; the result message did not (its raw
    # dict has no _session_context key), so it defaults to {}.
    assert getattr(got[0], "_session_context", None) == {
        "session": "lint", "block_id": "b1", "block_name": "B",
    }
    assert getattr(got[1], "_session_context", None) == {}


@pytest.mark.asyncio
async def test_receive_response_safe_defaults_context_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event()
    raw.pop("_session_context")
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery([raw, _raw_result()])))
    async for parsed in streaming_mod.receive_response_safe(session):
        assert getattr(parsed, "_session_context", {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_streaming_engine.py -k session_context -v`
Expected: FAIL — `_session_context` attribute missing (dataclass has no such field).

- [ ] **Step 3: Implement**

In `receive_response_safe`, after `parsed = parse_message(data)`:

```python
        try:
            parsed = parse_message(data)
        except MessageParseError:
            ...
            continue
        # Carry the flowcoder transport's session stamp onto the parsed
        # object. StreamEvent/AssistantMessage/ResultMessage dataclasses are
        # non-slotted, so this attaches cleanly; SystemMessage keeps the raw
        # dict anyway. Absent for non-flowcoder transports and the stub model.
        parsed._session_context = data.get("_session_context", {})
        yield parsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_streaming_engine.py -k session_context -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/agenthub/agenthub/streaming.py tests/unit/test_streaming_engine.py
git commit -m "feat(agenthub): attach session context to parsed SDK messages"
```

---

### Task 3: agenthub streaming — per-session state, tagging, spawn events

**Files:**
- Modify: `packages/agenthub/agenthub/streaming.py`
- Test: `tests/unit/test_streaming_engine.py`

**Interfaces:**
- Consumes: Task 1's `SpawnStart`/`SpawnEnd`/`session` fields; Task 2's `parsed._session_context`; engine's `data.session` on raw system messages (Task 8).
- Produces: `stream_response` yields session-tagged events; child sessions never mutate parent `AgentSession` state (`activity`, `session_id`, `compacting`, ctx buffers); `SpawnEnd` for session X flushes X's remaining text as a `TextFlush(reason="spawn_complete", session=X)` before discarding X's ctx.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_streaming_engine.py`:

```python
def _ctx_for(msg: Any) -> str:
    """Mirror of the implementation's _msg_session."""
    c = getattr(msg, "_session_context", None)
    if c and c.get("session"):
        return c.get("session", "")
    data = getattr(msg, "data", None) or {}
    return str(data.get("data", {}).get("session", ""))


def _raw_block_start(session: str) -> dict:
    return {
        "type": "system",
        "subtype": "block_start",
        "data": {"block_id": "b2", "block_name": "Prompt", "block_type": "prompt",
                 "session": session},
    }


def _raw_spawn_start() -> dict:
    return {
        "type": "system",
        "subtype": "spawn_start",
        "data": {"agent_name": "lint", "command_name": "lint-fix",
                 "model": "opus", "backend": "claude",
                 "cwd": "/tmp", "parent_session": "main"},
    }


def _raw_spawn_complete(status: str = "completed") -> dict:
    return {
        "type": "system",
        "subtype": "spawn_complete",
        "data": {"agent_name": "lint", "status": status,
                 "duration_ms": 1234, "cost_usd": 0.042, "result": "{}"},
    }


@pytest.mark.asyncio
async def test_spawn_events_are_yielded(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_spawn_start(), _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    spawns = [e for e in events if isinstance(e, (SpawnStart, SpawnEnd))]
    assert len(spawns) == 2
    assert isinstance(spawns[0], SpawnStart) and spawns[0].agent_name == "lint"
    assert spawns[0].parent_session == "main"
    assert isinstance(spawns[1], SpawnEnd) and spawns[1].status == "completed"
    assert spawns[1].duration_ms == 1234


@pytest.mark.asyncio
async def test_child_block_start_tagged_and_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_block_start("lint"), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    blocks = [e for e in events if isinstance(e, BlockStart)]
    assert len(blocks) == 1 and blocks[0].session == "lint"


@pytest.mark.asyncio
async def test_child_text_flushed_on_spawn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")  # text delta, see Task 2 helper
    raw["event"] = {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "child says hi"}}
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [raw, _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    flushes = [e for e in events if isinstance(e, TextFlush) and e.session == "lint"]
    assert flushes and flushes[-1].reason == "spawn_complete"
    assert "child says hi" in flushes[-1].text


@pytest.mark.asyncio
async def test_child_flowchart_events_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_block_start("lint")
    session = types.SimpleNamespace(client=types.SimpleNamespace(_query=_FakeQuery(
        [_raw_spawn_start(), raw, _raw_spawn_complete(), _raw_result()])))
    events = [e async for e in streaming_mod.stream_response(session)]
    blocks = [e for e in events if isinstance(e, BlockStart)]
    assert len(blocks) == 1 and blocks[0].session == "lint"


@pytest.mark.asyncio
async def test_child_events_do_not_mutate_parent_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthub import streaming as streaming_mod

    raw = _raw_stream_event(session="lint")
    raw["event"] = {"type": "content_block_start",
                    "content_block": {"type": "tool_use", "name": "Bash", "id": "t1"}}
    session = types.SimpleNamespace(
        client=types.SimpleNamespace(_query=_FakeQuery([raw, _raw_result()])),
        activity=types.SimpleNamespace(phase="idle", tool_name=None, query_started=None),
    )
    events = [e async for e in streaming_mod.stream_response(session)]
    assert session.activity.phase == "idle", "child tool use must not touch parent activity"
    assert session.activity.tool_name is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_streaming_engine.py -k "spawn or child" -v`
Expected: FAIL — spawn subtypes fall through to `SystemNotification`; events carry no `session`; child text not flushed on spawn_complete; child events mutate parent activity.

- [ ] **Step 3: Implement**

Add module-level helper:

```python
def _msg_session(msg: Any) -> str:
    """Session tag for a parsed SDK message: '' == parent channel."""
    ctxd = getattr(msg, "_session_context", None)
    if ctxd and ctxd.get("session"):
        return ctxd.get("session", "")
    data = getattr(msg, "data", None)
    if isinstance(data, dict):
        return str(data.get("data", {}).get("session", ""))
    return ""
```

In `stream_response`, add `child_ctxs: dict[str, _Ctx] = {}` and route each message:

```python
    async for msg in receive_response_safe(session):
        msg_session = _msg_session(msg)
        is_child = bool(msg_session) and msg_session != "main"
        if not is_child:
            ctx.msg_total += 1  # msg_total counts parent messages only
        sctx = child_ctxs.setdefault(msg_session, _Ctx()) if is_child else ctx

        if isinstance(msg, StreamEvent):
            async for out in _handle_stream_event(sctx, session, msg, set_session_id_fn,
                                                  session_tag=msg_session):
                yield out
        elif isinstance(msg, AssistantMessage):
            async for out in _handle_assistant_message(sctx, session, msg, session_tag=msg_session):
                yield out
        elif isinstance(msg, ResultMessage):
            async for out in _handle_result_message(
                sctx, session, msg, set_session_id_fn, record_usage_fn, session_tag=msg_session
            ):
                yield out
        elif isinstance(msg, SystemMessage):
            async for out in _handle_system_message(
                sctx, session, msg, self_compacting_names, compact_start_times,
                pending_compact, session_tag=msg_session,
            ):
                yield out
                if isinstance(out, SpawnEnd):
                    # The child's session name is its agent_name (the engine
                    # names child sessions after the spawn block's agent_name).
                    # Flush the child's leftover text, then drop its ctx.
                    tail = child_ctxs.get(out.agent_name)
                    if tail is not None and tail.text_buffer.strip():
                        tail.flush_count += 1
                        yield TextFlush(text=tail.text_buffer, reason="spawn_complete",
                                        session=out.agent_name)
                        tail.text_buffer = ""
                    child_ctxs.pop(out.agent_name, None)

        # Mid-turn text splitting — per-session buffer
        if not sctx.hit_rate_limit and len(sctx.text_buffer) >= 1800:
            split_at = sctx.text_buffer.rfind("\n", 0, 1800)
            if split_at == -1:
                split_at = 1800
            flush_text = sctx.text_buffer[:split_at]
            sctx.text_buffer = sctx.text_buffer[split_at:].lstrip("\n")
            sctx.flush_count += 1
            yield TextFlush(text=flush_text, reason="mid_turn_split", session=msg_session)
```

`session_tag` threading — add `session_tag: str = ""` param to `_handle_stream_event`, `_handle_assistant_message`, `_handle_result_message`, `_handle_system_message`; yield-tag every event:

```python
        yield SessionId(session_id=msg.session_id, session=session_tag)
```
…and so on for each `yield` in the four handlers (all `StreamOutput` events get `session=session_tag`).

In `_handle_result_message`, gate session-id/usage side effects to the parent (a child's result must never overwrite the parent agent's persisted session or usage):

```python
    if not session_tag and not is_flowchart and set_session_id_fn:
        await set_session_id_fn(session, msg)

    if not session_tag and not is_flowchart and record_usage_fn:
        record_usage_fn(session.name, msg)
```

Child-state isolation inside `_handle_stream_event` — parent-only side effects:

```python
    # Session ID tracking (parent only — a child's session_id must never
    # overwrite the parent agent's persisted session).
    if not session_tag and not ctx.in_flowchart and msg.session_id and msg.session_id != session.session_id:
        if set_session_id_fn:
            await set_session_id_fn(session, msg.session_id)
        yield SessionId(session_id=msg.session_id)

    # Activity tracking (parent only — child events have no AgentSession)
    if not session_tag:
        update_activity(session.activity, event)
```

In `_handle_system_message`, add before the `else` branch:

```python
    elif msg.subtype == "spawn_start":
        data = msg.data.get("data", {})
        yield SpawnStart(
            agent_name=data.get("agent_name", ""),
            command_name=data.get("command_name", ""),
            model=data.get("model", ""),
            backend=data.get("backend", ""),
            parent_session=data.get("parent_session", ""),
            session=_msg_session(msg),
        )

    elif msg.subtype == "spawn_complete":
        data = msg.data.get("data", {})
        yield SpawnEnd(
            agent_name=data.get("agent_name", ""),
            status=data.get("status", ""),
            duration_ms=data.get("duration_ms", 0),
            cost_usd=data.get("cost_usd", 0.0),
            session=_msg_session(msg),
        )
```

Gate ONLY the compaction branches to the parent session — block/flowchart/spawn branches must still run for children (they route to the child thread):

```python
    if session_tag and msg.subtype in ("status", "compact_boundary"):
        return  # child compactions don't render; the parent thread owns compaction UX
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_streaming_engine.py -k "spawn or child" -v`
Expected: PASS.

Then run the full agenthub suite for regressions:

Run: `uv run pytest tests/unit/test_streaming_engine.py tests/unit/test_stream_types.py tests/unit/test_phase8_capstone.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/agenthub/agenthub/streaming.py tests/unit/test_streaming_engine.py
git commit -m "feat(agenthub): per-session streaming state, session tagging, spawn events"
```

---

### Task 4: Config vars

**Files:**
- Modify: `axi/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `config.FC_SPAWN_THREADS: bool` (default True), `config.FC_THREAD_GRACE_SECS: int` (default 300). Consumed by Tasks 6-7.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_config.py` (follow existing env-var test conventions):

```python
def test_fc_spawn_threads_defaults_on() -> None:
    assert config.FC_SPAWN_THREADS is True


def test_fc_spawn_threads_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_SPAWN_THREADS", "0")
    assert config.FC_SPAWN_THREADS is False


def test_fc_thread_grace_secs_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FC_THREAD_GRACE_SECS", raising=False)
    assert config.FC_THREAD_GRACE_SECS == 300


def test_fc_thread_grace_secs_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_THREAD_GRACE_SECS", "5")
    assert config.FC_THREAD_GRACE_SECS == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py -k "fc_spawn or fc_thread" -v`
Expected: FAIL — attributes don't exist.

- [ ] **Step 3: Implement**

In `axi/config.py`, next to the other `FC_*`/`STREAMING_DISCORD` env reads (line ~248):

```python
FC_SPAWN_THREADS = os.environ.get("FC_SPAWN_THREADS", "1").lower() not in ("0", "false", "no", "off")
FC_THREAD_GRACE_SECS = _env_int("FC_THREAD_GRACE_SECS", 300)
```

If no `_env_int` helper exists in the file, use:

```python
try:
    FC_THREAD_GRACE_SECS = max(0, int(os.environ.get("FC_THREAD_GRACE_SECS", "300")))
except ValueError:
    FC_THREAD_GRACE_SECS = 300
```

(Check the file for an existing int-parse helper first — reuse it if present.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py -k "fc_spawn or fc_thread" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add axi/config.py tests/unit/test_config.py
git commit -m "feat: FC_SPAWN_THREADS and FC_THREAD_GRACE_SECS config"
```

---

### Task 5: DiscordAgentState — spawn thread state

**Files:**
- Modify: `axi/axi_types.py:60-125`
- Test: `tests/unit/test_axi_types.py`

**Interfaces:**
- Produces: `discord_state(session).spawn_threads: dict[str, int]` (child agent name → thread id), `discord_state(session).pending_archives: dict[str, asyncio.Task]` (agent name → archive task). Survives renderer recreation; consumed by Tasks 6-7.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_axi_types.py`:

```python
def test_discord_state_spawn_threads_default_empty() -> None:
    session = _make_session()
    ds = discord_state(session)
    assert ds.spawn_threads == {}
    assert ds.pending_archives == {}


def test_discord_state_spawn_threads_roundtrip() -> None:
    session = _make_session()
    ds = discord_state(session)
    ds.spawn_threads["lint"] = 123456789
    assert discord_state(session).spawn_threads == {"lint": 123456789}
```

(`_make_session` per existing test helpers in that file — an `AgentSession` with `frontend_state=None`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_axi_types.py -k spawn_threads -v`
Expected: FAIL — attribute errors.

- [ ] **Step 3: Implement**

In `DiscordAgentState`:

```python
    # FlowCoder spawn threads: child agent name -> thread id (survives
    # renderer recreation on reconnect; renderer re-fetches Thread objects).
    spawn_threads: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    # Archive tasks pending the grace delay after spawn_complete.
    pending_archives: dict[str, asyncio.Task] = field(default_factory=lambda: dict[str, asyncio.Task]())
```

(`asyncio` is already imported in `axi_types.py` — verify; add `import asyncio` if not.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_axi_types.py -k spawn_threads -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add axi/axi_types.py tests/unit/test_axi_types.py
git commit -m "feat: spawn thread state on DiscordAgentState"
```

---

### Task 6: Renderer — session routing and per-child buffers

**Files:**
- Modify: `axi/discord_stream_renderer.py`
- Test: `tests/unit/test_discord_renderer_spawn_threads.py` (new)

**Interfaces:**
- Consumes: Task 1 events (`event.session` on routed events, `SpawnStart`/`SpawnEnd`), Task 4 config, Task 5 `discord_state(...).spawn_threads`, `send_long`/`audited_channel_send`/`_retry_discord_503` from `axi.agents`/`axi.discord_wire`/`axi.discord_stream` (existing imports).
- Produces: `_resolve_target(session) -> TextChannel | Thread | None`, `_send_system(text, session="")`, `_send_child(session, text)`; per-child `_child_deferred: dict[str, str]`, `_child_suppress: dict[str, bool]`, `_child_fc_command: dict[str, str]`; `_on_spawn_start`/`_on_spawn_end` (Task 7).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_discord_renderer_spawn_threads.py` following the conventions of `test_discord_renderer_block_suppression.py` (env setup, monkeypatched capture fixtures):

```python
"""Renderer session-routing + spawn thread behavior."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from agenthub.stream_types import (
    BlockStart,
    SpawnEnd,
    SpawnStart,
    StreamEnd,
    TextFlush,
)
from agenthub.types import AgentSession
from axi.discord_stream_renderer import DiscordStreamRenderer


class _FakeThread:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = 888
        self.jump_url = f"https://discord.com/channels/1/2/{self.id}"
        self.archived = False

    async def archive(self) -> None:
        self.archived = True


class _FakeChannel:
    def __init__(self) -> None:
        self.id = 4242
        self.threads: list[_FakeThread] = []

    async def create_thread(self, *, name: str, auto_archive_duration: int) -> _FakeThread:
        t = _FakeThread(name)
        self.threads.append(t)
        return t


class _FakeBot:
    def __init__(self) -> None:
        self._threads: dict[int, _FakeThread] = {}

    def get_channel(self, thread_id: int) -> _FakeThread | None:
        return self._threads.get(thread_id)


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    """Capture audited_channel_send (channel-or-thread, text) — _send_system path."""
    captured: list[tuple[Any, str]] = []

    async def _fake(channel: Any, text: str, **_kw: Any) -> None:
        captured.append((channel, text))

    import axi.discord_wire

    monkeypatch.setattr(axi.discord_wire, "audited_channel_send", _fake)
    return captured


@pytest.fixture
def flushed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    """Capture send_long (channel-or-thread, text) — assistant text path."""
    captured: list[tuple[Any, str]] = []

    async def _fake_send_long(channel: Any, text: str) -> Any:
        captured.append((channel, text))
        return None

    import axi.agents

    monkeypatch.setattr(axi.agents, "send_long", _fake_send_long)
    return captured


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> AgentSession:
    """Register a real AgentSession under 'agent' in the agents registry."""
    import axi.agents as agents_mod

    session = AgentSession(name="agent")
    monkeypatch.setitem(agents_mod.agents, "agent", session)
    return session


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_SPAWN_THREADS", "1")
    monkeypatch.setenv("FC_THREAD_GRACE_SECS", "0")  # instant archive for tests


def _renderer(channel: _FakeChannel, bot: _FakeBot) -> DiscordStreamRenderer:
    return DiscordStreamRenderer("agent", channel, bot, streaming_enabled=False)  # type: ignore[arg-type]


async def test_child_flush_routes_to_thread(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """A child-session TextFlush sends into the child's thread, not the channel."""
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    # Child text is deferred: each flush delivers the previous one, and the
    # final buffer drains at spawn_complete (see _on_spawn_end).
    await renderer.handle(TextFlush(text="first chunk", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="second chunk", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="", reason="end_turn", session="lint"))
    await renderer.handle(SpawnEnd(agent_name="lint", status="completed", session=""))

    assert any(t is thread and "first chunk" in text for t, text in flushed)
    assert any(t is thread and "second chunk" in text for t, text in flushed)
    assert not any(t is channel for t, _ in flushed), "no child text in the parent channel"


async def test_child_flush_without_thread_falls_back_prefixed(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    """No thread recorded → prefixed into the parent channel, never dropped."""
    channel = _FakeChannel()
    renderer = _renderer(channel, _FakeBot())

    await renderer.handle(TextFlush(text="orphan output", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="", reason="end_turn", session="lint"))
    # Drain the deferred buffer through the spawn-end path (fallback target).
    await renderer.handle(SpawnEnd(agent_name="lint", status="completed", session=""))

    assert any(
        t is channel and "[lint]" in text and "orphan output" in text for t, text in flushed
    )


async def test_spawn_threads_disabled_matches_pre_feature_behavior(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    import os

    os.environ["FC_SPAWN_THREADS"] = "0"
    channel = _FakeChannel()
    renderer = _renderer(channel, _FakeBot())

    await renderer.handle(TextFlush(text="plain output", reason="mid_turn_split", session="lint"))
    await renderer.handle(TextFlush(text="", reason="end_turn", session="lint"))

    assert any(t is channel and "plain output" in text and "[lint]" not in text
               for t, text in flushed)


async def test_child_output_schema_suppression_is_per_session(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(BlockStart(
        block_name="Schema", block_type="prompt",
        has_output_schema=True, session="lint",
    ))
    # Parent text must NOT be suppressed by a child's output-schema block
    await renderer.handle(TextFlush(text="parent visible", reason="end_turn"))

    assert any(t is channel and "parent visible" in text for t, text in flushed)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_discord_renderer_spawn_threads.py -v`
Expected: FAIL — `_send_system`/flush ignore `event.session`; child text lands in the channel.

- [ ] **Step 3: Implement**

In `DiscordStreamRenderer.__slots__`, add:

```python
        "_child_deferred",
        "_child_fc_command",
        "_child_suppress",
```

In `__init__`, after `self._fc_command`:

```python
        # Per-child-session rendering state (flush-based, no live-edit).
        self._child_deferred: dict[str, str] = {}
        self._child_suppress: dict[str, bool] = {}
        self._child_fc_command: dict[str, str] = {}
```

Add routing helper:

```python
    def _resolve_target(self, session_name: str):
        """Thread for a child session, the channel for parent, None = fallback.

        Fallback (None) means: render into the channel with a [agent] prefix.
        When FC_SPAWN_THREADS is off, children render into the channel
        unlabeled — exactly the pre-feature behavior.
        """
        if not session_name or session_name == "main":
            return self._channel
        if not config.FC_SPAWN_THREADS:
            return self._channel
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None or self._bot is None:
            return None
        thread_id = discord_state(session).spawn_threads.get(session_name)
        if not thread_id:
            return None
        return self._bot.get_channel(thread_id)
```

Change `_send_system` to take a session and route:

```python
    async def _send_system(self, text: str, session: str = "") -> None:
        from axi.discord_wire import audited_channel_send

        target = self._resolve_target(session)
        if target is None:
            target = self._channel
            text = f"[{session}] {text}"
        try:
            await audited_channel_send(
                target, text, operation="stream_renderer"
            )
        except Exception:
            log.debug("Failed to send system message for '%s'", self._agent_name)
```

`_send_long` similarly gains `session: str = ""` — resolve target; when `target is None`, send `f"[{session}] {text}"` to `self._channel` via `send_long`.

Child text routing in `_on_text_delta` / `_on_text_flush`:

```python
    async def _on_text_delta(self, event: TextDelta) -> None:
        if event.session and event.session != "main":
            return  # child text arrives via TextFlush (flush-based rendering)
        ...existing body...

    async def _on_text_flush(self, event: TextFlush) -> None:
        s = event.session
        if s and s != "main":
            if self._child_suppress.get(s):
                self._child_deferred.pop(s, None)
                return
            text = event.text
            if not text.strip():
                return
            deferred = self._child_deferred.get(s, "")
            if deferred:
                await self._send_child(s, deferred)
            self._child_deferred[s] = text.lstrip()
            return
        ...existing body...
```

New helper:

```python
    async def _send_child(self, session_name: str, text: str) -> None:
        """Flush buffered child text into its thread (or prefixed fallback)."""
        from axi.agents import send_long

        target = self._resolve_target(session_name)
        if target is None:
            await send_long(self._channel, f"[{session_name}] {text}")
            return
        await send_long(target, text)
```

Child block handlers in `_on_block_start` / `_on_block_complete`:

```python
    async def _on_block_start(self, event: BlockStart) -> None:
        s = event.session
        if s and s != "main":
            if event.block_type in _SILENT_BLOCK_TYPES:
                return
            self._child_suppress[s] = event.has_output_schema and not _show_output_schema()
            if self._child_fc_command.get(s) in _FC_QUIET_COMMANDS and not self._verbose():
                return
            label = f"**{event.block_name}**" if event.block_name else "?"
            block_type = f" (`{event.block_type}`)" if event.block_type else ""
            await self._send_system(f"▶ {label}{block_type}", s)
            return
        ...existing body...

    async def _on_block_complete(self, event: BlockComplete) -> None:
        s = event.session
        if s and s != "main":
            self._child_suppress.pop(s, None)
            if not event.success:
                await self._send_system(
                    f"❌ Block **{event.block_name}** failed", s
                )
            return
        ...existing body...
```

Child thinking indicators — per-session message id, routed into the child's target (full-stream fidelity: thinking renders in the thread, and a child's indicator must never delete the parent's):

Change the dispatch lines in `handle()` to pass the event:

```python
        elif isinstance(event, ThinkingStart):
            await self._on_thinking_start(event)
        elif isinstance(event, ThinkingEnd):
            await self._on_thinking_end(event)
```

Add `"_child_thinking_msg_id"` to `__slots__` and `self._child_thinking_msg_id: dict[str, str] = {}` to `__init__`:

```python
    async def _on_thinking_start(self, event: ThinkingStart) -> None:
        s = event.session
        if s and s != "main":
            target = self._resolve_target(s)
            if target is None:
                return
            try:
                resp = await _retry_discord_503(
                    config.discord_client.send_message,
                    target.id,
                    "*thinking...*",
                )
                self._child_thinking_msg_id[s] = resp["id"]
            except Exception:
                log.debug("Failed to post thinking indicator for '%s'", self._agent_name)
            return
        ...existing body...

    async def _on_thinking_end(self, event: ThinkingEnd) -> None:
        s = event.session
        if s and s != "main":
            msg_id = self._child_thinking_msg_id.pop(s, None)
            if msg_id:
                target = self._resolve_target(s)
                if target is not None:
                    try:
                        await _retry_discord_503(
                            config.discord_client.delete_message,
                            target.id,
                            msg_id,
                        )
                    except Exception:
                        log.debug("Failed to delete thinking indicator for '%s'", self._agent_name)
            # Verbose mode attaches the full thinking text as a file in the thread.
            thinking = (event.thinking_text or "").strip()
            if thinking and self._verbose() and self._resolve_target(s) is not None:
                await self._post_verbose_file(thinking, s)
            return
        ...existing body...
```

`_post_verbose_file` gains a `session: str = ""` param; the `audited_channel_send` target becomes `self._resolve_target(session) or self._channel`, and the child variant passes the thinking text with `session=s` and routes to the resolved target (`_resolve_target` returns `None` for a missing thread → skip, since the fallback prefix on a file is meaningless).

Child tool-use narration — route announcements and verbose narration into the child's target:

```python
    async def _on_tool_use_start(self, event: ToolUseStart) -> None:
        s = event.session
        if s and s != "main":
            if event.tool_use_id:
                self._tool_parents[event.tool_use_id] = event.parent_tool_use_id
            await self._announce_agent_tool_use(event.tool_name, event, None, s)
            return
        ...existing body...

    async def _on_tool_use_end(self, event: ToolUseEnd) -> None:
        s = event.session
        if s and s != "main":
            await self._announce_agent_tool_use(event.tool_name, event, event.tool_input, s)
            if event.tool_name and self._verbose():
                preview = f": {event.preview[:120]}" if event.preview else ""
                await self._send_system(f"`🔧 {event.tool_name}{preview}`", s)
            return
        ...existing body...
```

`_announce_agent_tool_use` gains a `session: str = ""` target param; the `_render_chunked(self._channel, ...)` calls become `_render_chunked(self._resolve_target(session) or self._channel, ...)` (fallback keeps announcements visible).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_discord_renderer_spawn_threads.py -v`
Expected: PASS.

Then run the existing renderer suites for regressions:

Run: `uv run pytest tests/unit/test_discord_renderer_*.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add axi/discord_stream_renderer.py tests/unit/test_discord_renderer_spawn_threads.py
git commit -m "feat(renderer): per-session routing and child buffers"
```

---

### Task 7: Renderer — thread lifecycle (spawn start/end, fallback, stream-end cleanup)

**Files:**
- Modify: `axi/discord_stream_renderer.py`
- Test: `tests/unit/test_discord_renderer_spawn_threads.py`

**Interfaces:**
- Consumes: Task 6's `_resolve_target`/`_send_system(session)`; Task 5's `spawn_threads`/`pending_archives`; Task 4's grace config.
- Produces: `_on_spawn_start(SpawnStart)` creates threads (recursive ancestry naming), `_on_spawn_end(SpawnEnd)` posts summary + schedules archive, `_on_stream_end` archives never-completed threads with `**interrupted**`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_discord_renderer_spawn_threads.py`:

```python
async def test_spawn_start_creates_thread_and_posts_status(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnStart(
        agent_name="lint", command_name="lint-fix", model="opus",
        backend="claude", parent_session="main",
    ))

    assert len(channel.threads) == 1
    thread = channel.threads[0]
    assert thread.name == "lint"
    assert discord_state(agent).spawn_threads == {"lint": thread.id}
    assert any(
        t is channel and "spawned" in text and "lint" in text for t, text in posted
    ), "parent status line"
    assert any(
        t is thread and "Spawned agent **lint**" in text for t, text in flushed
    ), "spawned line in thread"
    assert any(
        t is thread and "lint-fix" in text and "opus" in text for t, text in flushed
    ), "command/model line in thread"


async def test_nested_spawn_gets_ancestry_name(
    env, agent: AgentSession, posted: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    parent_thread = _FakeThread("lint")
    parent_thread.id = 888
    bot._threads[888] = parent_thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnStart(
        agent_name="fmt", command_name="fmt-do", parent_session="lint",
    ))

    assert len(channel.threads) == 1
    assert channel.threads[0].name == "lint/fmt"
    assert any(
        t is parent_thread and "fmt" in text and "spawned" in text for t, text in posted
    ), "nested spawn status line routes into the parent's thread"


async def test_spawn_end_routes_status_to_emitting_session(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    """A nested spawn's completion line goes into the parent's thread."""
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    parent_thread = _FakeThread("lint")
    parent_thread.id = 888
    bot._threads[888] = parent_thread
    ds = discord_state(agent)
    ds.spawn_threads["lint"] = 888
    ds.spawn_threads["fmt"] = 889
    child_thread = _FakeThread("lint/fmt")
    child_thread.id = 889
    bot._threads[889] = child_thread
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnEnd(
        agent_name="fmt", status="completed", duration_ms=500,
        cost_usd=0.0, session="lint",
    ))

    assert any(
        t is parent_thread and "fmt" in text and "completed" in text for t, text in posted
    ), "completion line in the parent's thread"
    assert any(t is child_thread and "Spawn **completed**" in text for t, text in flushed)


async def test_spawn_end_posts_summary_and_archives(
    env, agent: AgentSession, posted: list[tuple[Any, str]], flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(SpawnEnd(
        agent_name="lint", status="completed", duration_ms=1234,
        cost_usd=0.042, session="",
    ))

    assert any(t is thread and "Spawn **completed**" in text for t, text in flushed)
    assert any(t is thread and "123.4" in text for t, text in flushed), "duration in summary"
    assert any(t is thread and "$0.0420" in text for t, text in flushed), "cost in summary"
    assert any(t is channel and "lint" in text and "completed" in text for t, text in posted)
    assert "lint" not in discord_state(agent).spawn_threads, "mapping removed"
    await asyncio.sleep(0.1)  # let the (0-grace) archive task run
    assert thread.archived


async def test_spawn_end_without_thread_is_noop(
    env, agent: AgentSession, posted: list[tuple[Any, str]]
) -> None:
    renderer = _renderer(_FakeChannel(), _FakeBot())
    await renderer.handle(SpawnEnd(agent_name="ghost", status="completed", session=""))
    assert posted == []


async def test_stream_end_archives_unfinished_threads(
    env, agent: AgentSession, flushed: list[tuple[Any, str]]
) -> None:
    from axi.axi_types import discord_state

    channel = _FakeChannel()
    bot = _FakeBot()
    thread = _FakeThread("lint")
    bot._threads[888] = thread
    discord_state(agent).spawn_threads["lint"] = 888
    renderer = _renderer(channel, bot)

    await renderer.handle(StreamEnd(elapsed_s=1.0))

    assert thread.archived
    assert any(t is thread and "interrupted" in text for t, text in flushed)
    assert discord_state(agent).spawn_threads == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_discord_renderer_spawn_threads.py -k "spawn_start or spawn_end or stream_end" -v`
Expected: FAIL — `SpawnStart`/`SpawnEnd` hit `_on_system_notification` fallback (unhandled), no threads created.

- [ ] **Step 3: Implement**

Add `SpawnStart`/`SpawnEnd` to `handle()` dispatch:

```python
        elif isinstance(event, SpawnStart):
            await self._on_spawn_start(event)
        elif isinstance(event, SpawnEnd):
            await self._on_spawn_end(event)
```

Implement:

```python
    async def _on_spawn_start(self, event: SpawnStart) -> None:
        if not config.FC_SPAWN_THREADS:
            return
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None or self._bot is None:
            await self._send_system(f"▶ spawned **{event.agent_name}**", event.session)
            return
        ds = discord_state(session)
        if event.agent_name in ds.spawn_threads:
            return  # duplicate spawn_start — already handled
        thread_name = event.agent_name
        parent_name = event.parent_session or "main"
        if parent_name != "main":
            parent_id = ds.spawn_threads.get(parent_name)
            parent_thread = self._bot.get_channel(parent_id) if parent_id else None
            if parent_thread is not None:
                thread_name = f"{parent_thread.name}/{event.agent_name}"
        try:
            thread = await self._channel.create_thread(
                name=thread_name, auto_archive_duration=60
            )
        except Exception as e:
            log.warning(
                "Failed to create thread for spawned '%s': %s",
                event.agent_name, e,
            )
            await self._send_system(f"▶ spawned **{event.agent_name}** (no thread)", event.session)
            return
        ds.spawn_threads[event.agent_name] = thread.id
        # Status line routes to the emitting (parent) session: the parent
        # channel for top-level spawns, the parent's thread for nested ones.
        await self._send_system(f"▶ spawned **{event.agent_name}** → {thread.jump_url}", event.session)
        await self._send_child(event.agent_name, f"▶ Spawned agent **{event.agent_name}**")
        details = []
        if event.command_name:
            details.append(f"running `{event.command_name}`")
        if event.model:
            details.append(f"model `{event.model}`")
        if event.backend:
            details.append(f"backend `{event.backend}`")
        if details:
            await self._send_child(event.agent_name, " | ".join(details))

    async def _on_spawn_end(self, event: SpawnEnd) -> None:
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None:
            return
        ds = discord_state(session)
        # Drain any deferred child text FIRST — it must surface even when the
        # thread is gone or was never created (fallback prefix applies).
        deferred = self._child_deferred.pop(event.agent_name, "")
        if deferred:
            await self._send_child(event.agent_name, deferred)
        thread_id = ds.spawn_threads.get(event.agent_name)
        if not thread_id:
            return  # no thread (creation failed, or FC_SPAWN_THREADS=0)
        thread = self._bot.get_channel(thread_id) if self._bot else None
        if thread is not None:
            status = {
                "completed": "**completed**",
                "failed": "**failed**",
                "cancelled": "**cancelled**",
            }.get(event.status, "**finished**")
            summary = f"Spawn {status}"
            parts = []
            if event.duration_ms:
                parts.append(f"{event.duration_ms / 1000:.1f}s")
            if event.cost_usd:
                parts.append(f"${event.cost_usd:.4f}")
            if parts:
                summary += f" | {' | '.join(parts)}"
            await self._send_child(event.agent_name, summary)
        else:
            log.warning("Spawn thread gone for '%s' (id=%s)", event.agent_name, thread_id)
        ds.spawn_threads.pop(event.agent_name, None)
        await self._send_system(
            f"spawned **{event.agent_name}** {event.status or 'finished'}", event.session
        )
        ds.pending_archives[event.agent_name] = asyncio.create_task(
            self._archive_after(thread_id, event.agent_name, config.FC_THREAD_GRACE_SECS)
        )

    async def _archive_after(self, thread_id: int, agent_name: str, delay: float) -> None:
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        try:
            await asyncio.sleep(delay)
            thread = self._bot.get_channel(thread_id) if self._bot else None
            if thread is not None:
                await thread.archive()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Failed to archive thread for '%s': %s", agent_name, e)
        finally:
            session = _agents_mod.agents.get(self._agent_name)
            if session is not None:
                discord_state(session).pending_archives.pop(agent_name, None)
```

In `_on_stream_end`, after `await self.stop_typing()` and before the rate-limit return:

```python
        await self._cleanup_unfinished_spawn_threads()
```

```python
    async def _cleanup_unfinished_spawn_threads(self) -> None:
        """Archive threads whose spawn never completed (hard kill / no event).

        Only threads still in spawn_threads — spawns that completed are in
        pending_archives and left to their grace-delay task.
        """
        from axi import agents as _agents_mod
        from axi.axi_types import discord_state
        session = _agents_mod.agents.get(self._agent_name)
        if session is None:
            return
        ds = discord_state(session)
        for agent_name, thread_id in list(ds.spawn_threads.items()):
            thread = self._bot.get_channel(thread_id) if self._bot else None
            if thread is None:
                ds.spawn_threads.pop(agent_name, None)
                continue
            try:
                await self._send_child(agent_name, "Spawn **interrupted** — stream ended")
                await thread.archive()
            except Exception as e:
                log.warning("Failed to archive interrupted thread for '%s': %s", agent_name, e)
            ds.spawn_threads.pop(agent_name, None)
```

Note: `_on_spawn_end`'s parent status line uses `_send_system(...)` with the parent session (default `""`), so it lands in the parent channel via the `audited_channel_send` path — the `posted` fixture captures it. `_send_child` uses `send_long`, which the `flushed` fixture captures; for `_on_spawn_end`'s deferred flush the capture assertion above relies on `posted` only for summary lines (which go through `_send_child` → `send_long`). If any assertion above is ambiguous, capture both fixtures (`posted, flushed`) in the test signature — both fixtures are composable.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_discord_renderer_spawn_threads.py -v`
Expected: PASS.

Then the full renderer + hub suites:

Run: `uv run pytest tests/unit/test_discord_renderer_*.py tests/unit/test_hub_wiring.py tests/unit/test_phase8_capstone.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add axi/discord_stream_renderer.py tests/unit/test_discord_renderer_spawn_threads.py
git commit -m "feat(renderer): spawn thread lifecycle, fallback, stream-end archive"
```

---

### Task 8: flowcoder-engine PR — session-tagged lifecycle events

**Files (external repo `Gaia-PBC/flowcoder-core`):**
- Modify: `packages/flowcoder-engine/flowcoder_engine/protocol.py`
- Modify: `packages/flowcoder-engine/flowcoder_engine/walker.py`
- Test: engine-side tests per that repo's existing conventions (`packages/flowcoder-engine/tests/`)

**Interfaces:**
- Produces (consumed by Tasks 3/6-7 wire-format): `block_start`/`block_complete`/`block_timeout`/`flowchart_start`/`flowchart_complete` system events carry `data.session` (emitting walker's `self._session.name`); new `spawn_start` event `{agent_name, command_name, model, backend, cwd, parent_session, session}` (session == parent_session, the emitting walker); new `spawn_complete` event `{agent_name, status, duration_ms, cost_usd, result, session}`. Wire format per spec Section 1 (extended with `session` for uniform tagging — see the Deviation note above).

This task runs in a fresh clone of `https://github.com/Gaia-PBC/flowcoder-core.git` (no local checkout exists; `pyproject.toml` sources point at the git remote). Work on a branch, open a PR, do NOT touch the axi repo here.

- [ ] **Step 1: Write the failing engine tests**

In the flowcoder-core repo, add tests following the engine package's existing test layout (check `packages/flowcoder-engine/tests/` for the harness pattern; the engine exposes `ProtocolHandler` with a `sys.stdout` write — tests typically monkeypatch `sys.stdout`):

```python
def test_block_start_payload_includes_session(monkeypatch):
    captured = {}
    protocol = ProtocolHandler()
    def fake_emit(msg):
        captured.clear()
        captured.update(msg)
    monkeypatch.setattr(protocol, "emit", fake_emit)
    protocol.emit_block_start("b1", "Prompt", "prompt", session="lint")
    assert captured["data"]["session"] == "lint"


def test_spawn_start_payload(monkeypatch):
    captured = {}
    protocol = ProtocolHandler()
    def fake_emit(msg):
        captured.clear()
        captured.update(msg)
    monkeypatch.setattr(protocol, "emit", fake_emit)
    protocol.emit_spawn_start(agent_name="lint", command_name="lint-fix",
                              model="opus", backend="claude", cwd="/tmp",
                              parent_session="main", session="main")
    assert captured["subtype"] == "spawn_start"
    assert captured["data"]["agent_name"] == "lint"
    assert captured["data"]["parent_session"] == "main"
    assert captured["data"]["session"] == "main", "emitting (parent) session"


def test_spawn_complete_payload(monkeypatch):
    captured = {}
    protocol = ProtocolHandler()
    def fake_emit(msg):
        captured.clear()
        captured.update(msg)
    monkeypatch.setattr(protocol, "emit", fake_emit)
    protocol.emit_spawn_complete(agent_name="lint", status="completed",
                                 duration_ms=1234, cost_usd=0.042, result="{}",
                                 session="main")
    assert captured["data"]["status"] == "completed"
    assert captured["data"]["cost_usd"] == 0.042
    assert captured["data"]["session"] == "main"
```

Add a walker-level test that a flowchart with a spawn block + wait emits `spawn_start` then `spawn_complete` in order (drive the walker with a stub session per existing conventions).

- [ ] **Step 2: Run engine tests to verify they fail**

Run: (in flowcoder-core) the engine package test command per its `pyproject.toml` (e.g. `uv run pytest packages/flowcoder-engine/tests/`).
Expected: FAIL — `session` param unknown; `emit_spawn_start`/`emit_spawn_complete` don't exist.

- [ ] **Step 3: Implement — protocol.py**

`emit_block_start` gains `session: str = ""` and includes it in data:

```python
    def emit_block_start(self, block_id, block_name, block_type, session=""):
        """Emit block_start system message."""
        self.emit_system(
            "block_start",
            {"block_id": block_id, "block_name": block_name,
             "block_type": block_type, "session": session},
        )
```

`emit_block_complete` — add `session: str = ""` param; include `"session": session` in the data dict alongside `session_id`. `emit_block_timeout` — same. `emit_flowchart_start` / `emit_flowchart_complete` — add `session: str = ""` and include in data.

Add:

```python
    def emit_spawn_start(self, *, agent_name, command_name="", model="",
                         backend="", cwd="", parent_session="main", session=""):
        """Emit spawn_start when a spawn block creates a child agent."""
        self.emit_system(
            "spawn_start",
            {"agent_name": agent_name, "command_name": command_name,
             "model": model, "backend": backend, "cwd": cwd,
             "parent_session": parent_session, "session": session},
        )

    def emit_spawn_complete(self, *, agent_name, status, duration_ms=0,
                            cost_usd=0.0, result="", session=""):
        """Emit spawn_complete when a spawned agent finishes."""
        self.emit_system(
            "spawn_complete",
            {"agent_name": agent_name, "status": status,
             "duration_ms": duration_ms, "cost_usd": cost_usd,
             "result": result, "session": session},
        )
```

`session` is the emitting walker's `self._session.name` (the spawn's
parent) — uniform with the block/flowchart event tagging, so agenthub's
`_msg_session` reads one field everywhere. For spawn events,
`session == parent_session` always.

- [ ] **Step 4: Implement — walker.py**

Thread session into block events (emitter is always `self._session`):

```python
    # run(): block_start (line ~245)
    self._protocol.emit_block_start(
        current.id, current.name, current.type, session=self._session.name
    )
    # run(): block_complete (line ~272)
    self._protocol.emit_block_complete(
        current.id, current.name, result.success,
        session_id=self._session.session_id, session=self._session.name,
    )
    # _exec_wait timeout, _exec_input timeouts (lines ~390, ~836, ~871):
    self._protocol.emit_block_timeout(
        block.id, block.name, block.type, elapsed_ms, block.timeout_seconds,
        session=self._session.name,
    )
```

In `_exec_spawn`, after `self._spawned_sessions[agent_name] = child_session` and before `return BlockResult.ok(...)`:

```python
        self._protocol.emit_spawn_start(
            agent_name=agent_name,
            command_name=command_name,
            model=resolved_model or "",
            backend=block.backend or "",
            cwd=spawn_cwd or "",
            parent_session=self._session.name,
            session=self._session.name,
        )
```

In `_exec_wait`, success path — after `self._spawned_results[agent_name] = exec_result`:

```python
            self._protocol.emit_spawn_complete(
                agent_name=agent_name,
                status="completed" if exec_result.status == "completed" else "failed",
                duration_ms=exec_result.duration_ms,
                cost_usd=exec_result.cost_usd,
                result=json.dumps(exec_result.variables, default=str)[:2000],
                session=self._session.name,
            )
```

(`json` is already imported in walker.py — verify.)

In `_exec_wait`, timeout branch (after `task.cancel()`):

```python
                self._protocol.emit_spawn_complete(
                    agent_name=agent_name, status="failed",
                    result=f"timed out after {block.timeout_seconds}s",
                    session=self._session.name,
                )
```

In `_exec_wait`, exception branch (after `errors.append(...)`):

```python
                self._protocol.emit_spawn_complete(
                    agent_name=agent_name, status="failed", result=str(e),
                    session=self._session.name,
                )
```

In `_cleanup_spawned`, inside the loop over not-done tasks (after `task.cancel()`):

```python
            self._protocol.emit_spawn_complete(
                agent_name=agent_name, status="cancelled",
                session=self._session.name,
            )
```

- [ ] **Step 5: Run engine tests to verify they pass**

Run: (in flowcoder-core) `uv run pytest packages/flowcoder-engine/tests/`
Expected: PASS.

- [ ] **Step 6: Open the PR**

Push the branch to `Gaia-PBC/flowcoder-core` and open a PR titled `feat: session-tagged lifecycle events + spawn_start/spawn_complete`. Note in the PR description the exact wire formats (spec Section 1) so the Axi-side consumer is reviewable. Record the merge commit SHA for Task 9.

---

### Task 9: Bump flowcoder pins + uv sync

**Files:**
- Modify: `pyproject.toml:34-35`

**Interfaces:**
- Consumes: Task 8's merged commit SHA.
- Produces: installed `flowcoder-engine`/`flowcoder-flowchart` carrying `session` on block events + `spawn_start`/`spawn_complete`.

- [ ] **Step 1: Update the pins**

In `pyproject.toml`, replace `rev = "1f2f2b4437dcc0b9aa54aabfa7f7ecba1a34c042"` with the merge commit SHA of the Task 8 PR, on BOTH the `flowcoder-engine` and `flowcoder-flowchart` lines.

- [ ] **Step 2: Sync and verify**

Run: `uv lock && uv sync`
Expected: succeeds; `uv run python -c "import flowcoder_engine; from flowcoder_engine.protocol import ProtocolHandler; assert 'session' in ProtocolHandler.emit_block_start.__code__.co_varnames"` prints nothing and exits 0.

- [ ] **Step 3: Run the full unit suite**

Run: `uv run pytest tests/unit -q`
Expected: PASS (the wire-compat behavior — no-session events render as parent — is covered by existing tests plus Task 3's suite).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: bump flowcoder-engine/flowchart to spawn-events rev"
```

---

### Task 10: E2E live smoke

**Files:**
- Create (throwaway, deleted after): `commands/spawn-thread-test.json`
- Docs consulted: `docs/e2e-test-strategy.md`, existing `commands/smoke-and-notify.json` for command-JSON conventions.

**Interfaces:**
- Consumes: everything above end-to-end.

- [ ] **Step 1: Create the throwaway spawn flowchart**

`commands/spawn-thread-test.json` — a flowchart that spawns one child (`agent_name: "threadprobe"`) running a simple command (e.g. the bundled `prompt` wrapper or a minimal echo-style command), then waits. Match the schema in `commands/smoke-and-notify.json`: each block carries `id`/`type`/`name`/`position`, connections carry `id`/`source_block_id`/`target_block_id`/`source_port`/`target_port`/`is_true_path`/`condition`/`label`, and the flowchart uses `start_block_id`:

```json
{
  "id": "ad000031-thread-4000-8000-probe000001",
  "name": "spawn-thread-test",
  "description": "Throwaway e2e probe: spawn a child and wait (spawn-thread smoke).",
  "flowchart": {
    "blocks": {
      "b0000001-start-4000-8000-000000000001": {
        "id": "b0000001-start-4000-8000-000000000001",
        "type": "start",
        "name": "START",
        "position": { "x": 150, "y": 50 }
      },
      "b0000002-spawn-4000-8000-000000000002": {
        "id": "b0000002-spawn-4000-8000-000000000002",
        "type": "spawn",
        "name": "SPAWN PROBE",
        "position": { "x": 150, "y": 150 },
        "agent_name": "threadprobe",
        "command_name": "prompt",
        "arguments": "Say hello from the child agent.",
        "inherit_variables": false
      },
      "b0000003-wait-4000-8000-000000000003": {
        "id": "b0000003-wait-4000-8000-000000000003",
        "type": "wait",
        "name": "WAIT FOR PROBE",
        "position": { "x": 150, "y": 250 },
        "wait_for": ["threadprobe"]
      },
      "b0000004-end-4000-8000-000000000004": {
        "id": "b0000004-end-4000-8000-000000000004",
        "type": "end",
        "name": "DONE",
        "position": { "x": 150, "y": 350 }
      }
    },
    "connections": [
      {
        "id": "c0000001-0000-4000-8000-000000000001",
        "source_block_id": "b0000001-start-4000-8000-000000000001",
        "target_block_id": "b0000002-spawn-4000-8000-000000000002",
        "source_port": "bottom",
        "target_port": "top",
        "is_true_path": true,
        "condition": null,
        "label": null
      },
      {
        "id": "c0000002-0000-4000-8000-000000000002",
        "source_block_id": "b0000002-spawn-4000-8000-000000000002",
        "target_block_id": "b0000003-wait-4000-8000-000000000003",
        "source_port": "bottom",
        "target_port": "top",
        "is_true_path": true,
        "condition": null,
        "label": null
      },
      {
        "id": "c0000003-0000-4000-8000-000000000003",
        "source_block_id": "b0000003-wait-4000-8000-000000000003",
        "target_block_id": "b0000004-end-4000-8000-000000000004",
        "source_port": "bottom",
        "target_port": "top",
        "is_true_path": true,
        "condition": null,
        "label": null
      }
    ],
    "start_block_id": "b0000001-start-4000-8000-000000000001"
  },
  "metadata": {
    "created": "2026-08-16T00:00:00.000000",
    "modified": "2026-08-16T00:00:00.000000",
    "version": "1.0",
    "author": "axi",
    "tags": ["throwaway", "e2e", "spawn-thread"]
  },
  "arguments": []
}
```

(Verify exact spawn-block field names against `flowcoder_flowchart/blocks.py` — `agent_name`, `command_name`, `arguments`, `inherit_variables` per the installed `SpawnBlock`; adjust `command_name` to an existing bundled command such as `prompt` and confirm its invocation convention before running.)

- [ ] **Step 2: Launch the bot with the test flowchart**

Start the bot (or use the existing procmux/bridge test harness per `docs/e2e-test-strategy.md`) with `FC_THREAD_GRACE_SECS=5` and a real `DISCORD_TOKEN` in the test guild. Trigger the flowchart by messaging the agent `/spawn-thread-test` (or whatever the command invocation is for a normal message, per `AXI_FC_WRAP` conventions — if the harness auto-wraps, invoke the command explicitly).

- [ ] **Step 3: Verify thread lifecycle via discordquery**

Using `packages/discordquery` (or `scripts/discord_mcp_server.py` helpers), assert:

1. A thread named `threadprobe` was created on the agent's channel.
2. The thread contains: the `▶ Spawned agent **threadprobe**` line, the command/model line, child text (the child's answer to `say hello from the child`), block-event lines, and a completion summary containing `Spawn **completed**`.
3. The parent channel shows the status line (`▶ spawned **threadprobe** → ...`) and the completion line, and does NOT contain the child's raw text.
4. After ~5s grace, the thread is archived (check via `discordquery` thread state).

- [ ] **Step 4: Verify nested spawn**

Repeat with a child flowchart that itself spawns (`agent_name: "grandprobe"`) → assert a thread named `threadprobe/grandprobe` exists with the grandchild's text, and that grandchild events did not leak into `threadprobe`'s thread unlabeled.

- [ ] **Step 5: Verify opt-out and interrupt**

- Restart with `FC_SPAWN_THREADS=0` → rerun step 3; assert no thread, child text in parent channel (today's behavior).
- With threads on, hard-kill the child mid-run (`/stop` or kill the bridge process) → assert the thread is archived with `Spawn **interrupted**` and no dangling `spawn_threads` entries (subsequent spawns still work).

- [ ] **Step 6: Delete the throwaway flowchart**

```bash
rm commands/spawn-thread-test.json
```

---

### Task 11: Docs

**Files:**
- Modify: `docs/axi-runtime-configuration.md`
- Modify: `prompts/refs/flowcharts.md`

- [ ] **Step 1: Document the env vars**

In `docs/axi-runtime-configuration.md`, under the FlowCoder section:

```markdown
## FlowCoder spawn threads

When a flowchart spawn block creates a child agent, the child's output and
events stream into a per-child Discord thread on the agent's channel (named
after the child; nested spawns join the ancestry chain with `/`, e.g.
`child/grandchild`). The parent channel shows a compact spawned/complete
status line. Threads are archived 5 minutes after completion.

| Env var | Default | Effect |
|---|---|---|
| `FC_SPAWN_THREADS` | `1` | `0` disables threads — child output stays in the parent channel as before |
| `FC_THREAD_GRACE_SECS` | `300` | Seconds to keep a completed spawn's thread open before archiving |
```

- [ ] **Step 2: Document the flowchart behavior**

In `prompts/refs/flowcharts.md`, add a note under the spawn-block docs: spawned agents' output streams to per-agent Discord threads (recursive naming, auto-archived); input blocks inside spawned children still render in the parent channel.

- [ ] **Step 3: Commit**

```bash
git add docs/axi-runtime-configuration.md prompts/refs/flowcharts.md
git commit -m "docs: flowcoder spawn threads"
```
