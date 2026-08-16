"""eval-case WRITE helper: combine run-result.json (cost/error) + quality.json
(judge scores) into a single case-result.json that reduce_scores.py aggregates.

Usage: write_case_result.py <workdir_abs> <task_id> <level>
Writes <workdir>/case-result.json and prints it.
"""
import json
import sys
from pathlib import Path


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main() -> int:
    wd, task_id, level = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    run = _load(wd / "run-result.json")
    qual = _load(wd / "quality.json")  # full QualityResult: {score, criteria:[{category,score,...}]}
    crit = {c.get("category"): c.get("score") for c in qual.get("criteria", [])}
    out = {
        "task": task_id,
        "level": level,
        "cost_usd": run.get("cost_usd"),
        "error": run.get("error"),
        "overall": qual.get("score"),
        "correctness": crit.get("correctness"),
        "verification": crit.get("verification"),
        "code_quality": crit.get("code_quality"),
    }
    (wd / "case-result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
