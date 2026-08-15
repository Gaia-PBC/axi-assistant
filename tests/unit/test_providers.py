# tests/unit/test_providers.py
"""Unit tests for the provider registry and model discovery."""
import json
from unittest.mock import patch

import pytest

from axi import providers


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Isolate the module-level TTL cache between tests."""
    providers._cache.clear()


@pytest.fixture(autouse=True)
def _hermetic_registry(tmp_path, monkeypatch):
    """Point PROVIDERS_PATH at a tmp providers.json with the discovery providers.

    Discovery/cache tests call list_models("ollama-local")/list_models("vllm")
    and must not depend on an ambient providers.json at the runtime
    AXI_USER_DATA path.
    """
    p = tmp_path / "providers.json"
    p.write_text(json.dumps({"providers": [
        {"name": "ollama-local", "type": "ollama", "base_url": "http://localhost:11434"},
        {"name": "vllm", "type": "vllm", "base_url": "http://localhost:8199"},
    ]}))
    monkeypatch.setattr(providers, "PROVIDERS_PATH", str(p))


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

    def test_non_object_file_yields_only_anthropic(self, tmp_path, monkeypatch) -> None:
        p = tmp_path / "providers.json"
        p.write_text(json.dumps([]))
        monkeypatch.setattr(providers, "PROVIDERS_PATH", str(p))
        reg = providers.load_providers()
        assert set(reg) == {"anthropic"}
        assert reg["anthropic"].type == "anthropic"


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

    def test_ollama_family_prefix_context_length_wins(self) -> None:
        # Multi-arch model_info: the key matching details.family must win
        # over the first <arch>.context_length key (spec §2).
        tags = {"models": [{"name": "qwen3-coder:30b", "details": {"family": "qwen3"}}]}
        show = {"model_info": {
            "gemma4.context_length": 131072,
            "qwen3.context_length": 32768,
        }}
        with (
            patch("axi.providers._http_get", return_value=tags),
            patch("axi.providers._http_post", return_value=show),
        ):
            models = providers.list_models("ollama-local")
        assert models["ollama-local"] == [
            {"id": "qwen3-coder:30b", "context_window": 32768, "reasoning": False}
        ]

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

    def test_failed_fetch_helpers_do_not_crash(self) -> None:
        # A failed fetch makes list_models return {"error": ...} in place of
        # the model list; both helpers must treat that as "no models".
        with patch("axi.providers._http_get", side_effect=RuntimeError("conn refused")):
            assert providers.get_model_context_window("ollama-local", "m1") is None
            assert providers.find_providers_for_model("m1") == []
