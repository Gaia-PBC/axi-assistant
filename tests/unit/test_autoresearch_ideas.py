"""Tests for the autoresearch idea event log (optimizer-seed/scripts/ideas.py).

The idea store is an append-only JSONL event log; state is a fold over it, never
an in-place edit. Design: docs/superpowers/specs/2026-08-16-autoresearch-idea-generation-design.md
"""
from __future__ import annotations

import concurrent.futures as cf
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "user-data/autoresearch/scripts"
)


@pytest.fixture
def ideas(tmp_path, monkeypatch):
    """ideas.py with AUTORES_ROOT pointed at a throwaway dir."""
    monkeypatch.setenv("AUTORES_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    mod = importlib.import_module("ideas")
    importlib.reload(mod)  # drop any state cached under a previous AUTORES_ROOT
    yield mod
    sys.modules.pop("ideas", None)


def test_proposed_idea_is_in_future_state(ideas):
    idea_id = ideas.propose("Swap main_model to claude-haiku-4-5-20251001", origin="human")

    state = ideas.fold()

    assert state[idea_id]["state"] == "future"
    assert state[idea_id]["text"] == "Swap main_model to claude-haiku-4-5-20251001"
    assert state[idea_id]["origin"] == "human"


def test_propose_writes_one_line_per_idea(ideas, tmp_path):
    ideas.propose("first idea", origin="lineage-1")
    ideas.propose("second idea", origin="lineage-1")

    lines = (tmp_path / "ideas" / "events.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2


def test_idea_ids_are_unique_across_lineages_for_identical_text(ideas):
    a = ideas.propose("identical hypothesis", origin="lineage-1")
    b = ideas.propose("identical hypothesis", origin="lineage-2")

    assert a != b


def test_fold_of_empty_log_is_empty(ideas):
    assert ideas.fold() == {}


# --- state machine -------------------------------------------------------


def test_claim_moves_idea_from_future_to_present(ideas):
    idea_id = ideas.propose("swap the doer model", origin="human")

    assert ideas.claim(lineage="lineage-1", idea_id=idea_id) == idea_id
    assert ideas.fold()[idea_id]["state"] == "present"
    assert ideas.fold()[idea_id]["lineage"] == "lineage-1"


def test_claim_without_an_id_takes_the_oldest_future_idea(ideas):
    first = ideas.propose("first", origin="human")
    ideas.propose("second", origin="human")

    assert ideas.claim(lineage="lineage-1") == first


def test_claim_of_an_already_claimed_idea_fails(ideas):
    idea_id = ideas.propose("contested", origin="human")
    ideas.claim(lineage="lineage-1", idea_id=idea_id)

    assert ideas.claim(lineage="lineage-2", idea_id=idea_id) is None


def test_claim_on_empty_pool_returns_none(ideas):
    assert ideas.claim(lineage="lineage-1") is None


def test_claim_skips_ideas_that_are_not_future(ideas):
    done = ideas.propose("already done", origin="human")
    ideas.claim(lineage="lineage-1", idea_id=done)
    ideas.complete(idea_id=done, candidate_id="cand-1", score=0.9, kept=True)
    fresh = ideas.propose("still open", origin="human")

    assert ideas.claim(lineage="lineage-2") == fresh


def test_complete_moves_present_to_tried_and_records_the_attempt(ideas):
    idea_id = ideas.propose("test me", origin="lineage-1")
    ideas.claim(lineage="lineage-1", idea_id=idea_id)

    ideas.complete(idea_id=idea_id, candidate_id="cand-7", score=0.9111, kept=True)

    entry = ideas.fold()[idea_id]
    assert entry["state"] == "tried"
    assert entry["attempts"] == [{"candidate_id": "cand-7", "score": 0.9111, "kept": True}]


def test_one_idea_can_have_several_attempts(ideas):
    idea_id = ideas.propose("run twice", origin="human")
    ideas.complete(idea_id=idea_id, candidate_id="cand-a", score=0.80, kept=False)
    ideas.complete(idea_id=idea_id, candidate_id="cand-b", score=0.91, kept=True)

    assert len(ideas.fold()[idea_id]["attempts"]) == 2


def test_seeded_moves_idea_to_used_as_seed_linked_to_its_product(ideas):
    seed = ideas.propose("inspire me", origin="secretary")
    ideas.claim(lineage="lineage-1", idea_id=seed, purpose="seed")

    ideas.seeded(idea_id=seed, produced="generated-idea-abc123")

    entry = ideas.fold()[seed]
    assert entry["state"] == "used-as-seed"
    assert entry["produced"] == ["generated-idea-abc123"]


def test_retire_moves_idea_to_retired(ideas):
    idea_id = ideas.propose("bad idea", origin="lineage-3")

    ideas.retire(idea_id=idea_id, by="secretary", reason="duplicate")

    assert ideas.fold()[idea_id]["state"] == "retired"


def test_terminal_ops_resolve_last_wins_by_log_order(ideas):
    """A seed later tested on its own merits ends `tried`, not `used-as-seed`."""
    idea_id = ideas.propose("seed then test", origin="secretary")
    ideas.seeded(idea_id=idea_id, produced="derived-1")
    ideas.complete(idea_id=idea_id, candidate_id="cand-z", score=0.95, kept=True)

    assert ideas.fold()[idea_id]["state"] == "tried"


def test_retiring_after_completion_wins(ideas):
    idea_id = ideas.propose("tried then retired", origin="human")
    ideas.complete(idea_id=idea_id, candidate_id="cand-y", score=0.5, kept=False)
    ideas.retire(idea_id=idea_id, by="secretary", reason="superseded")

    assert ideas.fold()[idea_id]["state"] == "retired"


# --- the stale-claim reaper ---------------------------------------------


def test_a_claim_older_than_the_stale_threshold_folds_back_to_future(ideas, monkeypatch):
    monkeypatch.setenv("AUTORES_IDEA_STALE_SECONDS", "100")
    idea_id = ideas.propose("lineage will die holding this", origin="human")
    ideas.claim(lineage="lineage-1", idea_id=idea_id)

    fresh = ideas.fold()
    reaped = ideas.fold(now=_ts_of(ideas, idea_id) + 101)

    assert fresh[idea_id]["state"] == "present"
    assert reaped[idea_id]["state"] == "future"


def test_a_stale_claim_can_be_reclaimed(ideas, monkeypatch):
    monkeypatch.setenv("AUTORES_IDEA_STALE_SECONDS", "0")
    idea_id = ideas.propose("abandoned", origin="human")
    ideas.claim(lineage="lineage-1", idea_id=idea_id)

    assert ideas.claim(lineage="lineage-2", idea_id=idea_id) == idea_id


def test_a_completed_idea_is_never_reaped_as_stale(ideas, monkeypatch):
    monkeypatch.setenv("AUTORES_IDEA_STALE_SECONDS", "0")
    idea_id = ideas.propose("finished in time", origin="human")
    ideas.claim(lineage="lineage-1", idea_id=idea_id)
    ideas.complete(idea_id=idea_id, candidate_id="cand-q", score=0.7, kept=False)

    assert ideas.fold()[idea_id]["state"] == "tried"


def _ts_of(ideas, idea_id: str) -> float:
    """Epoch seconds of the most recent claim record for an idea."""
    claims = [e for e in ideas.read_events() if e.get("op") == "claim" and e["idea_id"] == idea_id]
    return claims[-1]["ts"]


# --- render + drift detection -------------------------------------------


def test_render_groups_ideas_by_state(ideas, tmp_path):
    future = ideas.propose("not tried yet", origin="human")
    done = ideas.propose("already run", origin="lineage-1")
    ideas.complete(idea_id=done, candidate_id="cand-1", score=0.88, kept=True)

    ideas.render()

    md = (tmp_path / "research-ideas.md").read_text(encoding="utf-8")
    assert "not tried yet" in md
    assert "already run" in md
    assert md.index("already run") < md.index("not tried yet"), "tried section precedes future"
    assert future in md
    assert done in md


def test_render_marks_the_file_as_generated(ideas, tmp_path):
    ideas.propose("anything", origin="human")
    ideas.render()

    assert "GENERATED" in (tmp_path / "research-ideas.md").read_text(encoding="utf-8")


def test_render_is_idempotent(ideas, tmp_path):
    ideas.propose("stable", origin="human")
    ideas.render()
    first = (tmp_path / "research-ideas.md").read_text(encoding="utf-8")
    ideas.render()

    assert (tmp_path / "research-ideas.md").read_text(encoding="utf-8") == first


def test_render_refuses_to_overwrite_a_hand_edited_file(ideas, tmp_path):
    ideas.propose("original", origin="human")
    ideas.render()
    (tmp_path / "research-ideas.md").write_text("a human edited this\n", encoding="utf-8")

    with pytest.raises(ideas.DriftError):
        ideas.render()

    assert (tmp_path / "research-ideas.md").read_text(encoding="utf-8") == "a human edited this\n"


def test_render_refuses_to_clobber_a_preexisting_unknown_file(ideas, tmp_path):
    """The migration case: a legacy hand-maintained research-ideas.md with no
    recorded render hash must not be silently destroyed."""
    (tmp_path / "research-ideas.md").write_text("# legacy hand-written queue\n", encoding="utf-8")
    ideas.propose("new", origin="human")

    with pytest.raises(ideas.DriftError):
        ideas.render()


def test_render_force_overwrites_drift(ideas, tmp_path):
    ideas.propose("original", origin="human")
    ideas.render()
    (tmp_path / "research-ideas.md").write_text("clobber me\n", encoding="utf-8")

    ideas.render(force=True)

    assert "original" in (tmp_path / "research-ideas.md").read_text(encoding="utf-8")


# --- CLI + real multi-process locking ------------------------------------
#
# These shell out so the flock is contended across genuinely separate
# processes. A same-process test would not prove the lock.

CLI = SCRIPTS / "ideas.py"
PY = str(Path(__file__).resolve().parents[2] / ".venv/bin/python")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(CLI), *args],
        env={**os.environ, "AUTORES_ROOT": str(root)},
        capture_output=True, text=True, timeout=60,
    )


