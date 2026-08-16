"""evaluate-config REDUCE helper: aggregate one level's case-result.json files
into a reduce-<level>.json vector, and decide whether to cascade to L3.

Usage: reduce_scores.py <eval_scratch_abs> <level> [threshold]
Reads <eval_scratch>/case-*/case-result.json filtered to <level>, writes
<eval_scratch>/reduce-<level>.json {level, aggregate, per_criterion, per_task,
cost_usd, n_evals, stderr}. Prints run_l3 (1|0).

run_l3 = 1 only if aggregate > L2_THRESHOLD AND L3 is enabled. Both come from the
central tunables (env override > tunables.json > default): threshold =
tunables.l2_threshold() (default 0.7), enabled = tunables.l3_enabled() (default
False, i.e. L2-only scoring). An explicit argv[3] still wins as a test override.
Phase 1 left L3 disabled (always run_l3=0); Phase 3 enables it via tunables.json
(L3_ENABLED) once an L3 instance is booted, and tunes L2_THRESHOLD.
"""
import json
import statistics
import sys
from pathlib import Path

import tunables  # sibling module (script dir is on sys.path)

CRITERIA = ("correctness", "verification", "code_quality")


def main() -> int:
    eval_scratch = Path(sys.argv[1])
    level = sys.argv[2] if len(sys.argv) > 2 else "L2"
    # Explicit argv[3] wins (test override); else the tuned tunables value.
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else tunables.l2_threshold()
    l3_enabled = tunables.l3_enabled()

    results = []
    for cr in sorted(eval_scratch.glob("case-*/case-result.json")):
        try:
            d = json.loads(cr.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("level") == level:
            results.append(d)

    overalls = [d["overall"] for d in results if d.get("overall") is not None]
    aggregate = sum(overalls) / len(overalls) if overalls else None
    cost = sum((d.get("cost_usd") or 0.0) for d in results)
    per_criterion = {}
    for name in CRITERIA:
        vals = [d[name] for d in results if d.get(name) is not None]
        per_criterion[name] = (sum(vals) / len(vals)) if vals else None
    stderr = (statistics.pstdev(overalls) / (len(overalls) ** 0.5)) if len(overalls) > 1 else None

    out = {
        "level": level,
        "aggregate": aggregate,
        "per_criterion": per_criterion,
        "per_task": {d["task"]: d.get("overall") for d in results},
        "errors": {d["task"]: d["error"] for d in results if d.get("error")},
        "cost_usd": cost,
        "n_evals": len(results),
        "stderr": stderr,
    }
    (eval_scratch / f"reduce-{level.lower()}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    run_l3 = 1 if (aggregate is not None and aggregate > threshold and l3_enabled) else 0
    print(run_l3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
