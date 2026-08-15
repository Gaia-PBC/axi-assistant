# Provider and Model Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Axi agents, spawns, and flowchart subagents select exact provider/model pairs (anthropic, ollama, vLLM) with automatic routing, per-model context windows, and tool-callable model discovery.

**Architecture:** A `user-data/providers.json` registry (named entries, built-in hardcoded `anthropic`) feeds a new `axi/providers.py` discovery module (per-type fetchers + 60s TTL cache). `config.resolve_runtime(model, provider=None)` replaces `get_model_runtime` with deterministic auto-routing and per-provider env assembly. `session.provider` persists alongside `session.model`. Flowchart spawn blocks get a `provider` field resolved to an `env` map by an Axi transform pass; the flowcoder engine gains spawn-block `env` support via a small upstream PR.

**Tech Stack:** Python 3.12, httpx (already a dependency), pydantic (flowcoder-flowchart), pytest + unittest.mock, flowcoder-core (git-pinned).

**Spec:** `docs/superpowers/specs/2026-08-15-provider-model-registry-design.md`

## Global Constraints

- Provider types: `anthropic` | `ollama` | `vllm` only.
- Ollama auth env var is `ANTHROPIC_AUTH_TOKEN` (Bearer); all other providers use `ANTHROPIC_API_KEY` (x-api-key).
- `provider:model` splits at the **first** colon, and only when the prefix is a known provider name (ollama model ids contain colons, e.g. `qwen3-coder:30b`).
- Tier-mapping env vars (`ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`) are set **only** when `provider` was explicit.
- Managed env vars stripped from agent env before resolution: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CLAUDE_CODE_SUBAGENT_MODEL`.
- Auto-route precedence: (1) `gpt-*`/`o1`/`o3`/`o4`/`o5` → ChatGPT proxy; (2) anthropic alias/ID → native; (3) exactly one non-anthropic provider match → that provider; (4) multiple matches → error listing candidates; (5) no match → native anthropic fallback.
- Unknown provider at resume → log + fall back to auto-routing, never crash.
- Discovery is advisory; routing never blocks on it. Failed provider fetch = "not discovered" for auto-routing.
- Registry errors are non-fatal: bad entry logged + skipped, duplicate name last-wins + warning, missing file → anthropic only.
- Flowchart transform: shadow dir `user-data/flowcharts-resolved/` prepended to search paths; only files containing `provider` spawn blocks are written; unknown provider in a flowchart → leave block as-is (log), never crash the engine.

---

### Task 1: `axi/providers.py` — registry, discovery, TTL cache

**Files:**
- Create: `axi/providers.py`
- Test: `tests/unit/test_providers.py`

**Interfaces:**
- Consumes: `axi.config.AXI_USER_DATA` (path constant), httpx.
- Produces:
  - `PROVIDERS_PATH: str` — `os.path.join(config.AXI_USER_DATA, "providers.json")`
  - `ANTHROPIC_MODELS: list[dict[str, Any]]` — hardcoded table, each `{"id": str, "context_window": int, "reasoning": bool}`
  - `class Provider` (dataclass): `name: str`, `type: str`, `base_url: str | None`, `api_key: str | None`, `models: list[str]`, `context_window: int | None`
  - `load_providers() -> dict[str, Provider]` — includes built-in `anthropic`; never raises
  - `get_provider(name: str) -> Provider | None`
  - `list_models(provider: str | None = None) -> dict[str, list[dict[str, Any]]]` — `{provider_name: [{"id", "context_window", "reasoning"}]}`; failed providers get `{"error": "..."}` in place of the list
  - `get_model_context_window(provider: str, model: str) -> int | None`
  - `find_providers_for_model(model: str) -> list[str]` — non-anthropic providers whose discovered/seed models contain `model`
  - `_CACHE_TTL_SECONDS = 60`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_providers.py
"""Unit tests for the provider registry and model discovery."""
import json
from unittest.mock import patch

import pytest

from axi import providers


class TestRegistry:
    def test_missing_file_yields_only_anthropic(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(providers, "PROVIDERS_PATH", str(tmp_path / "nope.json"))
        reg = providers.load_providers()
        assert set(reg) == {"anthropic"}
        assert reg["anthropic"].type == "anthropic"
        assert reg["anthropic"].base_url is None

    def test_bad_entry_skipped_duplicate_last_wins(self, tmp_path, monkeypatch) -> None:
        p = tmp_path / "providers.json"
        p.write_text(json.dumps({"providers": [
            {"name": "bad", "type": "nope", "base_url": "x"},
            {"name": "dup", "type": "ollama", "base_url": "http://a:1"},
            {"name": "dup", "type": "ollama", "base_url": "http://b:2"},
        ]}))
        monkeypatch.setattr(providers, "PROVIDERS_PATH", str(p))
        reg = providers.load_providers()
        assert "bad" not in reg
        assert reg["dup"].base_url == "http://b:2"

    def test_ollama_requires_base_url(self, tmp_path, monkeypatch) -> None:
        p = tmp_path / "providers.json"
        p.write_text(json.dumps({"providers": [
            {"name": "no-url", "type": "ollama"},
        ]}))
        monkeypatch.setattr(providers, "PROVIDERS_PATH", str(p))
        assert "no-url" not in providers.load_providers()


class TestDiscovery:
    def test_anthropic_hardcoded(self) -> None:
        models = providers.list_models("anthropic")
        assert any(m["id"] == "claude-opus-4-8" for m in models["anthropic"])
        assert all(m["context_window"] > 0 for m in models["anthropic"])

    def test_ollama_tags_plus_show(self) -> None:
        tags = {"models": [{"name": "qwen3-coder:30b", "details": {"family": "qwen3"}}]}
        show = {"model_info": {"qwen3.context_length": 32768}}
        with (
            patch("axi.providers._http_get", return_value=tags) as get,
            patch("axi.providers._http_post", return_value=show) as post,
        ):
            models = providers.list_models("ollama-local")
        assert models["ollama-local"] == [
            {"id": "qwen3-coder:30b", "context_window": 32768, "reasoning": False}
        ]
        get.assert_called_once_with("http://localhost:11434/api/tags")
        post.assert_called_once_with("http://localhost:11434/api/show", {"model": "qwen3-coder:30b"})

    def test_ollama_show_failure_degrades_to_null(self) -> None:
        tags = {"models": [{"name": "m1", "details": {"family": "f"}}]}
        with (
            patch("axi.providers._http_get", return_value=tags),
            patch("axi.providers._http_post", side_effect=RuntimeError("down")),
        ):
            models = providers.list_models("ollama-local")
        assert models["ollama-local"] == [{"id": "m1", "context_window": None, "reasoning": False}]

    def test_vllm_models_endpoint(self) -> None:
        resp = {"data": [{"id": "nvidia/Qwen3.6-35B-A3B-NVFP4", "max_model_len": 262144}]}
        with patch("axi.providers._http_get", return_value=resp):
            models = providers.list_models("vllm")
        assert models["vllm"] == [
            {"id": "nvidia/Qwen3.6-35B-A3B-NVFP4", "context_window": 262144, "reasoning": True}
        ]

    def test_provider_fetch_failure_reports_error(self) -> None:
        with patch("axi.providers._http_get", side_effect=RuntimeError("conn refused")):
            models = providers.list_models("vllm")
        assert "error" in models["vllm"]

    def test_seed_models_used_when_no_fetch(self) -> None:
        with (
            patch("axi.providers._http_get", side_effect=RuntimeError("down")),
            patch("axi.providers.load_providers", return_value={
                "gateway": providers.Provider(
                    name="gateway", type="anthropic", base_url="http://g:1",
                    api_key=None, models=["claude-sonnet-4-5"], context_window=None,
                ),
            }),
        ):
            models = providers.list_models("gateway")
        assert models["gateway"] == [{"id": "claude-sonnet-4-5", "context_window": None, "reasoning": False}]


class TestCache:
    def test_ttl_cache_hit_skips_fetch(self) -> None:
        tags = {"models": [{"name": "m1", "details": {"family": "f"}}]}
        show = {"model_info": {"f.context_length": 4096}}
        with (
            patch("axi.providers._http_get", return_value=tags) as get,
            patch("axi.providers._http_post", return_value=show) as post,
        ):
            providers.list_models("ollama-local")
            providers.list_models("ollama-local")
        assert get.call_count == 1
        assert post.call_count == 1

    def test_ttl_cache_expires(self) -> None:
        tags = {"models": [{"name": "m1", "details": {"family": "f"}}]}
        show = {"model_info": {"f.context_length": 4096}}
        with (
            patch("axi.providers._http_get", return_value=tags) as get,
            patch("axi.providers._http_post", return_value=show),
            patch("axi.providers._CACHE_TTL_SECONDS", 0),
        ):
            providers.list_models("ollama-local")
            providers.list_models("ollama-local")
        assert get.call_count == 2


class TestRoutingHelpers:
    def test_find_providers_for_model(self) -> None:
        with (
            patch("axi.providers.list_models", return_value={
                "ollama-local": [{"id": "qwen3-coder:30b", "context_window": 32768, "reasoning": False}],
                "vllm": [{"id": "nvidia/Qwen3.6-35B-A3B-NVFP4", "context_window": 262144, "reasoning": True}],
            }),
        ):
            assert providers.find_providers_for_model("qwen3-coder:30b") == ["ollama-local"]
            assert providers.find_providers_for_model("nvidia/Qwen3.6-35B-A3B-NVFP4") == ["vllm"]
            assert providers.find_providers_for_model("unknown-model") == []

    def test_get_model_context_window(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "vllm": [{"id": "m1", "context_window": 262144, "reasoning": True}],
        }):
            assert providers.get_model_context_window("vllm", "m1") == 262144
            assert providers.get_model_context_window("vllm", "m2") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'axi.providers'`

