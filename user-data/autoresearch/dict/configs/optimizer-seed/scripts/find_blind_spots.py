"""teacher READ helper: find the benchmark's blind spots (IMPLEMENTATION.md §2.8).

Scans the top-K scored TARGET bundles' per-task scores to find tasks where the
benchmark no longer discriminates — "where they all pass = no signal" — and lists
gaia-testbench tasks not yet in the benchmark (candidate held-out additions). The
teacher's EVOLVE step reads this to aim a new, harder/held-out task where the top
configs currently all pass (the anti-overfitting engine, PLAN.md §2e/§3.1).

Usage: find_blind_spots.py [--k K] [--saturated FLOAT] [--json]
Prints a human summary (default) or a JSON object (--json) with:
  benchmark_version, benchmark_tasks, top_k[], per_task{task: {scores, min, max,
  spread, saturated}}, saturated_tasks[], available_unused_tasks[].
A task is `saturated` when every top-K bundle scores >= --saturated (default 0.85)
on it — i.e. it can no longer separate good configs from better ones.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
sys.path.insert(0, str(REAL_AUTORES))
import derive_dictionary as dd  # noqa: E402

GAIA_TASKS_DIR = Path("/home/acer01/axi-assistant/user-data/soul-benchmarks/tasks")


def _per_task(bundle_dir: Path) -> dict[str, float]:
    try:
        score = json.loads((bundle_dir / "score.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Prefer the highest-fidelity level's per_task, else L2's.
    per_level = score.get("per_level", {})
    lvl = per_level.get("axi") or per_level.get("L2") or {}
    return lvl.get("per_task", {}) or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3, help="top-K scored target bundles to inspect")
    ap.add_argument("--saturated", type=float, default=0.85,
                    help="a task is non-discriminating if every top-K bundle scores >= this")
    ap.add_argument("--dict-dir", type=Path, default=REAL_AUTORES / "dict" / "configs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    manifest = json.loads((REAL_AUTORES / "benchmark-manifest.json").read_text(encoding="utf-8"))
    bench_tasks: list[str] = manifest["tasks"]

    rows = [r for r in dd.scan(args.dict_dir) if r["scored"]]
    rows.sort(key=lambda r: -(r["aggregate"] or 0.0))
    top = rows[: args.k]
    top_pt = {r["id"]: _per_task(args.dict_dir / r["id"]) for r in top}

    per_task = {}
    for t in bench_tasks:
        scores = {bid: pt.get(t) for bid, pt in top_pt.items() if pt.get(t) is not None}
        vals = list(scores.values())
        entry = {
            "scores": scores,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "spread": (max(vals) - min(vals)) if vals else None,
            "saturated": bool(vals) and all(v >= args.saturated for v in vals),
        }
        per_task[t] = entry

    saturated = [t for t, e in per_task.items() if e["saturated"]]
    available = sorted(p.stem for p in GAIA_TASKS_DIR.glob("*.toml"))
    unused = [t for t in available if t not in bench_tasks]

    result = {
        "benchmark_version": manifest.get("version"),
        "benchmark_tasks": bench_tasks,
        "top_k": [{"id": r["id"], "aggregate": r["aggregate"]} for r in top],
        "per_task": per_task,
        "saturated_tasks": saturated,
        "available_unused_tasks": unused,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"benchmark_version = {result['benchmark_version']}  ({len(bench_tasks)} tasks)")
    print(f"top-{args.k} scored targets: " + ", ".join(f"{r['id']}={r['aggregate']:.3f}" for r in top))
    print("\nper-task discrimination across the top configs:")
    for t, e in per_task.items():
        flag = "  <== SATURATED (all pass, no signal)" if e["saturated"] else ""
        sp = f"spread={e['spread']:.3f}" if e["spread"] is not None else "spread=-"
        print(f"  {t:32s} min={e['min']} max={e['max']} {sp}{flag}")
    print(f"\nsaturated (non-discriminating) tasks: {saturated or '(none)'}")
    print(f"gaia-testbench tasks available but NOT in the benchmark (held-out candidates):")
    for t in unused:
        print(f"  - {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
