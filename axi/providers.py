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
        if provider.base_url:
            # Anthropic-protocol gateway with no model-list endpoint; seed
            # models (if any) are applied by list_models.
            return []
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
    models = list_models(provider).get(provider, [])
    for m in models:
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
        models = list_models(name).get(name, [])
        ids = {m["id"] for m in models}
        if model in ids or model in entry.models:
            matches.append(name)
    return matches