- [ ] **Step 3: Write the implementation**

```python
# axi/providers.py
"""Provider registry and model discovery.

Providers are named entries in ``user-data/providers.json`` (precedent:
``mcp_servers.json``). A built-in ``anthropic`` provider is hardcoded and
always present. Discovery is advisory: routing never blocks on it, and a
failed fetch degrades to per-provider/per-model ``error``/``null`` markers.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from axi import config

log = logging.getLogger(__name__)

PROVIDERS_PATH = os.path.join(config.AXI_USER_DATA, "providers.json")
_CACHE_TTL_SECONDS = 60
_HTTP_TIMEOUT = 10.0

# Hardcoded anthropic model table — used for discovery/autocomplete only.
# Validation stays free-form, so unlisted model ids still type directly.
ANTHROPIC_MODELS: list[dict[str, Any]] = [
    {"id": "opus", "context_window": 200000, "reasoning": True},
    {"id": "sonnet", "context_window": 200000, "reasoning": True},
    {"id": "haiku", "context_window": 200000, "reasoning": False},
    {"id": "claude-opus-4-8", "context_window": 200000, "reasoning": True},
    {"id": "claude-sonnet-4-5", "context_window": 200000, "reasoning": True},
    {"id": "claude-haiku-4-5", "context_window": 200000, "reasoning": False},
]

_VALID_TYPES = {"anthropic", "ollama", "vllm"}


@dataclass
class Provider:
    name: str
    type: str
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] = field(default_factory=list)
    context_window: int | None = None


def _http_get(url: str) -> dict[str, Any]:
    resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _http_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(url, json=payload, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _builtin_anthropic() -> Provider:
    return Provider(name="anthropic", type="anthropic")


def load_providers() -> dict[str, Provider]:
    """Load the registry. Never raises; bad entries are logged and skipped."""
    result: dict[str, Provider] = {"anthropic": _builtin_anthropic()}
    if not os.path.exists(PROVIDERS_PATH):
        return result
    try:
        with open(PROVIDERS_PATH) as f:
            data = json.load(f)
    except Exception as e:
        log.warning("Failed to load providers.json: %s", e)
        return result
    for entry in data.get("providers", []):
        name = str(entry.get("name", "")).strip()
        ptype = str(entry.get("type", "")).strip()
        if not name:
            log.warning("Provider entry missing name; skipped")
            continue
        if ptype not in _VALID_TYPES:
            log.warning("Provider '%s' has invalid type '%s'; skipped", name, ptype)
            continue
        base_url = entry.get("base_url") or None
        if ptype in ("ollama", "vllm") and not base_url:
            log.warning("Provider '%s' (%s) requires base_url; skipped", name, ptype)
            continue
        if name in result:
            log.warning("Duplicate provider name '%s'; last entry wins", name)
        result[name] = Provider(
            name=name,
            type=ptype,
            base_url=base_url,
            api_key=entry.get("api_key") or None,
            models=[str(m) for m in (entry.get("models") or [])],
            context_window=entry.get("context_window") or None,
        )
    return result


def get_provider(name: str) -> Provider | None:
    return load_providers().get(name)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _fetch_ollama(base_url: str) -> list[dict[str, Any]]:
    tags = _http_get(f"{base_url}/api/tags")
    names = [m["name"] for m in tags.get("models", [])]

    def _show(name: str) -> dict[str, Any]:
        try:
            info = _http_post(f"{base_url}/api/show", {"model": name})
            model_info = info.get("model_info", {})
            ctx = None
            for key, value in model_info.items():
                if key.endswith(".context_length") and isinstance(value, int):
                    ctx = value
                    break
            return {"id": name, "context_window": ctx, "reasoning": False}
        except Exception:
            log.warning("ollama /api/show failed for '%s'", name, exc_info=True)
            return {"id": name, "context_window": None, "reasoning": False}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_show, names))


def _fetch_vllm(base_url: str) -> list[dict[str, Any]]:
    data = _http_get(f"{base_url}/v1/models")
    return [
        {
            "id": m["id"],
            "context_window": m.get("max_model_len"),
            "reasoning": True,
        }
        for m in data.get("data", [])
    ]


def _fetch(provider: Provider) -> list[dict[str, Any]]:
    if provider.type == "anthropic":
        return [dict(m) for m in ANTHROPIC_MODELS]
    if provider.type == "ollama":
        return _fetch_ollama(provider.base_url or "")
    if provider.type == "vllm":
        return _fetch_vllm(provider.base_url or "")
    return []


def _cached_models(provider: Provider) -> dict[str, Any]:
    now = time.monotonic()
    hit = _cache.get(provider.name)
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    try:
        models = _fetch(provider)
        result: dict[str, Any] = {"models": models}
    except Exception as e:
        log.warning("Discovery failed for provider '%s': %s", provider.name, e)
        result = {"error": str(e)}
    _cache[provider.name] = (now, result)
    return result


def list_models(provider: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return {provider_name: [model dicts]} for all (or one) providers."""
    reg = load_providers()
    names = [provider] if provider else list(reg)
    out: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        entry = reg.get(name)
        if entry is None:
            out[name] = {"error": f"unknown provider '{name}'"}
            continue
        cached = _cached_models(entry)
        if "error" in cached:
            out[name] = {"error": cached["error"]}
            continue
        models = cached["models"]
        if entry.models and not models:
            # Seed list for gateways with no model-list endpoint.
            models = [{"id": m, "context_window": entry.context_window, "reasoning": False} for m in entry.models]
        out[name] = models
    return out


def get_model_context_window(provider: str, model: str) -> int | None:
    entry = load_providers().get(provider)
    if entry is None:
        return None
    if entry.context_window is not None:
        return entry.context_window
    cached = _cached_models(entry)
    for m in cached.get("models", []):
        if m["id"] == model:
            return m.get("context_window")
    return None


def find_providers_for_model(model: str) -> list[str]:
    """Non-anthropic providers whose discovered/seed models contain ``model``."""
    reg = load_providers()
    matches: list[str] = []
    for name, entry in reg.items():
        if entry.type == "anthropic":
            continue
        cached = _cached_models(entry)
        ids = {m["id"] for m in cached.get("models", [])}
        if model in ids or model in entry.models:
            matches.append(name)
    return matches
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_providers.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add axi/providers.py tests/unit/test_providers.py
git commit -m "feat: provider registry and model discovery module"
```

