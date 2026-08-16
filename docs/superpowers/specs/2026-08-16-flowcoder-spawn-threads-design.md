# Streaming Flowcoder Spawn-Block Outputs and Events to Discord Threads

Date: 2026-08-16. Status: approved design, pending implementation plan.

## Problem

When a flowcoder flowchart executes a spawn block, the engine spawns a child
Claude subprocess that runs its own flowchart. Today the child's output
streams into the *parent agent's* Discord channel, interleaved with the
parent's own output, with no session labeling. Block events
(`block_start`, `block_complete`) from the child are indistinguishable from
the parent's. Spawn lifecycle has no machine-readable events at all — only
a stderr log line. For multi-spawn flowcharts (parallel lint/format/test
agents), the parent channel becomes an unreadable interleave.

We need:

- Each spawned agent's output and events streamed into its own Discord
  thread on the parent agent's channel.
- Spawn lifecycle events (spawn start / spawn complete) so Axi knows when
  to create and archive threads.
- Full-stream fidelity in the thread: text deltas, block events, tool-use
  narration, thinking (verbose mode), result summary — mirroring what the
  parent channel shows for the parent.
- Recursive threads: grandchildren (spawned agents' spawned agents) get
  their own threads, named with the ancestry chain.
- Nested thread naming: `<parent-thread-name>/<child-agent-name>`,
  `/`-joined per ancestry depth.
- Thread lifecycle: created at spawn, completion summary posted, archived
  after a 5-minute grace delay so readers can see it open. 60-minute
  Discord `auto_archive_duration` backstops bot death/restart.
- Parent channel stays readable: compact `▶ spawned <agent> → thread`
  status line at spawn, completion line at finish.
- Opt-out env var (`FC_SPAWN_THREADS=0`) restoring today's behavior.
- Thread creation failure (permissions, 404) or unknown session degrades to
  `[agent]`-prefixed messages in the parent channel — output is never
  dropped and never crashes the stream.

## Background facts (verified)

- flowcoder-engine (git-pinned at `1f2f2b4` in `pyproject.toml`) runs one
  engine process per agent. The main session is named `"main"`
  (`__main__.py:239-242`); spawned children are `ClaudeSession(name=agent_name)`
  created via the session factory (`walker._exec_spawn` →
  `self._session_factory.create(...)` or `self._session.clone(agent_name)`).
  All sessions share one `ProtocolHandler` writing JSON-lines to stdout.
- Child inner messages are forwarded via `session_message` system envelopes
  tagged with the child's `session`/`block_id`/`block_name`
  (`protocol.py emit_forwarded`, called from `session.py stream_query`
  for `system`/`assistant`/`stream_event` types). Block/flowchart lifecycle
  events (`emit_block_start`, `emit_block_complete`, `emit_block_timeout`,
  `emit_flowchart_start`, `emit_flowchart_complete`) carry **no session
  identity** — `walker.py` emits them from whatever walker is running, and
  the data payload has no `session` field.
- The child's terminal `result` message is consumed inside
  `session.py query()` (cost/usage accounting) and **never forwarded**.
  `stream_query` yields the `result` but the walker's `_exec_prompt` path
  uses `query()`.
- Spawn lifecycle: `_exec_spawn` (walker.py:606) creates the child walker
  task and returns `BlockResult.ok(output=f"Spawned agent '{agent_name}'")`.
  Completion happens in `_exec_wait` (walker.py:724+) which awaits child
  tasks, or in `_cleanup_spawned` on halt/cancel. Neither emits an event.
- Axi's transport (`axi/flowcoder_transport.py`) unwraps `session_message`
  envelopes and stamps `inner["_session_context"] =
  {session, block_id, block_name}` on the inner message. **Nothing reads
  `_session_context` today** (grep confirms: set in transport, referenced
  nowhere else).
- SDK parsing (`claude_agent_sdk/_internal/message_parser.py`):
  `SystemMessage` keeps the full raw `data` dict (injected keys survive);
  `StreamEvent`/`AssistantMessage`/`ResultMessage` construct typed objects
  and **drop** injected keys.
- agenthub `streaming.py` normalizes raw SDK messages into `StreamOutput`
  events with a single module-level `_Ctx` per stream (one stream per agent
  turn). `block_start`/`block_complete`/`flowchart_*` become typed
  `BlockStart`/`BlockComplete`/`FlowchartStart`/`FlowchartEnd` events.
  `spawn_start`/`spawn_complete` subtypes today fall through to generic
  `SystemNotification`.
- Rendering: `axi/discord_frontend.py` creates one
  `DiscordStreamRenderer` per `StreamStart` for the agent's channel
  (`discord_frontend.py:294-302`), destroyed on `StreamEnd`. Renderer
  methods take `event: StreamOutput` and send to a fixed channel.
- Discord thread API: `auto_archive_duration` is in **minutes**; valid
  values are `60`, `1440` (1 day), `4320` (3 days), `10080` (7 days).
  Threads can only be created on channels, never inside threads.
  `TextChannel.create_thread(name=..., auto_archive_duration=60)`; archive
  via `Thread.archive()`. Bot permissions already include
  `create_public_threads`, `manage_threads`, `send_messages_in_threads`
  (`channels.py:301-330`).
- Precedent for engine changes: the provider-model-registry work shipped
  engine changes as a flowcoder-core PR and bumped the git rev pins in
  `pyproject.toml` (`flowcoder-engine`, `flowcoder-flowchart`). The same
  mechanism applies here.

## Design

### 1. flowcoder-engine PR (external, git-pinned)

PR to `Gaia-PBC/flowcoder-core`; bump `flowcoder-engine` /
`flowcoder-flowchart` rev pins in `pyproject.toml` after merge.

1. **Session-tag lifecycle events.** `emit_block_start`,
   `emit_block_complete`, `emit_block_timeout`, `emit_flowchart_start`,
   `emit_flowchart_complete` gain a `session` field in their `data`
   payload, set from `self._session.name` (the walker's session — `"main"`
   for the parent, child agent name for spawned walkers). Walker emissions
   thread it through. Backward compatible: Axi ignores it on the parent
   path (session `"main"` == parent).

2. **`spawn_start` system event** — emitted in `_exec_spawn` after the
   child task is created:

   ```json
   {"type": "system", "subtype": "spawn_start",
    "data": {"agent_name": "lint", "command_name": "lint-fix",
             "model": "claude-opus-4-8", "backend": "claude",
             "cwd": "/path", "parent_session": "main"}}
   ```

   `parent_session` is the emitting walker's `self._session.name` — the
   identity that determines thread ancestry (child of `"main"` → top-level
   thread; child of a spawned walker → nested thread).

3. **`spawn_complete` system event** — emitted in `_exec_wait` when each
   child task settles, and in `_cleanup_spawned` for halt/cancel:

   ```json
   {"type": "system", "subtype": "spawn_complete",
    "data": {"agent_name": "lint", "status": "completed",
             "duration_ms": 123456, "cost_usd": 0.042, "result": "..."}}
   ```

   `status` ∈ `completed` | `failed` | `cancelled`. `result` is the
   child walker's final output (from `_exec_wait`'s existing
   `result.output` handling), truncated to a bounded length (e.g. 2k
   chars) — the full child result already lands in the parent's
   variables via merge.

