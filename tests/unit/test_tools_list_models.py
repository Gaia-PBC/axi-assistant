"""Unit tests for the axi_list_models MCP tool."""
from unittest.mock import patch

from axi import tools


class TestListModelsTool:
    async def test_returns_provider_models(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "anthropic": [{"id": "opus", "context_window": 200000, "reasoning": True}],
            "ollama-local": [{"id": "qwen3-coder:30b", "context_window": 32768, "reasoning": False}],
        }):
            result = await tools.axi_list_models.handler({})
        assert result["is_error"] is False
        text = result["content"][0]["text"]
        assert "anthropic" in text
        assert "opus" in text
        assert "ollama-local" in text
        assert "qwen3-coder:30b" in text

    async def test_provider_filter(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "vllm": [{"id": "m1", "context_window": 262144, "reasoning": True}],
        }) as lm:
            await tools.axi_list_models.handler({"provider": "vllm"})
        lm.assert_called_once_with("vllm")

    async def test_error_provider_reported_not_fatal(self) -> None:
        with patch("axi.providers.list_models", return_value={
            "vllm": {"error": "conn refused"},
            "anthropic": [{"id": "opus", "context_window": 200000, "reasoning": True}],
        }):
            result = await tools.axi_list_models.handler({})
        assert result["is_error"] is False
        text = result["content"][0]["text"]
        assert "conn refused" in text
        assert "opus" in text
