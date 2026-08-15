# Flowcharts Reference

Loaded by the `/soul` classifier when a message matches the "flowcharts" topic. Covers what flowcharts are, how to author them, the block-type catalog, common authoring patterns, and testing discipline.

If this file outgrows a single view, split into `flowcharts/block-types.md` + `flowcharts/patterns.md` with this file routing by sub-topic — same shape as the top-level `/soul` ref-doc routing.

## How It Works

Flowcharts are JSON-defined procedures that agents execute step by step. When you type `/command-name args`, the engine looks up `commands/<command-name>.json` and walks its blocks.

- Regular messages are wrapped in `/soul` (classify → route → hooks)
- `/command` messages are wrapped in `/soul-flow` (pre-task hook → execute command → post-task hook → completion check → record reporting → status update)
- `/soul` and `/soul-flow` themselves pass through unwrapped (they ARE the wrappers)
- `//raw message` bypasses all wrapping — sent directly to the agent

## Duration

Flowcharts run for any duration — single-shot procedures or infinite loops (mil/mill loop over deck cards indefinitely). Duration is not a reason to avoid flowcharts.

Flowcharts and agent-spawning are orthogonal, not alternatives. A spawned agent can run a flowchart via `axi_spawn_agent`'s `command` and `command_args` parameters — use this for long-running flowchart work that needs its own channel/context.

## File Structure

A flowchart command lives at `commands/<name>.json` (core) or `extensions/<ext>/commands/<name>.json` (extension; symlinked into `commands/` at startup). Top-level keys:

- `id` — unique identifier (UUID-like)
- `name` — short name used as `/name` to invoke
- `description` — one-line explanation
- `arguments` — optional list of argument names; substituted as `$1`, `$2`, … literally throughout block fields/prompts before execution
- `flowchart` — nested object containing:
  - `blocks` — **DICT** keyed by block ID (not a list); each value is a block object
  - `connections` — list of edge objects: `{source_block_id, target_block_id, source_port, target_port, is_true_path, condition, label}`
  - `start_block_id` — ID of the `start` block to begin execution
- `metadata` — `{created, modified, version, author, tags}`

Every block carries the base fields `id`, `type`, `name`, `position {x, y}` plus type-specific fields below.

## Block Types

Canonical source: `BlockType` enum + class definitions in `/home/pride/coding-projects/flowcoder/packages/flowcoder-flowchart/src/flowcoder_flowchart/blocks.py`. Mirror below — re-check the source if you suspect drift.

| Type | Type-specific fields | Purpose |
|------|----------------------|---------|
| `start` | — | Entry point. Exactly one per flowchart. |
| `end` | — | Terminate flowchart successfully. |
| `prompt` | `prompt` (str), `output_variable` (opt), `output_schema` (opt JSON schema) | Send prompt to Claude subprocess. If `output_schema` is set, the response is parsed/validated and bound to `output_variable`. |
| `branch` | `condition` (str) | Conditional fork. Outgoing connections carry `is_true_path: true/false`; the engine picks the matching path based on `condition` evaluated against current variables. |
| `variable` | `variable_name`, `variable_value`, `variable_type` (`string`/`number`/`boolean`/`json`) | Set a variable in the session. |
| `bash` | `command` (str); opt `capture_output`, `output_variable`, `output_type`, `continue_on_error`, `working_directory`, `exit_code_variable` | Execute a shell command. |
| `command` | `command_name` (str); opt `arguments`, `inherit_variables`, `merge_output` | Compose — run another flowchart inline. `arguments` accepts the same `$1`/`$2`/`{{var}}` substitution. |
| `refresh` | opt `target_session` | Kill the Claude subprocess and restart with a fresh context window. Flowchart variables persist; conversation history does not. |
| `spawn` | `agent_name`, `command_name`; opt `arguments`, `inherit_variables`, `exit_code_variable`, `config_file`, `model`, `backend`, `provider` | Spawn an axi sub-agent asynchronously running a named command. `provider` (a name from providers.json) resolves the block's model to that provider's env; omit it to inherit the parent agent's provider. Parent flowchart proceeds immediately. |
| `wait` | `wait_for` (list of agent names); opt `timeout_seconds` | Block the parent flowchart until all named spawned agents complete. |
| `exit` | `exit_code` (int), `exit_message` (str) | Terminate flowchart with explicit (often non-zero) exit. |
| `input` | opt `output_variable` | Pause flowchart, accept user input via the agent's Discord channel, bind to variable. |

## Variable Substitution

Two distinct mechanisms — don't mix them up:

