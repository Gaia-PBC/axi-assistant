"""Unit tests for the flowchart provider transform pass."""
import json
from unittest.mock import patch

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

    def test_stale_shadow_cleared_when_provider_removed(self, tmp_path, monkeypatch) -> None:
        src = tmp_path / "src"
        self._write_command(src, "soul-lite-do", {
            "start": {"id": "start", "type": "spawn", "name": "SPAWN",
                      "agent_name": "sl-do", "command_name": "do",
                      "model": "qwen3-coder:30b", "provider": "ollama-local"},
        })
        monkeypatch.setattr(flowchart_providers, "SHADOW_DIR", str(tmp_path / "shadow"))
        with patch("axi.flowchart_providers.resolve_runtime", return_value=(None, {
            "ANTHROPIC_BASE_URL": "http://localhost:11434",
            "ANTHROPIC_MODEL": "qwen3-coder:30b",
        }, "ollama-local")):
            shadow = flowchart_providers.transform_commands([str(src)])
        assert shadow == str(tmp_path / "shadow")
        assert (tmp_path / "shadow" / "soul-lite-do.json").exists()

        # Remove the provider block from the source and re-run: the stale
        # shadow copy must no longer serve the old env.
        self._write_command(src, "soul-lite-do", {
            "start": {"id": "start", "type": "spawn", "name": "S",
                      "agent_name": "a", "command_name": "do", "model": "opus"},
        })
        shadow = flowchart_providers.transform_commands([str(src)])
        assert shadow == ""
        assert not (tmp_path / "shadow" / "soul-lite-do.json").exists()

    def test_basename_collision_first_path_wins(self, tmp_path, monkeypatch) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        self._write_command(first, "do", {
            "start": {"id": "start", "type": "spawn", "name": "SPAWN",
                      "agent_name": "a", "command_name": "do",
                      "model": "qwen3-coder:30b", "provider": "ollama-local"},
        })
        self._write_command(second, "do", {
            "start": {"id": "start", "type": "spawn", "name": "S",
                      "agent_name": "a", "command_name": "do", "model": "opus"},
        })
        monkeypatch.setattr(flowchart_providers, "SHADOW_DIR", str(tmp_path / "shadow"))
        with patch("axi.flowchart_providers.resolve_runtime", return_value=(None, {
            "ANTHROPIC_BASE_URL": "http://localhost:11434",
            "ANTHROPIC_MODEL": "qwen3-coder:30b",
        }, "ollama-local")):
            shadow = flowchart_providers.transform_commands([str(first), str(second)])
        assert shadow == str(tmp_path / "shadow")
        out = json.loads((tmp_path / "shadow" / "do.json").read_text())
        block = out["flowchart"]["blocks"]["start"]
        # The shadow must hold the FIRST path's transformed version, matching
        # the engine's first-match-wins path-order resolution.
        assert "provider" not in block
        assert block["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