Child inner message forwarding is unchanged — `emit_forwarded` already
tags session identity.

### 2. Axi plumbing — transport + agenthub

4. **`axi/flowcoder_transport.py` — session-context registry.** Module-level
   `dict[uuid → {session, block_id, block_name}]` guarded by a lock. When
   unwrapping a `session_message` envelope, record the inner message's
   `uuid` → context; delete on the inner `result` message (stream end).
   System messages already carry `_session_context` in their raw `data` —
   no registry entry needed (and none created). Registry survives transport
   lifetime; entries for messages that never deliver a `result` (kill)
   expire via bounded size (e.g. cap at 1024, evict oldest).

5. **`packages/agenthub/agenthub/stream_types.py`** — new `SpawnStart` /
   `SpawnEnd` dataclasses:

   ```python
   @dataclass(slots=True)
   class SpawnStart:
       agent_name: str
       command_name: str = ""
       model: str = ""
       backend: str = ""
       parent_session: str = ""  # "main" or the spawning agent's name

   @dataclass(slots=True)
   class SpawnEnd:
       agent_name: str
       status: str = ""         # completed | failed | cancelled
       duration_ms: int = 0
       cost_usd: float = 0.0
   ```

   Add `session: str = ""` to the routed event dataclasses: `TextDelta`,
   `TextFlush`, `ThinkingStart`, `ThinkingEnd`, `ToolUseStart`,
   `ToolUseEnd`, `BlockStart`, `BlockComplete`, `FlowchartStart`,
   `FlowchartEnd`, `QueryResult`. `session == ""` means parent (existing
   events default to parent behavior; renderer treats `""`/`"main"` as the
   parent channel).

