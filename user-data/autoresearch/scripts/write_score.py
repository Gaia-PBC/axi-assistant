"""evaluate-config WRITE helper: assemble the bundle's score.json VECTOR from the
per-level reduce-*.json files (PLAN.md App. B / IMPLEMENTATION.md 0 shape).

Usage: write_score.py <bundle_dir> <eval_scratch_abs> [benchmark_version]
Writes <bundle_dir>/score.json and prints the top-level aggregate.

aggregate = the highest-fidelity level available (axi if present, else L2), so a
config that cleared the L2 gate is scored by its real end-to-end result.

benchmark_version: the benchmark is global (shared by every bundle), so the
version is read from benchmark-manifest.json (the single source of truth the
teacher bumps) unless an explicit argv[3] override is given (tests). This means an
object-loop eval and a teacher rescore stamp the SAME version the manifest is on.
"""
import json
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")


def _manifest_version() -> str:
    try:
        m = json.loads((REAL_AUTORES / "benchmark-manifest.json").read_text(encoding="utf-8"))
        return m.get("version", "seed")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return "seed"


def main() -> int:
    bundle_dir = Path(sys.argv[1])
    eval_scratch = Path(sys.argv[2])
    benchmark_version = sys.argv[3] if len(sys.argv) > 3 else _manifest_version()

    per_level = {}
    for f in sorted(eval_scratch.glob("reduce-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        per_level[d["level"]] = d

    primary = per_level.get("axi") or per_level.get("L2")
    aggregate = primary.get("aggregate") if primary else None
    stderr = primary.get("stderr") if primary else None
    per_criterion = primary.get("per_criterion", {}) if primary else {}
    n_evals = sum(d.get("n_evals", 0) for d in per_level.values())
    cost = sum((d.get("cost_usd") or 0.0) for d in per_level.values())

    score = {
        "aggregate": aggregate,
        "per_criterion": per_criterion,
        "per_level": {
            lvl: {
                "aggregate": d.get("aggregate"),
                "per_criterion": d.get("per_criterion", {}),
                "per_task": d.get("per_task", {}),
                "cost_usd": d.get("cost_usd"),
                "n_evals": d.get("n_evals"),
                "stderr": d.get("stderr"),
            }
            for lvl, d in per_level.items()
        },
        "n_evals": n_evals,
        "stderr": stderr,
        "cost_usd": cost,
        "benchmark_version": benchmark_version,
    }
    (bundle_dir / "score.json").write_text(json.dumps(score, indent=2), encoding="utf-8")
    print(aggregate if aggregate is not None else "null")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