---

### Task 2: `config.resolve_runtime` — routing, env assembly, hermetic strip

**Files:**
- Modify: `axi/config.py` (add `resolve_runtime`, `MANAGED_ENV_VARS`, `parse_provider_model`; make `get_model_runtime` wrap `resolve_runtime`)
- Modify: `axi/hub_wiring.py:44-58` (use `resolve_runtime` with `session.provider`, hermetic strip)
- Test: `tests/unit/test_config.py` (append classes)

**Interfaces:**
- Consumes: `axi.providers` (Task 1): `get_provider`, `find_providers_for_model`, `get_model_context_window`, `Provider`
- Produces:
  - `MANAGED_ENV_VARS: tuple[str, ...]` — the 10 vars from Global Constraints
  - `resolve_runtime(model: str, provider: str | None = None) -> tuple[str | None, dict[str, str], str]` — `(claude_model_arg, env, provider_name)`; raises `ValueError` for unknown explicit provider or ambiguous auto-route
  - `parse_provider_model(value: str) -> tuple[str | None, str]` — first-colon split only when prefix is a known provider name
  - `get_model_runtime(model)` keeps its signature, returns `(model_arg, env)` via `resolve_runtime`
  - `hub_wiring._make_agent_options` resolves with `session.provider`, falls back to auto-route on `ValueError`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_config.py
from axi import providers  # noqa: E402  (module-level import at top of file, not here)


