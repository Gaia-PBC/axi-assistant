"""Tests for the autoresearch model allowlist (models_lib / refresh_models).

A soul-config's model_set is a knob, and the model need not be Claude — the loop
optimizes Axi's soul, whatever serves it. So the allowlist has to carry the
provider, and regenerating it must never silently drop an id the search is
already using.
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
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORES_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    ml = importlib.reload(importlib.import_module("models_lib"))
    monkeypatch.setattr(ml, "REAL_AUTORES", tmp_path)
    yield ml, tmp_path
    sys.modules.pop("models_lib", None)


def _write_models(root: Path, models: list[dict]) -> None:
    (root / "models.json").write_text(
        json.dumps({"models": models, "candidates": []}), encoding="utf-8")


def test_render_names_the_provider_for_each_model(mods):
    ml, root = mods
    _write_models(root, [
        {"id": "claude-sonnet-5", "provider": "anthropic", "tier": "sonnet", "rel_cost": 5},
        {"id": "nemotron-3.5-lightning", "provider": "vllm", "tier": "local", "rel_cost": 0},
    ])

    text = ml.render_for_prompt()

    assert "anthropic" in text
    assert "vllm" in text
    assert "nemotron-3.5-lightning" in text


def test_render_marks_local_models_so_the_mutator_can_reason_about_cost(mods):
    ml, root = mods
    _write_models(root, [
        {"id": "nemotron-3.5-lightning", "provider": "vllm", "tier": "local", "rel_cost": 0},
    ])

    assert "rel_cost=0" in ml.render_for_prompt()


def test_a_local_model_is_a_valid_allowlist_entry(mods):
    ml, root = mods
    _write_models(root, [{"id": "nemotron-3.5-lightning", "provider": "vllm"}])

    assert ml.is_valid("nemotron-3.5-lightning")
    assert not ml.is_valid("some-hallucinated-model")


def test_provider_for_returns_the_declared_provider(mods):
    ml, root = mods
    _write_models(root, [
        {"id": "claude-sonnet-5", "provider": "anthropic"},
        {"id": "nemotron-3.5-lightning", "provider": "vllm"},
    ])

    assert ml.provider_for("nemotron-3.5-lightning") == "vllm"
    assert ml.provider_for("claude-sonnet-5") == "anthropic"
    assert ml.provider_for("unknown") is None


# --- regeneration must not destroy the search's own history --------------


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORES_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    mod = importlib.reload(importlib.import_module("refresh_models"))
    monkeypatch.setattr(mod, "REAL_AUTORES", tmp_path)
    monkeypatch.setattr(mod, "MODELS_JSON", tmp_path / "models.json")
    yield mod, tmp_path
    sys.modules.pop("refresh_models", None)


def test_in_use_ids_survive_a_regeneration_that_no_longer_discovers_them(refresh):
    """The registry's hardcoded table lists claude-sonnet-4-5, not claude-sonnet-5 —
    but every experiment in results.tsv ran on claude-sonnet-5. Dropping it would
    invalidate the allowlist against the loop's own history."""
    mod, root = refresh
    (root / "models.json").write_text(json.dumps({"models": [
        {"id": "claude-sonnet-5", "provider": "anthropic", "tier": "sonnet", "rel_cost": 5},
    ], "candidates": []}), encoding="utf-8")
    (root / "dict" / "configs" / "cand-a").mkdir(parents=True)
    (root / "dict" / "configs" / "cand-a" / "manifest.json").write_text(
        json.dumps({"id": "cand-a", "main_model": "claude-sonnet-5"}), encoding="utf-8")

    mod.regenerate(discovered={"anthropic": [{"id": "claude-sonnet-4-5"}]})

    ids = {m["id"] for m in json.loads((root / "models.json").read_text())["models"]}
    assert "claude-sonnet-5" in ids, "dropped an id the dictionary is still using"
    assert "claude-sonnet-4-5" in ids


def test_regeneration_stamps_the_provider_on_every_model(refresh):
    mod, root = refresh

    mod.regenerate(discovered={
        "anthropic": [{"id": "claude-opus-4-8"}],
        "vllm": [{"id": "nemotron-3.5-lightning", "context_window": 131072}],
    })

    models = {m["id"]: m for m in json.loads((root / "models.json").read_text())["models"]}
    assert models["claude-opus-4-8"]["provider"] == "anthropic"
    assert models["nemotron-3.5-lightning"]["provider"] == "vllm"