6. **`packages/agenthub/agenthub/streaming.py`**:

   - `_Ctx` becomes per-session: `dict[session_name, _Ctx]` (parent key
     `""`). Child text buffers, thinking state, and tool-input JSON never
     corrupt the parent's (and vice versa). Child entries are discarded on
     `spawn_complete` (the engine does not forward child `result`
     messages); the parent entry and any leftover child entries are
     discarded at stream end.
   - New DI parameter `session_context_fn: Callable[[str], dict | None]`
     (resolves an inner-message `uuid` to `{session, block_id,
     block_name}`), injected the same way as `set_session_id_fn`.
     `hub_wiring._stream_factory` supplies a closure over the transport
     registry.
   - Event tagging: for `stream_event`/`assistant`/`result` messages,
     resolve the registry by `msg.uuid` → `session`; for `system` messages,
     read `msg.data.get("_session_context", {}).get("session")`. Tag every
     yielded `StreamOutput` from that message with the session. Unknown
     session (registry miss, e.g. stub model) → `""` (parent).
   - New subtype handlers: `spawn_start` → `SpawnStart(agent_name=...,
     parent_session=...)`; `spawn_complete` → `SpawnEnd(...)`. Tagged with
     the *emitting* session (the parent of the spawn).
   - `flowchart_start`/`flowchart_complete`/`block_*` already route through
     `_handle_system_message`; tag with the system message's
     `_session_context.session`. Child block events therefore carry the
     child session with no engine-side change beyond Section 1.1.

### 3. Renderer + Discord threads

