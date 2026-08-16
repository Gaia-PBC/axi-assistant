"""Tests for per-config provider routing under the config-directory layout.

A soul-config's model need not be Claude, and a MIXED soul (local main session
driving an Anthropic doer, or the reverse) has to work. ANTHROPIC_BASE_URL is
process-wide, so the main session's routing captures every spawn unless each
spawn carries its own env — which flowcoder honours as of 728d3f6, with an
empty override value meaning unset.

The resolved copy is what gets evaluated; the original is what gets committed,
so localhost URLs never end up stored in the dictionary.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "user-data/autoresearch/dict/configs/optimizer-seed/scripts"
)


@pytest.fixture
def rc(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    mod = importlib.import_module("resolve_config")
    yield importlib.reload(mod)
    sys.modules.pop("resolve_config", None)


def _config(tmp_path: Path, main: str, spawn: str | None = None) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config-meta.json").write_text(
        json.dumps({"model": main, "prompt_arg": "message"}), encoding="utf-8")
    (d / "SYSTEM_PROMPT.md").write_text("soul", encoding="utf-8")
    blocks = {
        "s": {"id": "s", "type": "start", "name": "START"},
        "e": {"id": "e", "type": "end", "name": "END"},
    }
    conns = [{"id": "c1", "source_block_id": "s", "target_block_id": "e",
              "source_port": "out", "target_port": "in"}]
    if spawn is not None:
        blocks["do"] = {"id": "do", "type": "spawn", "name": "DO", "agent_name": "doer",
                        "command_name": "helper", "model": spawn}
        conns = [
            {"id": "c1", "source_block_id": "s", "target_block_id": "do",
             "source_port": "out", "target_port": "in"},
            {"id": "c2", "source_block_id": "do", "target_block_id": "e",
             "source_port": "out", "target_port": "in"},
        ]
    (d / "flowchart-main.json").write_text(json.dumps(
        {"id": "m", "name": "m", "flowchart": {
            "start_block_id": "s", "blocks": blocks, "connections": conns}}), encoding="utf-8")
    return d


def _spawn_env(resolved: Path) -> dict:
    d = json.loads((resolved / "flowchart-main.json").read_text(encoding="utf-8"))
    return d["flowchart"]["blocks"]["do"].get("env", {})


# --- main-session routing -------------------------------------------------


def test_a_local_main_model_reports_the_provider_endpoint(rc, tmp_path):
    out = rc.resolve(_config(tmp_path, "nemotron-3.5-lightning"), tmp_path / "r")

    assert out["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:8199"
    assert out["env"]["ANTHROPIC_MODEL"] == "nemotron-3.5-lightning"


def test_an_anthropic_main_model_needs_no_env(rc, tmp_path):
    assert rc.resolve(_config(tmp_path, "claude-sonnet-5"), tmp_path / "r")["env"] == {}


def test_the_model_is_read_from_config_meta_not_a_manifest(rc, tmp_path):
    """manifest.json is lineage-only under the new layout."""
    d = _config(tmp_path, "nemotron-3.5-lightning")
    (d / "manifest.json").write_text(
        json.dumps({"id": "x", "main_model": "claude-sonnet-5"}), encoding="utf-8")

    assert rc.resolve(d, tmp_path / "r")["env"]["ANTHROPIC_MODEL"] == "nemotron-3.5-lightning"


# --- mixed souls ----------------------------------------------------------


def test_a_local_spawn_under_an_anthropic_main_gets_the_local_endpoint(rc, tmp_path):
    out = rc.resolve(_config(tmp_path, "claude-sonnet-5", "nemotron-3.5-lightning"),
                     tmp_path / "r")

    assert _spawn_env(Path(out["config_dir"]))["ANTHROPIC_BASE_URL"] == "http://localhost:8199"


def test_an_anthropic_spawn_under_a_local_main_is_unset_back_to_native(rc, tmp_path):
    """The bug the first end-to-end run exposed: without this the doer is sent
    to vLLM, which does not serve it, and the case scores low for a routing
    fault rather than a bad hypothesis."""
    out = rc.resolve(_config(tmp_path, "nemotron-3.5-lightning", "claude-opus-4-8"),
                     tmp_path / "r")

    env = _spawn_env(Path(out["config_dir"]))
    assert env["ANTHROPIC_BASE_URL"] == "", "empty value = unset (flowcoder _clean_env)"
    assert set(env) == set(out["env"]), "neutralise exactly what the main session set"


def test_an_all_anthropic_config_gets_no_spawn_env(rc, tmp_path):
    out = rc.resolve(_config(tmp_path, "claude-sonnet-5", "claude-opus-4-8"), tmp_path / "r")

    assert _spawn_env(Path(out["config_dir"])) == {}


def test_a_spawn_with_no_pinned_model_is_left_alone(rc, tmp_path):
    """It inherits the main session deliberately; injecting env would override
    an inheritance the config author chose."""
    d = _config(tmp_path, "nemotron-3.5-lightning", "claude-opus-4-8")
    fc = json.loads((d / "flowchart-main.json").read_text(encoding="utf-8"))
    del fc["flowchart"]["blocks"]["do"]["model"]
    (d / "flowchart-main.json").write_text(json.dumps(fc), encoding="utf-8")

    assert _spawn_env(Path(rc.resolve(d, tmp_path / "r")["config_dir"])) == {}


# --- the copy is what gets evaluated, the original is what gets committed --


def test_resolving_never_mutates_the_original(rc, tmp_path):
    d = _config(tmp_path, "nemotron-3.5-lightning", "claude-opus-4-8")
    before = (d / "flowchart-main.json").read_text(encoding="utf-8")

    rc.resolve(d, tmp_path / "r")

    assert (d / "flowchart-main.json").read_text(encoding="utf-8") == before


def test_the_resolved_copy_carries_the_whole_config_dir(rc, tmp_path):
    out = rc.resolve(_config(tmp_path, "claude-sonnet-5", "claude-opus-4-8"), tmp_path / "r")

    r = Path(out["config_dir"])
    assert (r / "config-meta.json").exists()
    assert (r / "SYSTEM_PROMPT.md").read_text(encoding="utf-8") == "soul"
    assert (r / "flowchart-main.json").exists()


def test_config_meta_keeps_only_its_two_legal_keys(rc, tmp_path):
    """agentconfig.META_KNOWN_KEYS is strict — an extra key fails the load."""
    out = rc.resolve(_config(tmp_path, "nemotron-3.5-lightning"), tmp_path / "r")

    meta = json.loads((Path(out["config_dir"]) / "config-meta.json").read_text(encoding="utf-8"))
    assert set(meta) <= {"model", "prompt_arg"}


def test_resolving_twice_is_idempotent(rc, tmp_path):
    d = _config(tmp_path, "nemotron-3.5-lightning", "claude-opus-4-8")
    first = rc.resolve(d, tmp_path / "r")
    a = (Path(first["config_dir"]) / "flowchart-main.json").read_text(encoding="utf-8")

    second = rc.resolve(d, tmp_path / "r")

    assert (Path(second["config_dir"]) / "flowchart-main.json").read_text(encoding="utf-8") == a


# --- env must scope to the child, not the caller's shell ------------------


def test_exec_scopes_the_env_to_the_child_process(tmp_path):
    """`--export` leaked routing into the caller's shell, where a host-side
    tool (the judge) picked up the local ANTHROPIC_API_KEY and lost its OAuth.
    The env must reach `evaluate` and nothing else."""
    import os
    import subprocess

    py = str(Path(__file__).resolve().parents[2] / ".venv/bin/python")
    cfg = _config_for_exec(tmp_path)
    r = subprocess.run(
        [py, str(SCRIPTS / "resolve_config.py"), str(cfg), str(tmp_path / "r"),
         "--exec", "--", "sh", "-c", "printenv GAIA_AGENT_ENV; printenv ANTHROPIC_BASE_URL || true"],
        capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"},
    )

    assert r.returncode == 0, r.stderr
    blob, _, rest = r.stdout.partition("\n")
    assert json.loads(blob)["ANTHROPIC_BASE_URL"] == "http://localhost:8199"
    assert rest.strip() == "", (
        "routing must NOT be set as raw vars — `evaluate` runs the judge in the same "
        "process and it needs the harness's own auth")
    assert "ANTHROPIC_BASE_URL" not in os.environ, "caller's env must be untouched"


def test_exec_propagates_the_child_exit_code(tmp_path):
    import subprocess

    py = str(Path(__file__).resolve().parents[2] / ".venv/bin/python")
    r = subprocess.run(
        [py, str(SCRIPTS / "resolve_config.py"), str(_config_for_exec(tmp_path)),
         str(tmp_path / "r"), "--exec", "--", "sh", "-c", "exit 3"],
        capture_output=True, text=True, timeout=60)

    assert r.returncode == 3


def _config_for_exec(tmp_path: Path) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config-meta.json").write_text(
        json.dumps({"model": "nemotron-3.5-lightning", "prompt_arg": "message"}),
        encoding="utf-8")
    (d / "flowchart-main.json").write_text(json.dumps(
        {"id": "m", "name": "m", "flowchart": {"start_block_id": "s", "blocks": {
            "s": {"id": "s", "type": "start", "name": "S"},
            "e": {"id": "e", "type": "end", "name": "E"}},
            "connections": [{"id": "c", "source_block_id": "s", "target_block_id": "e",
                             "source_port": "out", "target_port": "in"}]}}), encoding="utf-8")
    return d