def test_regeneration_preserves_hand_annotations(refresh):
    mod, root = refresh
    (root / "models.json").write_text(json.dumps({"models": [
        {"id": "claude-opus-4-8", "provider": "anthropic", "tier": "opus", "rel_cost": 15},
    ], "candidates": []}), encoding="utf-8")

    mod.regenerate(discovered={"anthropic": [{"id": "claude-opus-4-8"}]})

    m = json.loads((root / "models.json").read_text())["models"][0]
    assert m["rel_cost"] == 15
    assert m["tier"] == "opus"


def test_a_retained_legacy_entry_still_gets_a_provider(refresh):
    """The pre-registry models.json had no `provider` key. A retained entry from
    that format must still be stamped, or provider_for()/render report '?' for a
    model the loop is actively using. Free-form ids route to anthropic
    (resolve_runtime rule 5), so that is the correct default."""
    mod, root = refresh
    (root / "models.json").write_text(json.dumps({"models": [
        {"id": "claude-sonnet-5", "tier": "sonnet", "rel_cost": 5},  # no provider
    ], "candidates": []}), encoding="utf-8")
    (root / "dict" / "configs" / "cand-a").mkdir(parents=True)
    (root / "dict" / "configs" / "cand-a" / "manifest.json").write_text(
        json.dumps({"id": "cand-a", "main_model": "claude-sonnet-5"}), encoding="utf-8")

    mod.regenerate(discovered={"anthropic": [{"id": "claude-opus-4-8"}]})

    models = {m["id"]: m for m in json.loads((root / "models.json").read_text())["models"]}
    assert models["claude-sonnet-5"]["provider"] == "anthropic"


def test_bare_tier_aliases_are_not_allowlisted(refresh):
    """`sonnet` is not a reproducible experiment value — the alias remaps over
    time, so a lineage attributed to it stops meaning anything. They belong in
    `candidates`, not in the list the mutator may propose from."""
    mod, root = refresh

    mod.regenerate(discovered={"anthropic": [
        {"id": "sonnet"}, {"id": "opus"}, {"id": "haiku"}, {"id": "claude-opus-4-8"},
    ]})

    data = json.loads((root / "models.json").read_text())
    allowed = {m["id"] for m in data["models"]}
    assert allowed == {"claude-opus-4-8"}
    assert {c["id"] for c in data["candidates"]} >= {"sonnet", "opus", "haiku"}


def test_an_unannotated_anthropic_id_gets_a_usable_tier_and_cost(refresh):
    """rel_cost=None defeats the quality-per-cost reasoning the prompt asks for."""
    mod, root = refresh

    mod.regenerate(discovered={"anthropic": [
        {"id": "claude-haiku-4-5"}, {"id": "claude-sonnet-4-5"}, {"id": "claude-opus-4-8"},
    ]})

    m = {x["id"]: x for x in json.loads((root / "models.json").read_text())["models"]}
    assert (m["claude-haiku-4-5"]["tier"], m["claude-haiku-4-5"]["rel_cost"]) == ("haiku", 1)
    assert (m["claude-sonnet-4-5"]["tier"], m["claude-sonnet-4-5"]["rel_cost"]) == ("sonnet", 5)
    assert (m["claude-opus-4-8"]["tier"], m["claude-opus-4-8"]["rel_cost"]) == ("opus", 15)
    assert all(x["rel_cost"] is not None for x in m.values())


def test_a_provider_that_failed_discovery_does_not_erase_its_models(refresh):
    """ollama being down must not silently shrink the search space."""
    mod, root = refresh
    (root / "models.json").write_text(json.dumps({"models": [
        {"id": "deepseek-r1:70b", "provider": "ollama-local", "tier": "local"},
    ], "candidates": []}), encoding="utf-8")

    mod.regenerate(discovered={"ollama-local": {"error": "Connection refused"},
                               "anthropic": [{"id": "claude-opus-4-8"}]})

    ids = {m["id"] for m in json.loads((root / "models.json").read_text())["models"]}
    assert "deepseek-r1:70b" in ids