class TestResolveRuntime:
    def _reg(self, *entries):
        reg = {"anthropic": providers.Provider(name="anthropic", type="anthropic")}
        for e in entries:
            reg[e.name] = e
        return reg

    def test_native_alias_no_env(self) -> None:
        with patch("axi.providers.load_providers", return_value=self._reg()):
            model_arg, env, provider = config.resolve_runtime("opus")
        assert model_arg == "opus"
        assert env == {}
        assert provider == "anthropic"

    def test_gpt_routes_to_proxy(self) -> None:
        with (
            patch.dict("os.environ", {"AXI_CHATGPT_PROXY_API_KEY": "test-token"}, clear=True),
            patch("axi.providers.load_providers", return_value=self._reg()),
        ):
            model_arg, env, provider = config.resolve_runtime("gpt-5.4")
        assert model_arg is None
        assert env["ANTHROPIC_MODEL"] == "gpt-5.4"
        assert provider == "chatgpt-proxy"

    def test_explicit_ollama_provider(self) -> None:
        reg = self._reg(providers.Provider(
            name="ollama-local", type="ollama", base_url="http://localhost:11434",
        ))
        with (
            patch("axi.providers.load_providers", return_value=reg),
            patch("axi.providers.get_model_context_window", return_value=32768),
        ):
            model_arg, env, provider = config.resolve_runtime("qwen3-coder:30b", provider="ollama-local")
        assert model_arg is None
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
        assert env["ANTHROPIC_MODEL"] == "qwen3-coder:30b"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "32768"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "qwen3-coder:30b"
        assert provider == "ollama-local"

    def test_ollama_auth_token_var(self) -> None:
        reg = self._reg(providers.Provider(
            name="ollama-local", type="ollama", base_url="http://localhost:11434", api_key="ollama",
        ))
        with (
            patch("axi.providers.load_providers", return_value=reg),
            patch("axi.providers.get_model_context_window", return_value=None),
        ):
            _, env, _ = config.resolve_runtime("m1", provider="ollama-local")
        assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
        assert "ANTHROPIC_API_KEY" not in env

    def test_vllm_api_key_var(self) -> None:
        reg = self._reg(providers.Provider(
            name="vllm", type="vllm", base_url="http://localhost:8199", api_key="k",
        ))
        with (
            patch("axi.providers.load_providers", return_value=reg),
            patch("axi.providers.get_model_context_window", return_value=262144),
        ):
            _, env, _ = config.resolve_runtime("nvidia/Qwen3.6-35B-A3B-NVFP4", provider="vllm")
        assert env["ANTHROPIC_API_KEY"] == "k"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"

    def test_unknown_provider_raises(self) -> None:
        with patch("axi.providers.load_providers", return_value=self._reg()):
            with pytest.raises(ValueError, match="Unknown provider 'nope'"):
                config.resolve_runtime("m1", provider="nope")

    def test_auto_route_single_match(self) -> None:
        reg = self._reg(providers.Provider(
            name="vllm", type="vllm", base_url="http://localhost:8199",
        ))
        with (
            patch("axi.providers.load_providers", return_value=reg),
            patch("axi.providers.find_providers_for_model", return_value=["vllm"]),
            patch("axi.providers.get_model_context_window", return_value=262144),
        ):
            model_arg, env, provider = config.resolve_runtime("nvidia/Qwen3.6-35B-A3B-NVFP4")
        assert model_arg is None
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8199"
        assert provider == "vllm"
        # auto-routed: no tier mapping
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env

    def test_auto_route_ambiguous_raises(self) -> None:
        with (
            patch("axi.providers.load_providers", return_value=self._reg()),
            patch("axi.providers.find_providers_for_model", return_value=["a", "b"]),
        ):
            with pytest.raises(ValueError, match="multiple providers.*a.*b"):
                config.resolve_runtime("shared-model")

    def test_auto_route_no_match_falls_back_to_anthropic(self) -> None:
        with (
            patch("axi.providers.load_providers", return_value=self._reg()),
            patch("axi.providers.find_providers_for_model", return_value=[]),
        ):
            model_arg, env, provider = config.resolve_runtime("some-free-form-id")
        assert model_arg == "some-free-form-id"
        assert env == {}
        assert provider == "anthropic"

    def test_explicit_native_anthropic_returns_model_arg(self) -> None:
        with patch("axi.providers.load_providers", return_value=self._reg()):
            model_arg, env, provider = config.resolve_runtime("opus", provider="anthropic")
        assert model_arg == "opus"
        assert env == {}
        assert provider == "anthropic"

    def test_explicit_anthropic_gateway(self) -> None:
        reg = self._reg(providers.Provider(
            name="gateway", type="anthropic", base_url="https://g.example", api_key="gk",
        ))
        with (
            patch("axi.providers.load_providers", return_value=reg),
            patch("axi.providers.get_model_context_window", return_value=None),
        ):
            model_arg, env, provider = config.resolve_runtime("claude-sonnet-4-5", provider="gateway")
        assert model_arg is None
        assert env["ANTHROPIC_BASE_URL"] == "https://g.example"
        assert env["ANTHROPIC_API_KEY"] == "gk"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-sonnet-4-5"
        assert provider == "gateway"


class TestParseProviderModel:
    def test_plain_model(self) -> None:
        with patch("axi.providers.load_providers", return_value={"anthropic": providers.Provider(name="anthropic", type="anthropic")}):
            assert config.parse_provider_model("opus") == (None, "opus")

    def test_provider_prefix(self) -> None:
        with patch("axi.providers.load_providers", return_value={
            "anthropic": providers.Provider(name="anthropic", type="anthropic"),
            "ollama-local": providers.Provider(name="ollama-local", type="ollama", base_url="http://x"),
        }):
            assert config.parse_provider_model("ollama-local:qwen3-coder:30b") == ("ollama-local", "qwen3-coder:30b")

    def test_colon_in_model_not_provider(self) -> None:
        with patch("axi.providers.load_providers", return_value={"anthropic": providers.Provider(name="anthropic", type="anthropic")}):
            assert config.parse_provider_model("qwen3-coder:30b") == (None, "qwen3-coder:30b")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k "ResolveRuntime or ParseProviderModel" -v`
Expected: FAIL — `AttributeError: module 'axi.config' has no attribute 'resolve_runtime'`

- [ ] **Step 3: Write the implementation**

Add to `axi/config.py` (after `get_model_runtime`, before `get_resolved_model`):

```python
# Env vars Axi manages per provider. Stripped from agent env before resolution
# so one provider's leftovers never leak into a session routed elsewhere.
MANAGED_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

_TIER_MAPPING_VARS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def parse_provider_model(value: str) -> tuple[str | None, str]:
    """Split ``provider:model`` at the first colon, only when the prefix is a
    known provider name. Ollama model ids contain colons (``qwen3-coder:30b``),
    so a bare model string is never split."""
    from axi import providers

    if ":" in value:
        prefix, _, rest = value.partition(":")
        if providers.get_provider(prefix) is not None:
            return prefix, rest
    return None, value


def _provider_env(provider: Any, model: str, explicit: bool) -> dict[str, str]:
    from axi import providers

    env: dict[str, str] = {}
    if provider.type == "anthropic" and provider.base_url is None:
        return env  # native API, OAuth
    env["ANTHROPIC_BASE_URL"] = provider.base_url or ""
    env["ANTHROPIC_MODEL"] = model
    if provider.api_key:
        key = "ANTHROPIC_AUTH_TOKEN" if provider.type == "ollama" else "ANTHROPIC_API_KEY"
        env[key] = provider.api_key
    ctx = provider.context_window or providers.get_model_context_window(provider.name, model)
    if ctx:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(ctx)
    if explicit and provider.base_url is not None:
        for var in _TIER_MAPPING_VARS:
            env[var] = model
    return env


def resolve_runtime(model: str, provider: str | None = None) -> tuple[str | None, dict[str, str], str]:
    """Resolve a model (and optional provider) into Claude model args and env.

    Returns ``(claude_model_arg, env, provider_name)``. Raises ValueError for
    an unknown explicit provider or an ambiguous auto-route (model present on
    multiple non-anthropic providers).
    """
    from axi import providers

    resolved = _normalize_model_selector(model)
    if provider is not None:
        entry = providers.get_provider(provider)
        if entry is None:
            raise ValueError(f"Unknown provider '{provider}'")
        if entry.type == "anthropic" and entry.base_url is None:
            return resolved, {}, "anthropic"  # native API, OAuth
        return None, _provider_env(entry, resolved, explicit=True), provider
    if uses_chatgpt_proxy(resolved):
        return None, _chatgpt_proxy_env(resolved), "chatgpt-proxy"
    if _normalize_model_selector(resolved) in VALID_MODELS or resolved in {m["id"] for m in providers.ANTHROPIC_MODELS}:
        return resolved, {}, "anthropic"
    matches = providers.find_providers_for_model(resolved)
    if len(matches) == 1:
        entry = providers.get_provider(matches[0])
        assert entry is not None
        return None, _provider_env(entry, resolved, explicit=False), matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Model '{resolved}' is available on multiple providers: {', '.join(matches)}. "
            f"Specify one with provider:model."
        )
    return resolved, {}, "anthropic"


