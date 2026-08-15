"""Unit tests for Axi harness/model configuration."""

from unittest.mock import patch

import pytest

from axi import config
from axi import providers
from axi.config import (
    CHATGPT_PROXY_DEFAULT_ENV,
    VALID_HARNESSES,
    VALID_MODELS,
    get_fc_wrap,
    get_harness,
    get_model,
    get_model_runtime,
    get_resolved_model,
    set_model,
    uses_chatgpt_proxy,
)


class TestValidateNamespace:
    def test_off_allowed(self) -> None:
        from axi import config

        assert config._validate_namespace("off") == "off"

    def test_valid_namespace(self) -> None:
        from axi import config

        assert config._validate_namespace("dev") == "dev"

    def test_empty_rejected(self) -> None:
        from axi import config

        with pytest.raises(ValueError, match="BOT_NAMESPACE is required"):
            config._validate_namespace("")

    def test_too_long_rejected(self) -> None:
        from axi import config

        with pytest.raises(ValueError, match="too long"):
            config._validate_namespace("a" * 21)

    @pytest.mark.parametrize("bad", ["Dev", "dev_1", "-dev", "dev-", "dev name", "déjà"])
    def test_invalid_chars_rejected(self, bad: str) -> None:
        from axi import config

        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            config._validate_namespace(bad)

    def test_module_constant_defaults_to_off(self) -> None:
        # Set by tests/unit/conftest.py before import.
        from axi import config

        assert config.BOT_NAMESPACE == "off"



