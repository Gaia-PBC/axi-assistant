import os
from unittest.mock import patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from axi.axi_types import AgentSession
from axi.hub_wiring import _make_agent_options


class TestMakeAgentOptionsModelSelection:
    def test_session_model_override_is_resolved_for_runtime(self) -> None:
        session = AgentSession(name="test", cwd="/tmp", model="gpt-5.4")

        with (
            patch("axi.agents.make_stderr_callback", return_value=None),
            # Provide the proxy api key via env so the resolver doesn't try to
            # read the per-install token file (which doesn't exist in tests).
            patch.dict(os.environ, {"AXI_CHATGPT_PROXY_API_KEY": "test-token"}),
        ):
            options = _make_agent_options(session, resume_id=None)

        assert options.model is None
        assert options.env["ANTHROPIC_MODEL"] == "gpt-5.4"
        assert options.env["ANTHROPIC_BASE_URL"]
        assert options.env["ANTHROPIC_API_KEY"] == "test-token"

    def test_global_model_is_used_when_session_model_is_unset(self) -> None:
        session = AgentSession(name="test", cwd="/tmp")

        with (
            patch("axi.agents.make_stderr_callback", return_value=None),
            patch("axi.hub_wiring.config.get_model", return_value="sonnet"),
        ):
            options = _make_agent_options(session, resume_id=None)

        assert options.model == "sonnet"
        assert "ANTHROPIC_MODEL" not in options.env


class TestMakeAgentOptionsProviderFallback:
    def test_value_error_falls_back_to_auto_route(self) -> None:
        session = AgentSession(name="test", cwd="/tmp", model="gpt-5.4")

        calls = {"n": 0}

        def _resolve(model, provider=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError(f"Unknown provider '{provider}'")
            return "sonnet", {"ANTHROPIC_MODEL": "sonnet"}, "anthropic"

        with (
            patch("axi.agents.make_stderr_callback", return_value=None),
            patch("axi.hub_wiring.config.resolve_runtime", side_effect=_resolve) as mock_resolve,
        ):
            options = _make_agent_options(session, resume_id=None)

        assert options.model == "sonnet"
        assert options.env["ANTHROPIC_MODEL"] == "sonnet"
        # explicit-provider attempt failed, then the fallback re-resolved
        # without the provider (auto-route)
        assert mock_resolve.call_count == 2
        assert mock_resolve.call_args_list[1].kwargs.get("provider") is None


class TestMakeAgentOptionsDefaultProvider:
    def test_global_default_provider_used_when_session_provider_unset(self) -> None:
        session = AgentSession(name="test", cwd="/tmp")

        with (
            patch("axi.agents.make_stderr_callback", return_value=None),
            patch("axi.hub_wiring.config.get_model", return_value="sonnet"),
            patch("axi.hub_wiring.config.get_provider_default", return_value="ollama-local"),
            patch("axi.hub_wiring.config.resolve_runtime", return_value=("sonnet", {}, "ollama-local")) as mock_resolve,
        ):
            options = _make_agent_options(session, resume_id=None)

        assert options.model == "sonnet"
        # session.provider is None, so the global default provider is used
        assert mock_resolve.call_args_list[0].kwargs.get("provider") == "ollama-local"
