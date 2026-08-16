"""One-shot migration: seed ideas/events.jsonl from the legacy files.

Joins the hand-maintained `research-ideas.md` (proposals) with `results.tsv`
(completions) on `idea_id`. The markdown's `[ ]`/`[~]`/`[x]` markers are NOT
trusted — as of 2026-08-16 it was wrong about all six of its entries (four
`[~]` that had finished, two `[ ]` that had run). results.tsv is the record of
what actually executed, so it decides state; the markdown only supplies text.

Usage: migrate_ideas.py [--render] [--dry-run]

Design: docs/superpowers/specs/2026-08-16-autoresearch-idea-generation-design.md
"""
from __future__ import annotations

import csv
import re
from typing import TYPE_CHECKING

import ideas
import tunables

if TYPE_CHECKING:
    from pathlib import Path

ENTRY_RE = re.compile(r"^- \[[ x~]\]\s*([^\s:]+):\s*(.*)$")


class AlreadyMigrated(RuntimeError):
    """events.jsonl already has content — migrating again would double every record."""


def parse_markdown(path: Path) -> list[tuple[str, str]]:
    """Return [(idea_id, text)] for real list entries, ignoring preamble prose."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        m = ENTRY_RE.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def parse_results(path: Path) -> list[dict]:
    """Return completion rows in file order."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows = []
    for row in csv.DictReader(raw.splitlines(), delimiter="\t"):
        idea_id = (row.get("idea_id") or "").strip()
        if not idea_id:
            continue
        score = (row.get("cand_score") or "").strip()
        rows.append({
            "idea_id": idea_id,
            "candidate_id": (row.get("candidate_id") or "").strip(),
            "score": float(score) if score else None,
            "kept": (row.get("kept") or "").strip().lower() == "true",
        })
    return rows


def run(dry_run: bool = False) -> dict:
    root = tunables.autores_root()
    if ideas.read_events():
        raise AlreadyMigrated(f"{ideas.events_path()} is not empty; refusing to double-import")

    entries = parse_markdown(root / "research-ideas.md")
    rows = parse_results(root / "results.tsv")

    known = {idea_id for idea_id, _ in entries}
    orphans = [r["idea_id"] for r in rows if r["idea_id"] not in known]
    # dict.fromkeys keeps first-seen order and de-dupes an idea that ran twice.
    orphans = list(dict.fromkeys(orphans))

    summary = {"proposed": len(entries), "orphans": len(orphans), "completed": len(rows)}
    if dry_run:
        return summary

    # Proposals first: `propose` resets an entry to `future`, so a completion
    # appended before its proposal would be overwritten by the fold.
    for idea_id, text in entries:
        ideas.propose(text, origin="human", idea_id=idea_id)
    for idea_id in orphans:
        ideas.propose(
            "(recovered from results.tsv — original text not in research-ideas.md)",
            origin="human", idea_id=idea_id,
        )
    for r in rows:
        ideas.complete(r["idea_id"], r["candidate_id"], r["score"], r["kept"])
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--render", action="store_true", help="also rewrite research-ideas.md")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    a = ap.parse_args(argv)

    try:
        summary = run(dry_run=a.dry_run)
    except AlreadyMigrated as e:
        print(f"migrate_ideas: {e}")
        return 1
    print(json.dumps(summary))
    if a.render and not a.dry_run:
        # force: the legacy markdown predates any render hash by definition.
        ideas.render(force=True)
        print(f"migrate_ideas: rendered {ideas.markdown_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
