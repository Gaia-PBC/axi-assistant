"""Tests for how an experiment's outcome is recorded.

Two behaviours the mutator-generated-ideas design depends on:
  * results.tsv rows carry a REAL timestamp (the evidence pack reasons over
    "what was tried and when"), not a hardcoded literal.
  * the candidate's own manifest.json is authoritative for idea_id, so a
    mutator that mints its own idea can report it upward.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "user-data/autoresearch/dict/configs/optimizer-seed/scripts"
PY = str(ROOT / ".venv/bin/python")


def _run(script: str, root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(SCRIPTS / script), *args],
        env={**os.environ, "AUTORES_ROOT": str(root)},
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def exp(tmp_path):
    """An experiment dir laid out the way run-experiment builds it."""
    exp_dir = tmp_path / "scratch" / "exp-1"
    out_dir = exp_dir / "cand-1"
    out_dir.mkdir(parents=True)
    (exp_dir / "decision.json").write_text(json.dumps({
        "candidate_id": "cand-1", "parent_id": "baseline", "idea_id": "some-idea",
        "cand_score": 0.9, "parent_score": 0.8, "kept": True,
    }), encoding="utf-8")
    return tmp_path, out_dir


def test_recorded_timestamp_is_the_real_time(exp):
    root, out_dir = exp

    r = _run("record_experiment.py", root, str(out_dir))

    assert r.returncode == 0, r.stderr
    row = (root / "results.tsv").read_text(encoding="utf-8").splitlines()[-1]
    stamp = row.split("\t")[0]
    assert stamp != "2026-08-15", "timestamp is still the hardcoded literal"
    parsed = dt.datetime.fromisoformat(stamp)
    assert abs((dt.datetime.now() - parsed).total_seconds()) < 120  # noqa: DTZ005 — naive, as written


def test_explicit_timestamp_argument_still_wins(exp):
    root, out_dir = exp

    _run("record_experiment.py", root, str(out_dir), "2020-01-01T00:00:00")

    row = (root / "results.tsv").read_text(encoding="utf-8").splitlines()[-1]
    assert row.split("\t")[0] == "2020-01-01T00:00:00"


# --- idea_id flows up through the candidate manifest ---------------------


def _score(path: Path, aggregate: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aggregate": aggregate}), encoding="utf-8")


def test_candidate_manifest_idea_id_beats_the_positional_arg(tmp_path):
    out_dir = tmp_path / "scratch" / "exp-1" / "cand-1"
    _score(out_dir / "score.json", 0.95)
    (out_dir / "manifest.json").write_text(
        json.dumps({"id": "cand-1", "idea_id": "minted-by-the-mutator"}), encoding="utf-8")
    _score(tmp_path / "dict" / "configs" / "baseline" / "score.json", 0.80)

    r = _run("compare_scores.py", tmp_path, str(out_dir), "baseline", "passed-in-idea")

    assert r.returncode == 0, r.stderr
    decision = json.loads((out_dir.parent / "decision.json").read_text(encoding="utf-8"))
    assert decision["idea_id"] == "minted-by-the-mutator"


def test_positional_idea_id_is_used_when_the_manifest_has_none(tmp_path):
    out_dir = tmp_path / "scratch" / "exp-1" / "cand-1"
    _score(out_dir / "score.json", 0.95)
    (out_dir / "manifest.json").write_text(json.dumps({"id": "cand-1"}), encoding="utf-8")
    _score(tmp_path / "dict" / "configs" / "baseline" / "score.json", 0.80)

    _run("compare_scores.py", tmp_path, str(out_dir), "baseline", "passed-in-idea")

    decision = json.loads((out_dir.parent / "decision.json").read_text(encoding="utf-8"))
    assert decision["idea_id"] == "passed-in-idea"


def test_missing_manifest_does_not_break_the_decision(tmp_path):
    out_dir = tmp_path / "scratch" / "exp-1" / "cand-1"
    _score(out_dir / "score.json", 0.95)
    _score(tmp_path / "dict" / "configs" / "baseline" / "score.json", 0.80)

    r = _run("compare_scores.py", tmp_path, str(out_dir), "baseline", "passed-in-idea")

    assert r.returncode == 0, r.stderr
    decision = json.loads((out_dir.parent / "decision.json").read_text(encoding="utf-8"))
    assert decision["idea_id"] == "passed-in-idea"
    assert decision["kept"] is True
