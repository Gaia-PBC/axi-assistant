"""Tests for the one-shot migration that seeds ideas/events.jsonl.

Reconstructs the event log by joining research-ideas.md (proposals) with
results.tsv (completions). The legacy markdown is known to be wrong about
every entry's state, so results.tsv is authoritative for what actually ran.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "user-data/autoresearch/scripts"
)

LEGACY_MD = """# Research Ideas — config-mutation queue

Some preamble prose that is not an entry.

- [~] idea-do-sonnet: Swap the spawn_do model to claude-sonnet-5.
- [x] idea-kb-concise: Add a directive to kb/SYSTEM_PROMPT.md.
- [ ] idea-main-haiku: Swap main_model to claude-haiku-4-5-20251001.
"""

LEGACY_TSV = (
    "timestamp\tparent_id\tidea_id\tcandidate_id\tcand_score\tparent_score\tkept\tcost_usd\n"
    "2026-08-15\tbaseline\tidea-do-sonnet\tcand-1\t0.8889\t0.8889\tfalse\t\n"
    "2026-08-15\tbaseline\tidea-main-haiku\tcand-2\t0.9333\t0.8000\ttrue\t\n"
    "2026-08-15\tbaseline\tidea-main-haiku\tcand-3\t0.9111\t0.8000\ttrue\t\n"
    "2026-08-15\tbaseline\torphan-idea\tcand-4\t0.7000\t0.8000\tfalse\t\n"
)


@pytest.fixture
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORES_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    (tmp_path / "research-ideas.md").write_text(LEGACY_MD, encoding="utf-8")
    (tmp_path / "results.tsv").write_text(LEGACY_TSV, encoding="utf-8")
    ideas = importlib.reload(importlib.import_module("ideas"))
    migrate = importlib.reload(importlib.import_module("migrate_ideas"))
    yield migrate, ideas, tmp_path
    for name in ("migrate_ideas", "ideas"):
        sys.modules.pop(name, None)


def test_migration_imports_every_markdown_entry(mods):
    migrate, ideas, _ = mods

    migrate.run()

    state = ideas.fold()
    assert "idea-do-sonnet" in state
    assert "idea-kb-concise" in state
    assert "idea-main-haiku" in state


def test_migration_keeps_the_original_idea_text(mods):
    migrate, ideas, _ = mods

    migrate.run()

    assert ideas.fold()["idea-do-sonnet"]["text"] == "Swap the spawn_do model to claude-sonnet-5."


def test_migration_ignores_preamble_prose(mods):
    migrate, ideas, _ = mods

    migrate.run()

    assert all(not k.startswith("Some") for k in ideas.fold())


def test_results_tsv_is_authoritative_over_the_markdown_marker(mods):
    """The md calls idea-main-haiku unclaimed; results.tsv shows it ran twice."""
    migrate, ideas, _ = mods

    migrate.run()

    entry = ideas.fold()["idea-main-haiku"]
    assert entry["state"] == "tried"
    assert len(entry["attempts"]) == 2


def test_an_idea_with_no_results_row_stays_future(mods):
    """The md marks idea-kb-concise [x], but nothing in results.tsv ran it."""
    migrate, ideas, _ = mods

    migrate.run()

    assert ideas.fold()["idea-kb-concise"]["state"] == "future"


def test_migration_synthesizes_a_proposal_for_an_orphan_results_row(mods):
    """orphan-idea is in results.tsv with no markdown entry."""
    migrate, ideas, _ = mods

    migrate.run()

    entry = ideas.fold()["orphan-idea"]
    assert entry["state"] == "tried"
    assert entry["attempts"][0]["candidate_id"] == "cand-4"


def test_migration_records_scores_and_kept_flags(mods):
    migrate, ideas, _ = mods

    migrate.run()

    attempt = ideas.fold()["idea-do-sonnet"]["attempts"][0]
    assert attempt["score"] == pytest.approx(0.8889)
    assert attempt["kept"] is False


def test_migration_refuses_to_run_over_an_existing_log(mods):
    migrate, ideas, tmp_path = mods
    migrate.run()
    before = (tmp_path / "ideas" / "events.jsonl").read_text(encoding="utf-8")

    with pytest.raises(migrate.AlreadyMigrated):
        migrate.run()

    assert (tmp_path / "ideas" / "events.jsonl").read_text(encoding="utf-8") == before


def test_migrated_proposals_are_attributed_to_human_origin(mods):
    migrate, ideas, _ = mods

    migrate.run()

    assert ideas.fold()["idea-do-sonnet"]["origin"] == "human"
