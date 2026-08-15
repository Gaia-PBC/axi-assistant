# Provider and Model Registry for Axi

Date: 2026-08-15. Status: approved design, pending implementation plan.

## Problem

Axi today supports exactly two model buckets: native Claude aliases (`opus`,
`sonnet`, `haiku`, or any free-form ID routed to the real Anthropic API) and
`gpt-*` models routed through the local ChatGPT proxy. The provider is
implicit in the model name, the endpoint is hardcoded, and there is no way to
select a provider/model pair for a spawned agent or a flowchart subagent.

We need:

- Named providers, including multiple instances of the same backend (e.g. two
  ollama servers).
- Automatic routing: callers specify only a model; the provider is inferred.
  An explicit provider override is available when inference is ambiguous or
  the caller wants a specific endpoint.
- Model discovery via a tool call: anthropic models hardcoded, ollama via
  `/api/tags` + `/api/show`, vLLM via `/v1/models`.
- Per-model context windows so Claude Code's `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
  is set correctly for local models.
- The ability to set whatever env vars a provider needs
  (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`,
  `ANTHROPIC_MODEL`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, tier-mapping vars).
- Flowchart subagents (spawn blocks) able to select an exact provider/model
  pair.

## Background facts (verified)

- Ollama natively serves the Anthropic Messages API at
  `{base}/v1/messages`. Claude Code connects with
  `ANTHROPIC_BASE_URL=http://localhost:11434` and
  `ANTHROPIC_AUTH_TOKEN=ollama` (Bearer auth; `ANTHROPIC_API_KEY` is not
  used). Source: ollama docs, "Anthropic compatibility".
- vLLM natively serves the Anthropic Messages API (README: "OpenAI-compatible
  API server, plus Anthropic Messages API and gRPC support"). The local
  deployment on this box uses a shim at `:8199` because that vLLM build
  rejects `role:"system"` messages inside the messages array; the shim is an
  operator concern — a provider entry simply points at whatever endpoint
  speaks the protocol.
- Ollama discovery: `GET /api/tags` returns model names plus family/params/
  quant, but no context length. `POST /api/show` per model returns
  `model_info` containing `<family>.context_length` (e.g.
  `gemma4.context_length: 131072`). The `parameters` `num_ctx` line is the
  runtime default, not the max, and is ignored.
- vLLM discovery: `GET /v1/models` returns model ids and `max_model_len`.
- Claude Code context window env var: `CLAUDE_CODE_MAX_CONTEXT_TOKENS`.
  Tier-mapping env vars: `ANTHROPIC_DEFAULT_OPUS_MODEL`,
  `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
  `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`. All already
  used in the disabled vLLM block of this repo's `.env`.
- Current routing: `config.get_model_runtime(model)` returns
  `(claude_model_arg, env)`; `gpt-*`/`o1`/`o3`/`o4`/`o5` prefixes route to
  the ChatGPT proxy env, everything else is native Claude. `session.model`
  holds a per-agent override; `agents._save_agent_config` persists
  `{"model": ...}` per agent across restarts.
- Flowchart spawn blocks do NOT go through `axi_spawn_agent`. The
  flowcoder-engine (`flowcoder-core`, git-pinned at `1f2f2b4`) spawns child
  Claude subprocesses directly (`walker._exec_spawn` →
  `session.with_model(block.model).clone(name)`), and children inherit the
  parent engine's env (`session.py` `_clean_env()`). There is no per-child
  env mechanism; the documented `config_file` spawn-block field is
  unimplemented in the engine.

## Design

### 1. Provider registry — `user-data/providers.json`

A JSON file in the same directory as the existing `mcp_servers.json`
precedent. Loaded at startup and on demand; validated; bad entries are
logged and skipped, never fatal.

```json
{
  "providers": [
    { "name": "ollama-local", "type": "ollama", "base_url": "http://localhost:11434" },
    { "name": "ollama-2",     "type": "ollama", "base_url": "http://192.168.1.5:11434" },
    { "name": "vllm",         "type": "vllm",   "base_url": "http://localhost:8199", "api_key": "vllm-local-no-auth" },
    { "name": "gateway",      "type": "anthropic", "base_url": "https://zenmux.ai/api/anthropic", "api_key": "…", "models": ["claude-sonnet-4-5", "…"] }
  ]
}
```

Fields:

- `name` — unique identifier used in `provider:model` syntax and the
  `provider` argument. Required.
- `type` — `anthropic` | `ollama` | `vllm`. Required.
- `base_url` — endpoint. Required for `ollama`/`vllm`; optional for
  `anthropic` (omitted = real Anthropic API with OAuth).
- `api_key` — optional. When set, determines the auth env var: `ollama`
  sets `ANTHROPIC_AUTH_TOKEN` (Bearer); `anthropic`/`vllm` set
  `ANTHROPIC_API_KEY` (x-api-key). Omitted → no auth env set (local
  no-auth servers).
- `models` — optional seed list for Anthropic-protocol gateways that expose
  no model-list endpoint. Used by discovery; never an allowlist.
- `context_window` — optional per-provider override, used only when
  discovery cannot determine a model's context window.

A built-in `anthropic` provider is hardcoded in code and always present; it
is not in the JSON. Its model list is hardcoded (haiku/sonnet/opus aliases
plus current IDs such as `claude-opus-4-8`). The hardcoded list is used only
for discovery/autocomplete; validation stays free-form, so newer or
unlisted model IDs can still be typed directly.

Missing file → only the built-in `anthropic` provider exists.

### 2. Model discovery — `axi/providers.py` + `axi_list_models` tool

New module `axi/providers.py`:

- Registry load/validate.
- Per-type fetchers:
  - `anthropic`: hardcoded table.
  - `ollama`: `GET {base}/api/tags` for names, then one parallel
    `POST {base}/api/show` per model; extract `<family>.context_length`
    from `model_info` (prefer the key matching `details.family`, fall back
    to any `<arch>.context_length`, else `null`).
  - `vllm`: `GET {base}/v1/models` → ids + `max_model_len`.
- TTL cache: module-level, ~60s, keyed by provider. No disk, no DB.
  Spawn-time routing reuses the same cache, so a spawn adds at most one
  `/api/show` for the chosen model when cold.

New MCP tool `axi_list_models` (in `axi/tools.py`, same utils MCP server as
`axi_spawn_agent`, so every agent can call it). Optional `provider` filter
argument. Returns:

```json
{"providers": [
  {"name": "anthropic", "models": [{"id": "claude-opus-4-8", "context_window": 200000, "reasoning": true}, ...]},
  {"name": "ollama-local", "models": [{"id": "qwen3-coder:30b", "context_window": 32768, "reasoning": false}, ...]},
  {"name": "vllm", "models": [{"id": "nvidia/Qwen3.6-35B-A3B-NVFP4", "context_window": 262144, "reasoning": true}]}
]}
```

Discovery is advisory. Routing never blocks on it. A provider whose fetch
fails reports `"error": "..."` in the tool output; other providers still
list. A per-model `/api/show` failure reports `context_window: null` for
that model.

### 3. Routing and env resolution — `config.resolve_runtime(model, provider=None)`

Replaces `get_model_runtime` (which it wraps for backward compatibility).
Returns `(model_arg, env, provider_name)`.

Selection semantics:

- `provider` given → exact pair. Look up the entry; `model` passes through
  as-is (validated against the existing model-name regex; not required to
  appear in discovery). Unknown provider name → error at the call site.
- `provider` omitted → automatic routing, deterministic precedence:
  1. `gpt-*` / `o1` / `o3` / `o4` / `o5` prefixes → legacy ChatGPT proxy env
     (unchanged behavior; this is the built-in "chatgpt-proxy" provider).
  2. Model matches a known anthropic alias/ID → native anthropic (no env).
  3. Model discovered on exactly one non-anthropic provider → that provider.
  4. Model discovered on multiple providers → error listing candidates; the
     caller must disambiguate with `provider:model`. Silent wrong routing is
     worse than a prompt to be explicit.
  5. Model discovered nowhere → native anthropic (today's free-form
     fallback).

Env assembly per provider type (merged into the agent env):

- `anthropic` built-in: nothing (native API, OAuth).
- `anthropic` custom gateway / `vllm` / `ollama`:
  - `ANTHROPIC_BASE_URL` = entry `base_url`.
  - `ANTHROPIC_MODEL` = model id.
  - Auth: `ollama` → `ANTHROPIC_AUTH_TOKEN`; others → `ANTHROPIC_API_KEY`;
    only when the entry has `api_key`.
  - `CLAUDE_CODE_MAX_CONTEXT_TOKENS` = discovered or overridden context
    window.
  - Tier mapping (`ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL`,
    `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`) → the
    selected model id, only when `provider` was explicit. Auto-routed bare
    models do not guess which tier internal calls should use.

Hermetic env strip: `hub_wiring._make_agent_options` currently pops only the
three `ANTHROPIC_*` vars. It now pops the full managed set
(`ANTHROPIC_*`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS`,
`CLAUDE_CODE_SUBAGENT_MODEL`) before re-adding resolved env, so one
provider's `.env` leftovers can never leak into a session routed elsewhere.

Persistence: `session.provider` added alongside `session.model`;
`agents._save_agent_config` / `_load_agent_config` gain a `provider` slot.
On resume with an unknown/deleted provider name → log and fall back to
auto-routing; never crash a session.

Entry points:

- `/model` accepts `provider:model` (split at the **first** colon — ollama
  model ids themselves contain colons, e.g. `qwen3-coder:30b`; a bare model
  string is untouched for backward compatibility).
- `/spawn`, `axi_spawn_agent`, and the HTTP `/v1/spawn` endpoint gain an
  optional `provider` argument; `axi_spawn_agent`'s JSON schema gains
  `provider`.
- Flowchart spawn blocks gain an optional `provider` field (see Section 4).

### 4. Flowchart subagent provider selection

Flowchart spawn blocks do not go through `axi_spawn_agent`; the engine
spawns child Claude subprocesses that inherit the parent engine's env. To
let a flowchart child run on a different provider, extend the engine:

- **flowcoder-core PR**: add an optional `env: {VAR: value}` map to the
  spawn block schema; `ClaudeSession` gains an env override merged into
  `_clean_env()` at child spawn; `with_model`/`clone` plumb it through.
- **Axi side — transform pass**: the engine parses flowchart JSON itself
  from the search paths, so Axi cannot inject a resolved env into a block
  the engine already parsed. Instead, at engine launch (`get_search_paths` /
  `build_engine_cmd`), Axi scans the command JSONs in the search paths; for
  every spawn block carrying a `provider` field, it resolves
  `provider` + `model` through `resolve_runtime`, rewrites the block to
  carry the resulting `env` map (dropping `provider`), and writes the
  transformed command to a runtime shadow dir (e.g.
  `user-data/flowcharts-resolved/`). The shadow dir is prepended to the
  search paths; command resolution is first-match-wins, so the transformed
  copy shadows the original. Blocks without `provider` pass through
  untouched. Unknown `provider` in a flowchart → the transform logs and
  leaves the block as-is (the engine ignores the unknown field), so a bad
  flowchart degrades to parent-env behavior rather than crashing the engine.
- Bump the `flowcoder-engine` / `flowcoder-flowchart` git pin in
  `pyproject.toml` to the PR's commit.

Example:

```json
{ "type": "spawn", "model": "qwen3-coder:30b", "provider": "ollama-local", "command_name": "soul-lite-do" }
```

The existing `model`-only spawn blocks (e.g. `soul-lite.json` spawning
`claude-opus-4-8`) keep working unchanged.

### 5. Error handling

- Registry: missing file → anthropic only; bad entry → logged + skipped;
  duplicate name → last wins + warning.
- Discovery: provider fetch fails → `"error"` in tool output, others
  continue; per-model `/api/show` fails → `context_window: null`.
- Routing: unknown explicit provider → error at call site; ambiguous model
  (2+ providers) → error listing candidates; explicit provider + dead server
  → env still builds (no network at spawn), agent fails at first request
  with the server's own error — same as today's behavior with a down proxy.
- Auto-route fallback: model found nowhere → anthropic, exactly today's
  free-form behavior. Documented, not silent-new. A provider whose fetch
  failed is treated as "not discovered" for auto-routing purposes — a model
  that would have matched a down provider falls through to anthropic and
  fails there with the API's own "unknown model" error, which is
  diagnosable. Explicit `provider` is the escape hatch.

### 6. Testing

Unit:

- Registry load/validation (missing file, bad entry, duplicate name).
- Resolver precedence: gpt-* → proxy; alias → anthropic; single-provider
  match; multi-provider ambiguity; unknown provider; free-form fallback.
- Env assembly per type: ollama → `ANTHROPIC_AUTH_TOKEN`; vLLM/gateway →
  `ANTHROPIC_API_KEY`; `CLAUDE_CODE_MAX_CONTEXT_TOKENS`; tier mapping only
  when provider explicit.
- Hermetic env strip.
- Config persistence round-trip with the provider slot.
- Discovery parsing from fixture JSON (ollama tags+show, vLLM models) with
  httpx mocked; TTL cache hit/miss/expiry.
- Axi-side flowchart: provider → env dict lands in the block config passed
  to the engine (at the `build_engine_*` boundary).

Engine PR (flowcoder-core): env merge in `_clean_env`, `with_model`/`clone`
env plumbing, spawn-block `env` schema.

Live smoke: `axi_list_models` against a real ollama/vLLM if one is up;
otherwise mocked.

### 7. Docs

- `docs/axi-runtime-configuration.md`: providers.json schema, routing rules,
  env vars.
- `prompts/refs/flowcharts.md`: spawn block `provider`/`env` fields.
- `.env.template`: no new vars (providers.json replaces env for this).

## Out of scope

- omp-style SQLite model cache with authoritative/updated_at semantics —
  rejected; a 60s in-memory TTL is sufficient for local/cloud endpoints.
- Named pair profiles ("fast", "big") — rejected; ad-hoc `provider:model`
  covers the need.
- Changing how the ChatGPT proxy itself is configured.
- Codex backend sessions in the flowcoder engine.