def get_model_runtime(model: str) -> tuple[str | None, dict[str, str]]:
    """Resolve an Axi model selector into Claude model args and env vars."""
    model_arg, env, _ = resolve_runtime(model)
    return model_arg, env
```

Add `"MANAGED_ENV_VARS"`, `"parse_provider_model"`, `"resolve_runtime"` to `config.__all__`.

Modify `axi/hub_wiring.py` `_make_agent_options` (lines 44-58):

```python
    selected_model = session.model or config.get_model()
    try:
        resolved_model, resolved_env, _ = config.resolve_runtime(selected_model, provider=getattr(session, "provider", None))
    except ValueError:
        log.warning(
            "Provider resolution failed for agent '%s' (model=%s provider=%s); falling back to auto-routing",
            session.name, selected_model, getattr(session, "provider", None),
        )
        resolved_model, resolved_env, _ = config.resolve_runtime(selected_model)
    minflow_data_dir = os.environ.get("MINFLOW_DATA_DIR") or os.path.expanduser("~/.config/minflow")
    base_env = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "MINFLOW_DATA_DIR": minflow_data_dir,
        "PATH": os.path.join(config.BOT_DIR, "bin") + ":" + os.environ.get("PATH", ""),
    }
    for key in config.MANAGED_ENV_VARS:
        base_env.pop(key, None)
    base_env.update(resolved_env)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS (existing + new tests)

- [ ] **Step 5: Commit**

```bash
git add axi/config.py axi/hub_wiring.py tests/unit/test_config.py
git commit -m "feat: resolve_runtime with provider routing and env assembly"
```

---

### Task 3: `axi_list_models` MCP tool

**Files:**
- Modify: `axi/tools.py` (add `axi_list_models` tool; add to `utils_mcp_server` tools list at line ~1002)
- Test: `tests/unit/test_tools_list_models.py`

**Interfaces:**
- Consumes: `axi.providers.list_models` (Task 1)
- Produces: `axi_list_models(args: McpArgs) -> McpResult` — MCP tool callable by every agent via the `utils` server

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_list_models.py
"""Unit tests for the axi_list_models MCP tool."""
from unittest.mock import patch

from axi import tools


class TestListModelsTool:
    def test_returns_provider_models(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "anthropic": [{"id": "opus", "context_window": 200000, "reasoning": True}],
            "ollama-local": [{"id": "qwen3-coder:30b", "context_window": 32768, "reasoning": False}],
        }):
            result = tools.axi_list_models({})
        assert result["is_error"] is False
        text = result["content"][0]["text"]
        assert "anthropic" in text and "opus" in text
        assert "ollama-local" in text and "qwen3-coder:30b" in text

    def test_provider_filter(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "vllm": [{"id": "m1", "context_window": 262144, "reasoning": True}],
        }) as lm:
            tools.axi_list_models({"provider": "vllm"})
        lm.assert_called_once_with("vllm")

    def test_error_provider_reported_not_fatal(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "vllm": {"error": "conn refused"},
            "anthropic": [{"id": "opus", "context_window": 200000, "reasoning": True}],
        }):
            result = tools.axi_list_models({})
        assert result["is_error"] is False
        text = result["content"][0]["text"]
        assert "conn refused" in text
        assert "opus" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_tools_list_models.py -v`
Expected: FAIL — `AttributeError: module 'axi.tools' has no attribute 'axi_list_models'`

- [ ] **Step 3: Write the implementation**

Add to `axi/tools.py` (after `axi_spawn_agent`):

```python
@tool(
    "axi_list_models",
    "List available models per provider (anthropic, ollama, vllm). Use this to discover exact model ids and context windows before spawning an agent or selecting a model. Optional 'provider' argument filters to one provider.",
    {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Optional provider name to filter (e.g. 'ollama-local', 'vllm'). Omit to list all providers.",
            },
        },
    },
)
async def axi_list_models(args: McpArgs) -> McpResult:
    from axi import providers

    provider = args.get("provider") or None
    listing = providers.list_models(provider)
    lines: list[str] = []
    for name, models in listing.items():
        if isinstance(models, dict) and "error" in models:
            lines.append(f"**{name}**: error — {models['error']}")
            continue
        lines.append(f"**{name}**:")
        for m in models:
            ctx = m.get("context_window")
            ctx_s = f" (context {ctx})" if ctx else ""
            lines.append(f"- `{m['id']}`{ctx_s}")
    return {
        "content": [{"type": "text", "text": "\n".join(lines) or "No providers configured."}],
        "is_error": False,
    }