def test_cli_propose_prints_the_new_idea_id(tmp_path):
    r = _run(tmp_path, "propose", "a brand new hypothesis", "--origin", "lineage-1")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()
    assert (tmp_path / "ideas" / "events.jsonl").exists()


def test_cli_claim_exits_nonzero_on_an_empty_pool(tmp_path):
    """The mutator branches on this to proceed unseeded."""
    r = _run(tmp_path, "claim", "--lineage", "lineage-1")

    assert r.returncode != 0


def test_cli_claim_exits_zero_and_prints_the_id_on_success(tmp_path):
    idea_id = _run(tmp_path, "propose", "claimable", "--origin", "human").stdout.strip()
    assert idea_id, "propose must print an id — otherwise this test passes vacuously"

    r = _run(tmp_path, "claim", "--lineage", "lineage-1")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == idea_id
    ops = [json.loads(ln)["op"] for ln in
           (tmp_path / "ideas" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ops == ["propose", "claim"]


def test_concurrent_claims_of_one_idea_yield_exactly_one_winner(tmp_path):
    idea_id = _run(tmp_path, "propose", "the contested idea", "--origin", "human").stdout.strip()

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: _run(tmp_path, "claim", "--lineage", f"lineage-{i}", "--idea-id", idea_id),
            range(8),
        ))

    winners = [r for r in results if r.returncode == 0]
    assert len(winners) == 1, f"{len(winners)} processes claimed the same idea"


def test_concurrent_claims_never_hand_the_same_idea_to_two_lineages(tmp_path):
    """8 racers, 4 ideas: every winner must hold a distinct idea."""
    for n in range(4):
        _run(tmp_path, "propose", f"idea number {n}", "--origin", "human")

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: _run(tmp_path, "claim", "--lineage", f"lineage-{i}"),
            range(8),
        ))

    claimed = [r.stdout.strip() for r in results if r.returncode == 0]
    assert len(claimed) == 4, f"expected 4 successful claims, got {len(claimed)}"
    assert len(set(claimed)) == 4, f"an idea was handed out twice: {claimed}"


def test_cli_show_json_reports_folded_state(tmp_path):
    _run(tmp_path, "propose", "visible idea", "--origin", "human")

    r = _run(tmp_path, "show", "--json")

    assert r.returncode == 0, r.stderr
    assert any(v["text"] == "visible idea" for v in json.loads(r.stdout).values())
