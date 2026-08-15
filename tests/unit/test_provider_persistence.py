"""Unit tests for provider persistence and spawn plumbing."""
from unittest.mock import patch

import pytest

from axi import commands_api, config


class TestSetModelParsing:
    async def test_plain_model_sets_no_provider(self) -> None:
        with (
            patch("axi.config.set_model", return_value="") as sm,
            patch("axi.commands_api.agents", spec=[]),
        ):
            result = await commands_api.set_model(None, "opus")
        assert result.ok
        sm.assert_called_once_with("opus")

    async def test_provider_model_parses(self) -> None:
        with (
            patch("axi.config.parse_provider_model", return_value=("ollama-local", "qwen3-coder:30b")),
            patch("axi.config.validate_model", return_value=""),
            patch("axi.config.normalize_model", side_effect=lambda m: m),
            patch("axi.config.set_model", return_value=""),
            patch("axi.commands_api.agents", spec=[]),
        ):
            result = await commands_api.set_model(None, "ollama-local:qwen3-coder:30b")
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