```

Add `axi_list_models` to the `utils_mcp_server` tools list (line ~1002):

```python
utils_mcp_server = create_sdk_mcp_server(
    name="utils",
    version="1.0.0",
    tools=[
        get_date_and_time,
        post_file,
        set_status,
        clear_status,
        toggle_plan_mode,
        read_messages,
        post_message,
        search_messages,
        wait_for_message,
        axi_list_models,
    ],
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_tools_list_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add axi/tools.py tests/unit/test_tools_list_models.py
git commit -m "feat: axi_list_models MCP tool for provider/model discovery"
```

---

### Task 4: `session.provider` persistence and spawn plumbing

**Files:**
- Modify: `packages/agenthub/agenthub/types.py:96` (add `provider` field)
- Modify: `packages/agenthub/agenthub/runtime.py:109,150` (add `provider` param to `spawn_agent`, pass to `AgentSession`)
- Modify: `axi/agents.py` (`spawn_agent` signature + `_save_agent_config`/`_load_agent_config` + `_rebuild_session` + `reconstruct_agents_from_channels` + `restart_agent`)
- Modify: `axi/commands_api.py` (`validate_spawn`, `spawn`, `set_model`)
- Modify: `axi/tools.py` (`axi_spawn_agent` schema + arg)
- Modify: `axi/http_api.py` (`SpawnRequest`, `ModelRequest`)
- Modify: `axi/main.py` (`/spawn` command, `/model` command)
- Test: `tests/unit/test_provider_persistence.py`

**Interfaces:**
- Consumes: `config.parse_provider_model`, `config.resolve_runtime` (Task 2)
- Produces:
  - `AgentSession.provider: str | None`
  - `agents.spawn_agent(..., model=None, provider=None)`
  - `agents._save_agent_config(name, mcp_server_names, extensions=None, model=None, provider=None)`
  - `commands_api.validate_spawn(name, cwd, model, provider=None)` — data carries `{cwd, model, provider}`
  - `commands_api.spawn(name, prompt, *, cwd=None, resume=None, model=None, provider=None)`
  - `commands_api.set_model(agent, model)` — parses `provider:model` internally, persists both
  - `axi_spawn_agent` schema gains `provider`; `SpawnRequest`/`ModelRequest` gain `provider`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_provider_persistence.py
"""Unit tests for provider persistence and spawn plumbing."""
from unittest.mock import patch

import pytest

from axi import commands_api, config


class TestSetModelParsing:
    def test_plain_model_sets_no_provider(self) -> None:
        with (
            patch("axi.config.set_model", return_value="") as sm,
            patch("axi.commands_api.agents", spec=[]),
        ):
            result = commands_api.set_model(None, "opus")
        assert result.ok
        sm.assert_called_once_with("opus")

    def test_provider_model_parses(self) -> None:
        with (
            patch("axi.config.parse_provider_model", return_value=("ollama-local", "qwen3-coder:30b")),
            patch("axi.config.validate_model", return_value=""),
            patch("axi.config.normalize_model", side_effect=lambda m: m),
            patch("axi.config.set_model", return_value=""),
            patch("axi.commands_api.agents", spec=[]),
        ):
            result = commands_api.set_model(None, "ollama-local:qwen3-coder:30b")
        assert result.ok
        assert "ollama-local:qwen3-coder:30b" in result.message


class TestValidateSpawn:
    def test_provider_passes_through(self) -> None:
        with (
            patch("axi.config.normalize_model", side_effect=lambda m: m),
            patch("axi.config.validate_model", return_value=""),
            patch("axi.config.ALLOWED_CWDS", ["/tmp"]),
        ):
            result = commands_api.validate_spawn("a1", "/tmp/x", "m1", provider="ollama-local")
        assert result.ok
        assert result.data["provider"] == "ollama-local"
        assert result.data["model"] == "m1"


class TestAgentConfigPersistence:
    def test_save_load_roundtrip_with_provider(self, tmp_path, monkeypatch) -> None:
        from axi import agents

        monkeypatch.setattr(config, "AXI_USER_DATA", str(tmp_path))
        agents._save_agent_config("a1", None, model="m1", provider="ollama-local")
        cfg = agents._load_agent_config("a1")
        assert cfg["model"] == "m1"
        assert cfg["provider"] == "ollama-local"

    def test_load_missing_provider_ok(self, tmp_path, monkeypatch) -> None:
        from axi import agents

        monkeypatch.setattr(config, "AXI_USER_DATA", str(tmp_path))
        agents._save_agent_config("a2", None, model="m1")
        cfg = agents._load_agent_config("a2")
        assert cfg.get("provider") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_provider_persistence.py -v`
Expected: FAIL — `TypeError: _save_agent_config() got an unexpected keyword argument 'provider'`

- [ ] **Step 3: Write the implementation**

`packages/agenthub/agenthub/types.py` (after `model: str | None = None` at line 96):

```python
    model: str | None = None
    provider: str | None = None
```

`packages/agenthub/agenthub/runtime.py` — add `provider: str | None = None` to `spawn_agent` kwargs (after `model`), pass `provider=provider` to the `AgentSession(...)` constructor.

`axi/agents.py`:

```python
def _save_agent_config(
    agent_name: str,
    mcp_server_names: list[str] | None,
    extensions: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    ...
    if model is not None:
        data["model"] = model
    if provider is not None:
        data["provider"] = provider
```

`spawn_agent` signature: add `provider: str | None = None` after `model`; pass `provider=provider` to `hub.spawn_agent(...)`; pass `provider=provider` to `_save_agent_config(...)`.

`_rebuild_session`: capture `old_provider = session.provider if session else None`; pass `provider=old_provider` to the new `AgentSession(...)`.

`reconstruct_agents_from_channels`: `saved_provider: str | None = agent_cfg.get("provider")`; pass `provider=saved_provider` to `AgentSession(...)`.

`restart_agent`: `session.provider = agent_cfg.get("provider")`; pass `provider=session.provider` to `_save_agent_config(...)`.

`axi/commands_api.py`:

```python
def validate_spawn(name: str, cwd: str | None, model: str | None, provider: str | None = None) -> CommandResult:
    ...
    if provider is not None and config.get_provider(provider) is None:
        return CommandResult(message=f"*System:* Unknown provider '{provider}'.", ok=False, ephemeral=True)
    return CommandResult(message="", data={"cwd": agent_cwd, "model": agent_model, "provider": provider})
```

(Add `get_provider` to `config.__all__` and re-export from `axi.providers` — see note below.)

```python
async def spawn(
    name: str, prompt: str, *, cwd: str | None = None, resume: str | None = None,
    model: str | None = None, provider: str | None = None,
) -> CommandResult:
    ...
    valid = validate_spawn(agent_name, cwd, model, provider=provider)
    ...
    agent_provider = valid.data["provider"]
    await agents.spawn_agent(agent_name, agent_cwd, prompt, resume=resume, model=agent_model, provider=agent_provider)
    model_suffix = f" using **{agent_model}**" if agent_model else ""
    if agent_provider:
        model_suffix += f" on **{agent_provider}**"
    ...
    data={"name": agent_name, "cwd": agent_cwd, "model": agent_model, "provider": agent_provider},
```

`set_model` — parse `provider:model` at the top, persist both:

```python
async def set_model(agent: str | None, model: str | None) -> CommandResult:
    if model is None:  # view
        if agent and agent in agents.agents:
            session = agents.agents[agent]
            current = session.model or config.get_model()
            if session.provider:
                current = f"{session.provider}:{current}"
            return CommandResult(message=f"Current model for **{agent}**: **{current}**", data={"agent": agent, "model": current, "provider": session.provider})
        current = config.get_model()
        return CommandResult(message=f"Current default model: **{current}**", data={"agent": None, "model": current})

    provider, bare_model = config.parse_provider_model(model)
    error = config.validate_model(bare_model)
    if error:
        return CommandResult(message=f"*System:* {error}", ok=False, ephemeral=True)
    normalized = config.normalize_model(bare_model)
    if agent and agent in agents.agents:
        session = agents.agents[agent]
        session.model = normalized
        session.provider = provider
        agent_cfg = agents._load_agent_config(agent)
        agents._save_agent_config(agent, session.mcp_server_names, extensions=agent_cfg.get("extensions"), model=normalized, provider=provider)
        await agents.reset_session(agent)
        display = f"{provider}:{normalized}" if provider else normalized
        return CommandResult(
            message=f"*System:* Agent **{agent}** switched to **{display}** and restarted with a fresh session.",
            data={"agent": agent, "model": normalized, "provider": provider},
        )
    error = config.set_model(normalized)
    if error:
        return CommandResult(message=f"*System:* {error}", ok=False, ephemeral=True)
    return CommandResult(
        message=f"*System:* Default model set to **{config.get_model()}**.", data={"agent": None, "model": config.get_model()}
    )
```

`axi/tools.py` `axi_spawn_agent`: add to schema properties:

```python
            "provider": {
                "type": "string",
                "description": "Optional provider name (from providers.json, e.g. 'ollama-local', 'vllm'). Leave unset to auto-route the model.",
            },
```

and in the handler after `agent_model` validation:

```python
    agent_provider: str | None = args.get("provider")
    if agent_provider is not None and config.get_provider(agent_provider) is None:
        return {"content": [{"type": "text", "text": f"Error: unknown provider '{agent_provider}'"}], "is_error": True}
```

and pass `provider=agent_provider` to the `agents.spawn_agent(...)` call inside `_do_spawn`.

`axi/http_api.py`:

```python
class SpawnRequest(BaseModel):
    name: str
    prompt: str
    cwd: str | None = None
    resume: str | None = None
    model: str | None = None
    provider: str | None = None
```

`api_spawn`: pass `provider=req.provider`. `ModelRequest`: add `provider: str | None = None`; `api_model`: pass `provider=req.provider` to `set_model` (extend `set_model` signature with `provider: str | None = None` that overrides parsing when set).

`axi/main.py` `/spawn` command: add `provider: str | None = None` param + `@app_commands.describe(provider="Optional provider name (e.g. ollama-local, vllm)")`; pass to `validate_spawn` and `commands_api.spawn`.

`axi/main.py` `/model` command: no signature change (parsing happens in `set_model`); update the describe text to mention `provider:model`.

**Note on `config.get_provider`:** add to `axi/config.py`:

```python
def get_provider(name: str) -> Any | None:
    """Look up a provider by name (None if unknown). Re-exported for callers."""
    from axi import providers

    return providers.get_provider(name)
```

and add `"get_provider"` to `config.__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_provider_persistence.py tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Run the full unit tier to catch regressions**

Run: `uv run pytest tests/unit -q`
Expected: PASS (existing tests unaffected — `get_model_runtime` signature unchanged, `_save_agent_config` new param is keyword-only optional)

- [ ] **Step 6: Commit**

```bash
git add packages/agenthub/agenthub/types.py packages/agenthub/agenthub/runtime.py axi/agents.py axi/commands_api.py axi/tools.py axi/http_api.py axi/main.py axi/config.py tests/unit/test_provider_persistence.py
git commit -m "feat: session.provider persistence and spawn plumbing"
```

---

### Task 5: Flowchart provider transform + flowcoder-core PR

**Files:**
- Create: `axi/flowchart_providers.py` (transform pass)
- Modify: `axi/flowcoder.py` (`get_search_paths` prepends shadow dir)
- Modify: `pyproject.toml` (pin bump after PR merges)
- Test: `tests/unit/test_flowchart_providers.py`
- External: flowcoder-core PR (schema + env plumbing)

**Interfaces:**
- Consumes: `config.resolve_runtime` (Task 2), `config.AXI_USER_DATA`
- Produces:
  - `SHADOW_DIR: str` — `os.path.join(config.AXI_USER_DATA, "flowcharts-resolved")`
  - `transform_commands(search_paths: list[str]) -> list[str]` — writes transformed command JSONs to `SHADOW_DIR`, returns the shadow dir path (or `[]` if nothing to transform)
  - `get_search_paths` prepends the shadow dir when it has content

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_flowchart_providers.py
"""Unit tests for the flowchart provider transform pass."""
import json

from axi import flowchart_providers


class TestTransform:
    def _write_command(self, path, name, blocks):
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps({
            "name": name,
            "description": "test",
            "flowchart": {"blocks": blocks},
        }))

    def test_provider_block_rewritten_to_env(self, tmp_path, monkeypatch) -> None:
        src = tmp_path / "src"
        self._write_command(src, "soul-lite-do", {
            "start": {"id": "start", "type": "spawn", "name": "SPAWN",
                      "agent_name": "sl-do", "command_name": "do",
                      "model": "qwen3-coder:30b", "provider": "ollama-local"},
        })
        monkeypatch.setattr(flowchart_providers, "SHADOW_DIR", str(tmp_path / "shadow"))
        with (
            monkeypatch.context() as m,
        ):
            from unittest.mock import patch
            with patch("axi.flowchart_providers.resolve_runtime", return_value=(None, {
                "ANTHROPIC_BASE_URL": "http://localhost:11434",
                "ANTHROPIC_MODEL": "qwen3-coder:30b",
            }, "ollama-local")):
                shadow = flowchart_providers.transform_commands([str(src)])
        assert shadow == str(tmp_path / "shadow")
        out = json.loads((tmp_path / "shadow" / "soul-lite-do.json").read_text())
        block = out["flowchart"]["blocks"]["start"]
        assert "provider" not in block
        assert block["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
        assert block["model"] == "qwen3-coder:30b"

    def test_no_provider_blocks_writes_nothing(self, tmp_path, monkeypatch) -> None:
        src = tmp_path / "src"
        self._write_command(src, "plain", {
            "start": {"id": "start", "type": "spawn", "name": "S",
                      "agent_name": "a", "command_name": "do", "model": "opus"},
        })
        monkeypatch.setattr(flowchart_providers, "SHADOW_DIR", str(tmp_path / "shadow"))
        shadow = flowchart_providers.transform_commands([str(src)])
        assert shadow == ""
        assert not (tmp_path / "shadow").exists()

    def test_unknown_provider_leaves_block_untouched(self, tmp_path, monkeypatch) -> None:
        src = tmp_path / "src"
        self._write_command(src, "bad", {
            "start": {"id": "start", "type": "spawn", "name": "S",
                      "agent_name": "a", "command_name": "do",
                      "model": "m1", "provider": "nope"},
        })
        monkeypatch.setattr(flowchart_providers, "SHADOW_DIR", str(tmp_path / "shadow"))
        with patch("axi.flowchart_providers.resolve_runtime", side_effect=ValueError("Unknown provider 'nope'")):
            shadow = flowchart_providers.transform_commands([str(src)])
        assert shadow == ""
        assert not (tmp_path / "shadow").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_flowchart_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'axi.flowchart_providers'`

- [ ] **Step 3: Write the implementation**

```python
# axi/flowchart_providers.py
"""Transform flowchart spawn blocks carrying a ``provider`` field into
``env`` maps the flowcoder engine understands.

The engine parses command JSONs itself from the search paths, so Axi cannot
inject resolved env into a block the engine already parsed. Instead, at
engine launch we scan the command JSONs, rewrite provider blocks to carry
their resolved env, and write the transformed commands to a shadow dir that
is prepended to the search paths (first-match-wins makes shadows
authoritative).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from axi import config

log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(config.AXI_USER_DATA, "flowcharts-resolved")


def _iter_command_files(search_paths: list[str]):
    for sp in search_paths:
        if not os.path.isdir(sp):
            continue
        for fname in sorted(os.listdir(sp)):
            if fname.endswith(".json"):
                yield os.path.join(sp, fname)


def _transform_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Return the rewritten block, or None if unchanged/error."""
    if block.get("type") != "spawn" or "provider" not in block:
        return None
    provider = block["provider"]
    model = block.get("model") or ""
    from axi import config as cfg

    try:
        _, env, _ = cfg.resolve_runtime(model, provider=provider)
    except ValueError as e:
        log.warning("Flowchart provider block skipped: %s", e)
        return None
    new_block = dict(block)
    new_block.pop("provider", None)
    new_block["env"] = env
    return new_block


def transform_commands(search_paths: list[str]) -> str:
    """Rewrite provider spawn blocks into env maps; return shadow dir path
    ('' if nothing was transformed)."""
    written: list[str] = []
    for path in _iter_command_files(search_paths):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            log.warning("Skipping unreadable command %s: %s", path, e)
            continue
        blocks = data.get("flowchart", {}).get("blocks", {})
        changed = False
        for key, block in blocks.items():
            if not isinstance(block, dict):
                continue
            new_block = _transform_block(block)
            if new_block is not None:
                blocks[key] = new_block
                changed = True
        if not changed:
            continue
        os.makedirs(SHADOW_DIR, exist_ok=True)
        out_path = os.path.join(SHADOW_DIR, os.path.basename(path))
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        written.append(out_path)
    if not written:
        return ""
    return SHADOW_DIR
```

Modify `axi/flowcoder.py` `get_search_paths` — prepend the shadow dir when it has content:

```python
def get_search_paths(extra: list[str] | None = None) -> list[str]:
    """Return flowchart command search paths."""
    from axi import config

    default_search = _default_commands_dir()
    bot_commands = os.path.join(config.BOT_DIR, "commands")
    env_raw = os.environ.get("FLOWCODER_SEARCH_PATH", "")
    env_paths = [p for p in env_raw.split(":") if p]
    paths = [default_search, bot_commands] + env_paths + (extra or [])

    discord_commands = os.path.join(bot_commands, "discord")
    if os.path.isdir(discord_commands) and _has_discord_frontend():
        paths.append(discord_commands)

    from axi import flowchart_providers

    shadow = flowchart_providers.transform_commands(paths)
    if shadow:
        paths.insert(0, shadow)

    return paths
```

**flowcoder-core PR** (external, in `Gaia-PBC/flowcoder-core`):

1. `packages/flowcoder-flowchart/flowcoder_flowchart/blocks.py` — `SpawnBlock` gains:
   ```python
   env: dict[str, str] | None = None
   ```
2. `packages/flowcoder-engine/flowcoder_engine/session.py`:
   - `ClaudeSession.__init__` gains `env: dict[str, str] | None = None`; store as `self._env`.
   - `clone()` and `with_model()` pass `env=self._env` through.
   - `start()`: `await self._process.start(self._claude_cmd, _clean_env(), os.getcwd())` becomes:
     ```python
     env = _clean_env()
     if self._env:
         env.update(self._env)
     await self._process.start(self._claude_cmd, env, os.getcwd())
     ```
3. `packages/flowcoder-engine/flowcoder_engine/session_factory.py` — `create(self, backend, name, model=None, env=None)`; pass `env` to the creator.
4. `packages/flowcoder-engine/flowcoder_engine/__main__.py` — the `claude` factory lambda gains `env`:
   ```python
   lambda name, model, env=None: ClaudeSession(name=name, claude_cmd=[*claude_cmd, "--model", model] if model else list(claude_cmd), protocol=protocol, env=env),
   ```
5. `packages/flowcoder-engine/flowcoder_engine/walker.py` `_exec_spawn` — pass `block.env`:
   ```python
   if block.backend and self._session_factory:
       child_session = self._session_factory.create(block.backend, agent_name, block.model, block.env)
   elif block.model or block.env:
       child_session = self._session.with_model(block.model or "").with_env(block.env).clone(agent_name)
   ```
   (Add `with_env(env)` to `ClaudeSession` returning a copy with `self._env = env`.)

After the PR merges, bump both pins in `pyproject.toml`:

```toml
flowcoder-engine = { git = "https://github.com/Gaia-PBC/flowcoder-core.git", rev = "<PR merge commit sha>", subdirectory = "packages/flowcoder-engine" }
flowcoder-flowchart = { git = "https://github.com/Gaia-PBC/flowcoder-core.git", rev = "<PR merge commit sha>", subdirectory = "packages/flowcoder-flowchart" }
```

then `uv lock` and `uv sync`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_flowchart_providers.py -v`
Expected: PASS

- [ ] **Step 5: Run the full unit tier**

Run: `uv run pytest tests/unit -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add axi/flowchart_providers.py axi/flowcoder.py tests/unit/test_flowchart_providers.py
git commit -m "feat: flowchart provider transform pass"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/axi-runtime-configuration.md`
- Modify: `prompts/refs/flowcharts.md`

- [ ] **Step 1: Update `docs/axi-runtime-configuration.md`**

Add a "Providers" section after the "Model" section:

```markdown
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
```

- [ ] **Step 2: Update `prompts/refs/flowcharts.md`**

In the spawn block row (line ~52), extend the field list:

```
| `spawn` | `agent_name`, `command_name`; opt `arguments`, `inherit_variables`, `exit_code_variable`, `config_file`, `model`, `backend`, `provider` | Spawn an axi sub-agent asynchronously running a named command. `provider` (a name from providers.json) resolves the block's model to that provider's env; omit it to inherit the parent agent's provider. Parent flowchart proceeds immediately. |
```

- [ ] **Step 3: Commit**

```bash
git add docs/axi-runtime-configuration.md prompts/refs/flowcharts.md
git commit -m "docs: provider registry configuration and flowchart provider field"
```

---

## Self-Review Notes

- Spec §1 (registry) → Task 1. §2 (discovery + tool) → Tasks 1 + 3. §3 (routing/env/persistence/entry points) → Tasks 2 + 4. §4 (flowchart) → Task 5. §5 (error handling) → Task 1 (registry/discovery), Task 2 (routing), Task 4 (unknown provider at call sites), Task 5 (flowchart unknown provider). §6 (testing) → per-task tests. §7 (docs) → Task 6.
- The vLLM upgrade (shim removal) is intentionally NOT in this plan — the experiment showed the NGC image's flash_attn is incompatible with PyPI vllm 0.27.1, so the live server stays on 0.21.0 dev with the shim at `:8199`. The provider entry points at `:8199`; upgrading vLLM later is a separate task.
- `get_resolved_model` (used by `main.py` startup logging) is untouched — it calls `get_model_runtime`, which now wraps `resolve_runtime` with the same behavior for existing inputs.
