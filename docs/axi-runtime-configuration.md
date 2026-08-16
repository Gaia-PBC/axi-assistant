# Axi Runtime Configuration

Axi separates two choices:

- `AXI_HARNESS`: how Axi runs each agent.
- `AXI_MODEL`: which model the harness should use.

This keeps the setup to one operational choice and one model choice. Users
should not need to set Claude Code's low-level `ANTHROPIC_*` variables for the
standard ChatGPT proxy setup.

## Harness

Set `AXI_HARNESS` in `.env`:

```env
AXI_HARNESS=claude_code
```

Supported values:

| Value | Behavior |
|---|---|
| `claude_code` | Run plain Claude Code sessions. Use this for Claude Code pointed at Claude models or the ChatGPT proxy. |
| `flowcoder` | Run agents through the FlowCoder engine and flowchart layer. |

`claude-code` is accepted as an alias for `claude_code`.

The old `FLOWCODER_ENABLED=0/1` flag is still read for backwards compatibility
when `AXI_HARNESS` is not set, but new configs should use `AXI_HARNESS`.

## Model

Set `AXI_MODEL` in `.env`:

```env
AXI_MODEL=gpt-5.4
```

Supported forms:

| Model value | Behavior |
|---|---|
| `opus`, `sonnet`, `haiku` | Passed to Claude Code as the native Claude model selector. |
| `gpt-*` such as `gpt-5.4` | Routed through the local ChatGPT Anthropic proxy. |

The legacy value `codex` is accepted as an alias for `gpt-5.4`, but new configs
should set the actual model name.

## Providers

Axi routes models to named providers. The built-in `anthropic` provider is
always available (native API, OAuth). Additional providers live in
`user-data/providers.json`:

```json
{
  "providers": [
    { "name": "ollama-local", "type": "ollama", "base_url": "http://localhost:11434" },
    { "name": "vllm", "type": "vllm", "base_url": "http://localhost:8199", "api_key": "vllm-local-no-auth" }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | Unique id used as `provider:model` (e.g. `ollama-local:qwen3-coder:30b`) |
| `type` | `anthropic` \| `ollama` \| `vllm` |
| `base_url` | Endpoint. Required for `ollama`/`vllm`; optional for `anthropic` (omitted = real Anthropic API) |
| `api_key` | Optional. Ollama uses `ANTHROPIC_AUTH_TOKEN` (Bearer); others use `ANTHROPIC_API_KEY` |
| `models` | Optional seed list for gateways with no model-list endpoint |
| `context_window` | Optional override when discovery can't determine it |

The endpoint must accept Claude Code's request shape. On this box, vLLM's
raw `:8000` rejects `role:"system"` messages (vLLM < 0.24.0); point the
entry at the shim `:8199` or upgrade vLLM to >= 0.24.0.

### Routing

Callers specify a model; the provider is inferred:

1. `gpt-*`/`o1`/`o3`/`o4`/`o5` → ChatGPT proxy
2. Claude aliases/ids → native anthropic
3. Model on exactly one other provider → that provider
4. Model on multiple providers → error; use `provider:model`
5. No match → native anthropic (free-form ids keep working)

An explicit provider sets `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, auth,
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` (from discovery), and maps every Claude
tier to the model. Use `/model ollama-local:qwen3-coder:30b` or the
`provider` argument on `/spawn` / `axi_spawn_agent`.

### Discovery

Any agent can call the `axi_list_models` tool to list models per provider
with context windows. Anthropic models are hardcoded; ollama uses
`/api/tags` + `/api/show`; vLLM uses `/v1/models`.

## FlowCoder Wrapper

When `AXI_HARNESS=flowcoder`, Axi can automatically route normal messages
through a FlowCoder command before Claude sees them:

```env
AXI_FC_WRAP=soul
```

Supported values:

| Value | Behavior |
|---|---|
| unset | Default legacy behavior: route through `soul`. |
| `prompt` | Use the bundled pass-through wrapper. Normal messages run as the model prompt with no extra lifecycle steps. |
| `off`, `none`, `0`, `false` | Disable automatic wrapping. Messages go directly to the FlowCoder engine/Claude session. |
| `soul` | Use the legacy `/soul` wrapper for normal messages and `/soul-flow` wrapper for other slash commands. |
| any command name | Route normal messages as `/<command-name> <message>`. Explicit slash commands are not wrapped. |

Explicit flowchart commands, such as `/soul` or `/flowchart`, still work when
automatic wrapping is disabled.

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

## Effort

Axi passes `AXI_EFFORT` to Claude Code. Supported values are:

```env
AXI_EFFORT=max
```

Valid values are `low`, `medium`, `high`, and `max`. For ChatGPT/Codex models,
the proxy shim maps Claude Code's `max` effort to Codex's `xhigh` reasoning
level. The legacy spelling `xhigh` is accepted in `.env` and normalized to
Claude Code's `max`.

## ChatGPT Proxy Defaults

When `AXI_MODEL` starts with `gpt-`, Axi automatically runs Claude Code with:

```env
ANTHROPIC_BASE_URL=http://127.0.0.1:3000
ANTHROPIC_API_KEY=test
ANTHROPIC_MODEL=<AXI_MODEL>
```

These are injected into the Claude Code process. They do not need to be in
`.env` for normal use.

If the proxy is listening somewhere else, set:

```env
AXI_CHATGPT_PROXY_BASE_URL=http://127.0.0.1:3000
AXI_CHATGPT_PROXY_API_KEY=test
```

## Examples

Plain Claude Code on Claude:

```env
AXI_HARNESS=claude_code
AXI_MODEL=opus
```

Claude Code on ChatGPT 5.4 through the proxy:

```env
AXI_HARNESS=claude_code
AXI_MODEL=gpt-5.4
```

FlowCoder on Claude:

```env
AXI_HARNESS=flowcoder
AXI_MODEL=opus
```

FlowCoder on ChatGPT 5.4 through the proxy:

```env
AXI_HARNESS=flowcoder
AXI_MODEL=gpt-5.4
AXI_FC_WRAP=prompt
```

FlowCoder without automatic wrapper flowcharts:

```env
AXI_HARNESS=flowcoder
AXI_FC_WRAP=off
```

Restart Axi after changing these values.
