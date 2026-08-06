"""Unit tests for Axi harness/model configuration."""

from unittest.mock import patch

import pytest

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
