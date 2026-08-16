"""Tests for the mutator's evidence pack (config_evidence.py).

The mutator currently sees no empirical state at all. This assembles the
config-side analogue of find_blind_spots.py: folded idea history, the parent's
per-task weaknesses, and what has already been tried on this lineage.
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

RESULTS = (
    "timestamp\tparent_id\tidea_id\tcandidate_id\tcand_score\tparent_score\tkept\tcost_usd\n"
    "2026-08-16T01:00:00\tbaseline\ttried-a\tcand-1\t0.8000\t0.8000\tfalse\t\n"
    "2026-08-16T02:00:00\tbaseline\ttried-b\tcand-2\t0.9333\t0.8000\ttrue\t\n"
    "2026-08-16T03:00:00\tsomeone-else\ttried-c\tcand-3\t0.7000\t0.8000\tfalse\t\n"
)


@pytest.fixture
def ev(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORES_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    (tmp_path / "results.tsv").write_text(RESULTS, encoding="utf-8")
    bundle = tmp_path / "dict" / "configs" / "baseline"
    bundle.mkdir(parents=True)
    (bundle / "score.json").write_text(json.dumps({
        "aggregate": 0.80,
        "per_level": {"L2": {"per_task": {"sqrt-67-digits": 0.55, "sales-report": 1.0}}},
    }), encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps({
        "id": "baseline", "main_model": "claude-sonnet-5",
    }), encoding="utf-8")
    importlib.reload(importlib.import_module("ideas"))
    mod = importlib.reload(importlib.import_module("config_evidence"))
    yield mod, tmp_path
    for name in ("config_evidence", "ideas"):
        sys.modules.pop(name, None)


def test_evidence_reports_the_parents_weakest_task(ev):
    mod, _ = ev

    text = mod.build("baseline")

    assert "sqrt-67-digits" in text
    assert "0.55" in text


def test_evidence_lists_prior_experiments_on_this_lineage(ev):
    mod, _ = ev

    text = mod.build("baseline")

    assert "tried-a" in text
    assert "tried-b" in text


def test_evidence_excludes_experiments_from_other_lineages(ev):
    mod, _ = ev

    text = mod.build("baseline")

    assert "tried-c" not in text


def test_evidence_includes_the_folded_idea_pool(ev):
    mod, _ = ev
    ideas = sys.modules["ideas"]
    ideas.propose("a queued hypothesis", origin="secretary")

    text = mod.build("baseline")

    assert "a queued hypothesis" in text


def test_evidence_survives_a_parent_with_no_score(ev, tmp_path):
    mod, _ = ev
    (tmp_path / "dict" / "configs" / "unscored").mkdir(parents=True)

    text = mod.build("unscored")

    assert text.strip(), "must still produce usable context, not an empty string"


def test_evidence_survives_a_completely_empty_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORES_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    importlib.reload(importlib.import_module("ideas"))
    mod = importlib.reload(importlib.import_module("config_evidence"))

    text = mod.build("nobody")

    assert text.strip()
    sys.modules.pop("config_evidence", None)
    sys.modules.pop("ideas", None)


# --- null controls: a measured noise floor -------------------------------
#
# Two candidates were KEPT on changes that could not affect behaviour: one
# re-set main_model to the value the parent already had, the other edited
# manifest.spawn_block_models, a mirror field the evaluator never reads. Both
# "beat" their 0.80 parent. Suppressing them would hide the most honest thing
# the search has produced — an empirical read on how much of a score delta is
# noise. The evidence pack has to carry that, or the generator will keep
# reading sub-noise deltas as real effects.

NULL_RESULTS = (
    "timestamp\tparent_id\tidea_id\tcandidate_id\tcand_score\tparent_score\tkept\tcost_usd\n"
    "2026-08-16T09:58\tbaseline\tidea-main-haiku\tcand-A\t0.9333\t0.8000\ttrue\t\n"
    "2026-08-16T10:04\tbaseline\tidea-do-haiku\tcand-B\t0.9111\t0.8000\ttrue\t\n"
    "2026-08-16T10:30\tbaseline\treal-idea\tcand-C\t0.8600\t0.8000\ttrue\t\n"
)


def _with_null_controls(tmp_path):
    (tmp_path / "results.tsv").write_text(NULL_RESULTS, encoding="utf-8")
    (tmp_path / "null-controls.json").write_text(json.dumps({"controls": [
        {"candidate_id": "cand-A", "reason": "main_model re-set to the value the parent already had"},
        {"candidate_id": "cand-B", "reason": "edited manifest.spawn_block_models, which nothing reads"},
    ]}), encoding="utf-8")


def test_null_control_rows_are_labelled_in_the_lineage(ev, tmp_path):
    mod, _ = ev
    _with_null_controls(tmp_path)

    text = mod.build("baseline")

    line = next(ln for ln in text.splitlines() if "cand-A" in ln)
    assert "NULL CONTROL" in line


def test_the_measured_noise_floor_is_reported(ev, tmp_path):
    """Largest null-control gain over parent is 0.9333-0.8000 = 0.1333."""
    mod, _ = ev
    _with_null_controls(tmp_path)

    text = mod.build("baseline")

    assert "noise floor" in text.lower()
    assert "0.13" in text


def test_a_real_candidate_below_the_noise_floor_is_flagged(ev, tmp_path):
    """cand-C gained 0.06, under the 0.133 floor — not distinguishable."""
    mod, _ = ev
    _with_null_controls(tmp_path)

    line = next(ln for ln in mod.build("baseline").splitlines() if "cand-C" in ln)
    assert "below noise floor" in line.lower()


def test_no_null_controls_means_no_noise_claim(ev, tmp_path):
    """Never assert a floor that has not been measured."""
    mod, _ = ev
    (tmp_path / "results.tsv").write_text(NULL_RESULTS, encoding="utf-8")

    text = mod.build("baseline")

    assert "noise floor" not in text.lower()
    assert "NULL CONTROL" not in text


def test_the_idea_pool_also_marks_a_score_that_came_from_a_null_control(ev, tmp_path):
    """The lineage section is not the only place the number appears — the pool
    lists each idea's best score, and idea-main-haiku's best came from cand-A."""
    mod, _ = ev
    _with_null_controls(tmp_path)
    ideas = sys.modules["ideas"]
    ideas.propose("swap main_model to haiku", origin="human", idea_id="idea-main-haiku")
    ideas.complete("idea-main-haiku", "cand-A", 0.9333, True)

    line = next(ln for ln in mod.build("baseline").splitlines()
                if "idea-main-haiku" in ln and "tried" in ln)

    assert "null control" in line.lower()