- **`$1`, `$2`, …** — flowchart arguments (positional). Declared in the top-level `arguments` array of the JSON. Substituted **literally** throughout the JSON BEFORE execution. Lets a wrapper flowchart pass values into block fields like `command_name: "$1"` or `arguments: "$2"`.
- **`{{variable_name}}`** — runtime variable interpolation inside `prompt`, `bash`, and other string fields. Variables come from prior blocks' `output_variable` outputs, `variable` blocks, or `output_schema`-parsed prompt results. Example: `Current offset: {{next_offset}} | Target: {{target}}`.

`branch` `condition` fields are evaluated against current variables (JSONPath/template style — not full Python). A bare variable name like `has_more` is truthy-tested.

## Authoring Heuristics

### Refresh blocks for very long loops

Loops with >~10–20 iterations or >~30-minute wall-clock should place a `refresh` block inside the loop body. Without refresh, every iteration accumulates messages in the Claude subprocess's conversation → context window fills → autocompact triggers → context loss + slowdown.

Place `refresh` **after** per-iteration state has been persisted (checkpoint file, DB write, etc.). Refresh discards conversation history, so any unsaved iteration-local state is lost. Flowchart-level variables (set by `variable` / `output_variable`) survive.

### Spawn + wait for parallelizable operations

If iteration steps are independent — no shared mutable state, no ordering dependency — use `spawn` blocks to run N copies in parallel, then a `wait` block to join.

Pattern (50 items in 5 parallel batches of 10):

```
... compute batch list, store as variable ...
[SPAWN agent_name=batch-1 command_name=process-batch arguments="1 10"]
[SPAWN agent_name=batch-2 command_name=process-batch arguments="11 20"]
[SPAWN agent_name=batch-3 command_name=process-batch arguments="21 30"]
[SPAWN agent_name=batch-4 command_name=process-batch arguments="31 40"]
[SPAWN agent_name=batch-5 command_name=process-batch arguments="41 50"]
[WAIT wait_for=[batch-1, batch-2, batch-3, batch-4, batch-5]]
... continue with aggregated state ...
```

`spawn` is asynchronous — the parent flowchart proceeds to subsequent blocks immediately. `wait` blocks the parent until every named agent finishes. Set `timeout_seconds` if you need a ceiling.

### Smoke-and-notify for testing

Before deploying a long-running flowchart, smoke-test it at minimal scale and have it notify you when done. The generic wrapper `commands/smoke-and-notify.json` already implements this:

1. Spawn a sub-agent via `axi_spawn_agent` with `command='smoke-and-notify'` and `command_args='<target_command> <small_args> <your_agent_name>'`.
2. `smoke-and-notify` runs the target flowchart via a `command` block (inner composition).
3. Its final `prompt` block uses `axi_send_message` to send a summary back to the spawning agent — including final state, iteration count, error count, recommended primary-source verifications.

This isolates the test from your active session and gives you a structured report to verify before scaling. **Don't skip** — design correctness alone is not evidence the run will execute correctly. Watch one full iteration end-to-end and inspect outputs (DB rows, files, agent behavior) before raising the target.

### Block-prompt authoring discipline

When writing a `prompt` block, embed operational rules **inside the prompt** — don't rely on the agent's outer system prompt to carry rules across iterations (especially across `refresh` boundaries, which throw away conversation history).

After drafting a block prompt, re-read end-to-end and audit:
- (a) Constraints stated inline (a HARD RULES preamble works well)
- (b) No contradictions between steps
- (c) No implicit dependency on context that may decay across `refresh`

If a rule emerges later (e.g. "don't write outside cwd"), revise the relevant **block prompt** — not the spawn prompt — before re-deploying.

## Core Commands

- **`/soul`** — Core message handler. Classifies incoming messages as tasks or conversation, runs extension hooks (pre_task, execute, post_task, post_respond), and manages the full task lifecycle including record reporting and status updates.
- **`/soul-flow`** — Task lifecycle wrapper. Wraps any flowchart command with pre/post hooks, completion checks, record reporting, and status updates. You rarely invoke this directly — it's applied automatically when you run any `/command`.

## Extension Commands

Extensions can add their own flowchart commands in `extensions/<name>/commands/`. These are symlinked into `commands/` at startup so they're discoverable alongside core commands.

Notable extension commands (when the relevant extension is loaded):
- **`/mill`** — Auto-execute MinFlow deck cards, stopping when human approval is needed (e.g. plan review, ambiguous decisions).
- **`/mil`** — Auto-execute MinFlow deck cards with minimal human approval — only stops for very complex/ambiguous plans or critical research findings.

## Listing Available Commands

Use the `/flowchart-list` Discord slash command to see all available flowchart commands and their descriptions.
