"""teacher RESCORE helper: re-stamp benchmark_version onto a bundle's score.json.

gaia-testbench used to write `benchmark_version` into score.json. It no longer
does (origin/main 9429814): `evaluate batch` emits aggregate / per_criterion /
per_task / errors / n_evals / solved_rate / n_solved / n_validated / stderr /
cost_usd, and nothing else. RESCORE_TOPK_* copies that file over the bundle's
own, so without this the version APPLY_EVOLUTION just bumped disappears from
the very bundles it was bumped for -- and a score with no version cannot be
told apart from a score taken against a different task set.

Idempotent, and deliberately additive: it only inserts the key, never edits a
number gaia produced.

Usage: stamp_score_version.py <score.json> <benchmark-manifest.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def stamp(score_path: Path, manifest_path: Path) -> str:
    """Write the manifest's version into score.json. Returns the version."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not version:
        raise SystemExit(f"stamp_score_version: {manifest_path} has no 'version'")

    score = json.loads(score_path.read_text(encoding="utf-8"))
    if not isinstance(score, dict):
        raise SystemExit(
            f"stamp_score_version: {score_path} is {type(score).__name__}, expected an object"
        )
    score["benchmark_version"] = version
    # The task list too: `benchmark_version` alone says which version was
    # current, not which tasks the number was actually computed over. When the
    # two disagree -- a rescore that raced a bump -- the reader needs to see it
    # rather than infer it.
    score["benchmark_tasks"] = list(manifest.get("tasks", []))
    score_path.write_text(json.dumps(score, indent=2) + "\n", encoding="utf-8")
    return version


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: stamp_score_version.py <score.json> <benchmark-manifest.json>")
    score_path, manifest_path = Path(sys.argv[1]), Path(sys.argv[2])
    if not score_path.is_file():
        raise SystemExit(f"stamp_score_version: no such file {score_path}")
    if not manifest_path.is_file():
        raise SystemExit(f"stamp_score_version: no such file {manifest_path}")
    print(stamp(score_path, manifest_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