7. **`axi/discord_stream_renderer.py` — session routing.** Each render
   method routes on `event.session`: `""`/`"main"` → the agent's channel
   (today's behavior); child session → that session's thread (looked up in
   `discord_state(session).spawn_threads`); unknown/missing thread → fallback
   (Section 10).

   Thread state lives in `discord_state(session)` (module
   `axi/axi_types.py`):

   ```python
   spawn_threads: dict[str, int]  # child agent_name -> thread id
   ```

   Survives renderer recreation on reconnect; renderer re-fetches the
   `Thread` object from the bot on demand (thread id → object via
   `bot.get_channel`).

8. **`_on_spawn_start(event)`**:

   - Parent thread lookup: `parent_session == "main"` → thread is the
     agent's channel; else `discord_state(session).spawn_threads.get(parent_session)`.
   - Create thread on the agent's main channel
     (`TextChannel.create_thread(name=..., auto_archive_duration=60)`):
     - top-level: `name = event.agent_name`
     - nested: `name = f"{parent_thread.name}/{event.agent_name}"`
     (ancestry chain, `/`-joined).
   - Parent-channel status line: `▶ spawned **lint** → <thread-jump-link>`.
   - Into the thread: a spawned line (`▶ Spawned agent **lint**`) plus the
     command/model line (`running \`lint-fix\`` + model/backend when
     present).
   - Record `spawn_threads[event.agent_name] = thread.id`.
   - Errors (permissions, 404): log, post the spawned line into the parent
     channel with `[lint]` prefix, record nothing. Output continues via the
     fallback path.

9. **`_on_spawn_end(event)`** — post to the thread:

   ```
   **completed** in 123.5s | Cost: $0.042
   ```

   (mirroring the flowchart_complete summary format), then schedule a
   standalone archive task: `await asyncio.sleep(config.FC_THREAD_GRACE_SECS)`
   → re-fetch thread →
   `thread.archive()` → remove `spawn_threads[agent_name]`. The task is
   stored in `discord_state(session).pending_archives: dict[str, asyncio.Task]`;
   cancelled on agent shutdown. If `spawn_complete` arrives when the thread
   is already gone (404) → remove mapping, log, done. Archiving an
   already-archived thread is a no-op (discord.py tolerates it).
   `status == "cancelled"` → note it in the summary line.

10. **Fallback path.** Any render of a child-session event whose thread
    lookup fails (creation failed, thread deleted, registry miss) → send to
    the parent channel prefixed `[agent_name]` (single line per event, no
    live-edit), exactly the pre-thread behavior plus the label. Never drop,
    never raise.

11. **`_on_stream_end`** — any `spawn_threads` entry still open whose spawn
    never completed (hard kill, no `spawn_complete`) → post
    `**interrupted**` to the thread and archive immediately (no grace
    delay — the turn is over, the thread's purpose is done). Entries in
    `pending_archives` are left to their 5-minute task (archive-of-archived
    is a no-op).

12. **Config** — `config.py`: `FC_SPAWN_THREADS` env var, default `"1"`
    (enabled). `"0"`/`"false"` → Section 3 disabled entirely: no threads
    created, all child output stays in the parent channel as today.
    `FC_THREAD_GRACE_SECS` env var, default `"300"` — the archive grace
    delay after `spawn_complete` (shorter in e2e tests). Both mirror the
    existing `STREAMING_DISCORD` env-var convention (`config.py:248`).

### 4. Error handling

- Thread creation fails → fallback path, log, never crash the stream.
- Thread deleted mid-stream → next child render falls back; `spawn_end`
  removes the mapping; archive task no-ops on 404.
- Registry miss for an inner message → parent session (today's behavior).
- `spawn_complete` without `spawn_start` (engine restart mid-spawn, stale
  event) → log, no thread action.
- Nested spawn with missing parent thread (parent thread creation failed,
  grandchild spawns anyway) → grandchild thread is created top-level with
  its own name (creation never cascades failure upward); the ancestry
  prefix is applied only when the parent thread exists.
- Grace-delay archive task killed by shutdown → 60-minute
  `auto_archive_duration` backstop.
- Unknown engine event subtype `spawn_*` (version skew: engine newer than
  Axi) → falls through to generic `SystemNotification` handling as today;
  unknown-session routing still applies from any `_session_context`
  present. Older engine (no `session` on block events, no `spawn_*`) →
  Axi sees today's untagged events and renders exactly as today (threads
  simply never start). Feature is forward/backward compatible at the
  wire level.

### 5. Testing

Unit:

- Transport registry: uuid recorded on unwrap, deleted on result, capped
  eviction, lock safety.
- `streaming.py`: per-session `_Ctx` isolation (child text doesn't flush
  into parent buffer); session tagging from registry and from
  `_session_context`; `spawn_start`/`spawn_complete` → `SpawnStart`/
  `SpawnEnd` events; unknown session → `""`.
- Renderer: `_on_spawn_start` thread creation args (name, ancestry,
  auto_archive_duration), status lines, mapping record; `_on_spawn_end`
  summary + archive task scheduling (clock mocked); fallback path for
  creation failure / missing thread; `_on_stream_end` interrupts cleanup.
- Config: `FC_SPAWN_THREADS` parsing (default on, `0` off).
- Wire-compat: a `SystemNotification`-fallback test for unknown `spawn_*`
  subtype; a no-`session`-field event renders as parent.

Live/e2e (existing spawn e2e pattern — `procmux-stdio-orch-spawn-test`):

- A flowchart spawning one child: thread created on the agent's channel,
  child text/block events land in the thread, parent channel shows status
  line only, completion summary posted, thread archived after grace (grace
  shortened via env override for the test, e.g. `FC_THREAD_GRACE_SECS`).
- Two children in parallel: two threads, no cross-talk.
- Nested spawn (child flowchart spawns a grandchild): grandchild thread
  named `<child>/<grandchild>`.
- `FC_SPAWN_THREADS=0`: no threads, today's behavior.
- Hard kill of child: thread archived on stream end with `**interrupted**`.

### 6. Docs

- `docs/axi-runtime-configuration.md`: `FC_SPAWN_THREADS` behavior.
- `prompts/refs/flowcharts.md` (or the flowchart docs the engine maintains):
  spawn block output now streams to per-agent Discord threads.

## Out of scope

- Input blocks (`input_request`) inside a spawned child — `input_request`
  is not session-tagged (only block/flowchart/spawn events are), so a
  child's input prompt renders in the parent channel as today, and
  responding in-thread is not wired to `send_input_response`. Follow-up.
- Per-spawn thread naming overrides in the flowchart JSON (custom thread
  names per spawn block). Follow-up if requested.
- Thread history replay (rendering a past spawn's thread content on
  reconnect) — threads persist in Discord; re-render is not needed.
- Changing the parent channel's existing `/soul` quieting or verbose
  behavior — the child thread inherits the same quiet rules
  (`_FC_QUIET_COMMANDS`) as the parent channel.
