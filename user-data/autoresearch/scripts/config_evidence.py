"""Assemble the mutator's evidence pack (mutate-config §2a).

The config-side analogue of find_blind_spots.py. The teacher's EVOLVE step gets
a blind-spot scan to aim at; the mutator historically got nothing, so it could
only apply an idea handed to it. This supplies the three things a generated
hypothesis has to be grounded in:

  1. the folded idea pool — what has been tried, what it scored, what is queued
  2. the parent's per-task scores — where THIS config is actually weak
  3. this lineage's prior experiments — what has already been attempted here

Usage: config_evidence.py <parent_id>

Design: docs/superpowers/specs/2026-08-16-autoresearch-idea-generation-design.md
"""
from __future__ import annotations

import csv
import json
from typing import TYPE_CHECKING

import ideas
import tunables

if TYPE_CHECKING:
    from pathlib import Path

MAX_TRIED = 25      # most recent tried ideas to show
MAX_QUEUED = 15
MAX_LINEAGE = 20


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _per_task(bundle: Path) -> dict[str, float]:
    """Prefer the highest-fidelity level's per_task, else L2's (same rule as
    find_blind_spots.py)."""
    per_level = _read_json(bundle / "score.json").get("per_level", {})
    lvl = per_level.get("axi") or per_level.get("L2") or {}
    return lvl.get("per_task", {}) or {}


def null_controls(root: Path) -> dict[str, str]:
    """Candidates whose edit could not have changed behaviour, {id: reason}.

    Accidental but valuable: the search produced these by mutating fields the
    evaluator never reads. Each is a null control, and the spread they show
    over their parent is a measured noise floor for the benchmark. Suppressing
    them would hide the most honest signal the loop has generated.
    """
    data = _read_json(root / "null-controls.json")
    return {c["candidate_id"]: c.get("reason", "")
            for c in data.get("controls", []) if c.get("candidate_id")}


def _delta(row: dict) -> float | None:
    try:
        return float(row["cand_score"]) - float(row["parent_score"])
    except (KeyError, TypeError, ValueError):
        return None


def noise_floor(rows: list[dict], controls: dict[str, str]) -> float | None:
    """Largest gain a known no-op achieved. None if nothing was measured —
    never assert a floor that has not been observed."""
    gains = [d for r in rows if r.get("candidate_id") in controls
             for d in [_delta(r)] if d is not None]
    return max(gains) if gains else None


def _lineage_rows(root: Path, parent_id: str) -> list[dict]:
    try:
        raw = (root / "results.tsv").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [r for r in csv.DictReader(raw.splitlines(), delimiter="\t")
            if (r.get("parent_id") or "").strip() == parent_id]


def build(parent_id: str) -> str:
    root = tunables.autores_root()
    bundle = root / "dict" / "configs" / parent_id
    out: list[str] = []

    # 1. parent's own standing
    score = _read_json(bundle / "score.json")
    manifest = _read_json(bundle / "manifest.json")
    agg = score.get("aggregate")
    out.append(f"### This config ({parent_id})")
    out.append(f"- aggregate: {agg if agg is not None else 'UNSCORED'}"
               f"  ·  main_model: {manifest.get('main_model') or '(inherits default)'}")
    per_task = _per_task(bundle)
    if per_task:
        out.append("- per-task scores (lowest first — these are where it is weak):")
        for task, val in sorted(per_task.items(), key=lambda kv: kv[1]):
            out.append(f"    {val:.2f}  {task}")
    else:
        out.append("- per-task scores: none recorded yet")
    out.append("")

    # 2. what this lineage already tried
    rows = _lineage_rows(root, parent_id)
    controls = null_controls(root)
    floor = noise_floor(rows, controls)
    out.append(f"### Already tried from this parent ({len(rows)})")
    if rows:
        for r in rows[-MAX_LINEAGE:]:
            cid = r.get("candidate_id")
            kept = "KEPT" if (r.get("kept") or "").lower() == "true" else "discarded"
            note = ""
            if cid in controls:
                note = f"  <== NULL CONTROL: {controls[cid]}"
            elif floor is not None:
                d = _delta(r)
                if d is not None and d <= floor:
                    note = "  (below noise floor — not distinguishable from a no-op)"
            out.append(f"- {r.get('idea_id') or '(no id)'} -> {r.get('cand_score') or '?'} "
                       f"({kept}, {cid}){note}")
        out.append("Do NOT re-propose one of these unchanged.")
    else:
        out.append("- nothing yet; this parent is unexplored")
    if floor is not None:
        out.append("")
        out.append(f"**Measured noise floor: {floor:.4f}.** That is the largest gain a "
                   f"candidate achieved on a change that provably could not affect behaviour "
                   f"(see NULL CONTROL rows above). A delta at or under it is not evidence of "
                   f"an effect, however tempting the story. Prefer a hypothesis whose predicted "
                   f"effect is comfortably larger, and treat a past 'win' below this line as "
                   f"unproven rather than as prior art.")
    out.append("")

    # 3. the shared idea pool
    state = ideas.fold()
    tried = [e for e in state.values() if e["state"] == "tried"]
    queued = [e for e in state.values() if e["state"] == "future"]
    inflight = [e for e in state.values() if e["state"] == "present"]
    out.append(f"### Idea pool — {len(tried)} tried, {len(inflight)} in flight, "
               f"{len(queued)} queued")
    def _best(entry: dict) -> str:
        """The pool lists each idea's best score, so a best that came from a
        null control has to say so here too — otherwise the number reads as
        prior art in the one section that does not carry the lineage note."""
        scored = [a for a in entry["attempts"] if a.get("score") is not None]
        if not scored:
            return "unscored"
        best = max(scored, key=lambda a: a["score"])
        label = f"{best['score']:.4f}"
        if best.get("candidate_id") in controls:
            label += ", NULL CONTROL — no-op, not evidence"
        return label

    out.extend(f"- [tried {_best(e)}] {e['idea_id']}: {e['text']}" for e in tried[-MAX_TRIED:])
    out.extend(f"- [queued, {e['origin'] or 'unknown'}] {e['idea_id']}: {e['text']}"
               for e in queued[:MAX_QUEUED])
    if not tried and not queued:
        out.append("- pool is empty; you are generating from scratch")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("parent_id")
    a = ap.parse_args(argv)
    print(build(a.parent_id), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