class TestHarness:
    def test_default_harness_is_flowcoder(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert get_harness() == "flowcoder"

    def test_claude_code_harness_env(self) -> None:
        with patch.dict("os.environ", {"AXI_HARNESS": "claude_code"}, clear=True):
            assert get_harness() == "claude_code"

    def test_hyphenated_harness_alias(self) -> None:
        with patch.dict("os.environ", {"AXI_HARNESS": "claude-code"}, clear=True):
            assert get_harness() == "claude_code"

    def test_legacy_flowcoder_disabled_maps_to_claude_code(self) -> None:
        with patch.dict("os.environ", {"FLOWCODER_ENABLED": "0"}, clear=True):
            assert get_harness() == "claude_code"

    def test_all_valid_harnesses_accepted(self) -> None:
        for harness in VALID_HARNESSES:
            with patch.dict("os.environ", {"AXI_HARNESS": harness}, clear=True):
                assert get_harness() == harness


class TestFlowCoderWrap:
    def test_default_wrap_is_soul(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert get_fc_wrap() == "soul"

    def test_wrap_can_be_disabled(self) -> None:
        for value in ("", "off", "none", "0", "false"):
            with patch.dict("os.environ", {"AXI_FC_WRAP": value}, clear=True):
                assert get_fc_wrap() is None

    def test_wrap_accepts_flowchart_name(self) -> None:
        with patch.dict("os.environ", {"AXI_FC_WRAP": "triage"}, clear=True):
            assert get_fc_wrap() == "triage"

    def test_invalid_wrap_disables(self) -> None:
        with patch.dict("os.environ", {"AXI_FC_WRAP": "../bad"}, clear=True):
            assert get_fc_wrap() is None


class TestGetModel:
    def test_default_is_opus(self) -> None:
        with (
            patch.dict("os.environ", {"AXI_MODEL": ""}),
            patch("axi.config._load_config", return_value={}),
        ):
            assert get_model() == "opus"

    def test_returns_configured_model(self) -> None:
        with (
            patch.dict("os.environ", {"AXI_MODEL": ""}),
            patch("axi.config._load_config", return_value={"model": "sonnet"}),
        ):
            assert get_model() == "sonnet"

    def test_env_override_wins(self) -> None:
        with (
            patch.dict("os.environ", {"AXI_MODEL": "gpt-5.4"}),
            patch("axi.config._load_config", return_value={"model": "sonnet"}),
        ):
            assert get_model() == "gpt-5.4"

    def test_legacy_codex_alias_maps_to_gpt54(self) -> None:
        with patch.dict("os.environ", {"AXI_MODEL": "codex"}):
            assert get_model() == "gpt-5.4"


class TestModelRuntime:
    def test_native_model_runtime(self) -> None:
        resolved_model, env = get_model_runtime("opus")
        assert resolved_model == "opus"
        assert env == {}

    def test_gpt_runtime_uses_proxy_env(self) -> None:
        # Provide AXI_CHATGPT_PROXY_API_KEY so the env-override path supplies
        # the token without touching the on-disk token file.
        with patch.dict("os.environ", {"AXI_CHATGPT_PROXY_API_KEY": "test-token"}, clear=True):
            resolved_model, env = get_model_runtime("gpt-5.4")
        assert resolved_model is None
        assert env == {
            "ANTHROPIC_API_KEY": "test-token",
            "ANTHROPIC_BASE_URL": CHATGPT_PROXY_DEFAULT_ENV["ANTHROPIC_BASE_URL"],
            "ANTHROPIC_MODEL": "gpt-5.4",
        }

    def test_legacy_codex_runtime_uses_proxy_env(self) -> None:
        with patch.dict("os.environ", {"AXI_CHATGPT_PROXY_API_KEY": "test-token"}, clear=True):
            resolved_model, env = get_model_runtime("codex")
        assert resolved_model is None
        assert env == {
            "ANTHROPIC_API_KEY": "test-token",
            "ANTHROPIC_BASE_URL": CHATGPT_PROXY_DEFAULT_ENV["ANTHROPIC_BASE_URL"],
            "ANTHROPIC_MODEL": "gpt-5.4",
        }

    def test_chatgpt_proxy_env_overrides(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AXI_CHATGPT_PROXY_BASE_URL": "http://127.0.0.1:3999",
                "AXI_CHATGPT_PROXY_API_KEY": "local-key",
            },
            clear=True,
        ):
            resolved_model, env = get_model_runtime("gpt-5.4")

        assert resolved_model is None
        assert env == {
            "ANTHROPIC_API_KEY": "local-key",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:3999",
            "ANTHROPIC_MODEL": "gpt-5.4",
        }

    def test_uses_chatgpt_proxy(self) -> None:
        assert uses_chatgpt_proxy("gpt-5.4")
        assert not uses_chatgpt_proxy("opus")

    def test_get_resolved_model_uses_configured_model(self) -> None:
        with (
            patch.dict("os.environ", {"AXI_MODEL": "", "AXI_CHATGPT_PROXY_API_KEY": "test-token"}),
            patch("axi.config._load_config", return_value={"model": "gpt-5.4"}),
        ):
            axi_model, resolved_model, env = get_resolved_model()
        assert axi_model == "gpt-5.4"
        assert resolved_model is None
        assert env == {
            "ANTHROPIC_API_KEY": "test-token",
            "ANTHROPIC_BASE_URL": CHATGPT_PROXY_DEFAULT_ENV["ANTHROPIC_BASE_URL"],
            "ANTHROPIC_MODEL": "gpt-5.4",
        }


class TestSetModel:
    def test_valid_model(self) -> None:
        with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
            result = set_model("sonnet")
            assert result == ""

    def test_provider_model(self) -> None:
        with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
            result = set_model("gpt-5.4")
            assert result == ""

    def test_invalid_model(self) -> None:
        result = set_model("gpt 5")
        assert "Invalid model" in result

    def test_case_insensitive(self) -> None:
        with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
            result = set_model("HAIKU")
            assert result == ""

    def test_all_valid_models_accepted(self) -> None:
        for model in VALID_MODELS:
            with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
                result = set_model(model)
                assert result == "", f"Model '{model}' should be valid"


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

    def test_gpt_proxy_env_uses_import_snapshot(self) -> None:
        # The hermetic strip in hub_wiring._make_agent_options pops the managed
        # vars from os.environ at runtime; _chatgpt_proxy_env must keep honoring
        # the ANTHROPIC_* overrides from the import-time snapshot. Patch the
        # snapshot directly (it is captured at import) and leave os.environ
        # without any AXI_CHATGPT_PROXY_* or ANTHROPIC_* values so the token
        # file would be the only fallback — proving the snapshot is used.
        with (
            patch.dict("axi.config._ENV_SNAPSHOT", {
                "ANTHROPIC_API_KEY": "snap-key",
                "ANTHROPIC_BASE_URL": "http://snap:1",
            }),
            patch.dict("os.environ", {}, clear=True),
            patch("axi.providers.load_providers", return_value=self._reg()),
        ):
            model_arg, env, provider = config.resolve_runtime("gpt-5.4")
        assert model_arg is None
        assert env["ANTHROPIC_API_KEY"] == "snap-key"
        assert env["ANTHROPIC_BASE_URL"] == "http://snap:1"
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
