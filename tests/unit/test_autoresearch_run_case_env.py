"""Tests for provider routing in the eval path.

A soul-config's model need not be Claude. For a local (ollama/vLLM) model the
eval subprocess needs ANTHROPIC_BASE_URL + friends, or the run silently goes to
the Anthropic endpoint with an id it does not serve — and the candidate gets
discarded for what is actually a harness gap, not a bad hypothesis.
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
def run_case(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    mod = importlib.import_module("run_case")
    yield importlib.reload(mod)
    sys.modules.pop("run_case", None)


def _bundle(tmp_path: Path, model: str | None) -> Path:
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "manifest.json").write_text(
        json.dumps({"id": "b", "main_model": model, "flowchart_entry": "soul.json"}),
        encoding="utf-8")
    return b


def test_an_anthropic_model_needs_no_env_override(run_case, tmp_path):
    env = run_case.resolve_bundle_env(_bundle(tmp_path, "claude-sonnet-5"))

    assert env == {}


def test_a_local_model_gets_the_provider_endpoint(run_case, tmp_path):
    """nemotron-3.5-lightning is served only by the vllm provider, so
    resolve_runtime auto-routes it."""
    env = run_case.resolve_bundle_env(_bundle(tmp_path, "nemotron-3.5-lightning"))

    assert env.get("ANTHROPIC_BASE_URL") == "http://localhost:8199"
    assert env.get("ANTHROPIC_MODEL") == "nemotron-3.5-lightning"


def test_a_bundle_with_no_model_pinned_needs_no_env(run_case, tmp_path):
    assert run_case.resolve_bundle_env(_bundle(tmp_path, None)) == {}


def test_an_unknown_model_falls_through_to_anthropic_rather_than_raising(run_case, tmp_path):
    """resolve_runtime rule 5: no match -> native anthropic. A hallucinated id
    must not crash the lineage before the eval can reject it."""
    assert run_case.resolve_bundle_env(_bundle(tmp_path, "not-a-real-model-xyz")) == {}


def test_a_missing_manifest_does_not_crash(run_case, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    assert run_case.resolve_bundle_env(empty) == {}


def test_the_adapter_puts_the_resolved_env_on_the_subprocess(run_case, tmp_path, monkeypatch):
    """The load-bearing assertion: resolving the env is useless unless the
    adapter actually hands it to the engine subprocess."""
    import asyncio

    from gaia_testbench.adapters import flowcoder_cli as fc

    bundle = _bundle(tmp_path, "nemotron-3.5-lightning")
    (bundle / "flowchart").mkdir()
    (bundle / "kb").mkdir()
    cfg = fc.Config(
        axi_assistant_path=Path("/home/acer01/axi-assistant"),
        flowcoder_path=tmp_path / "fc",
        model="claude-sonnet-5",
        bundle_dir=bundle,
        env=run_case.resolve_bundle_env(bundle),
    )
    seen: dict = {}

    async def _fake_exec(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here — we only need the launch kwargs")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    task = tmp_path / "t"
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(fc.FlowcoderCliAdapter(cfg).run(
            type("T", (), {"id": "t", "prompt": "hi"})(), task, "no_soul_with_default"))

    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:8199"
    assert seen["env"]["ANTHROPIC_MODEL"] == "nemotron-3.5-lightning"
    assert "PATH" in seen["env"], "must extend os.environ, not replace it"
