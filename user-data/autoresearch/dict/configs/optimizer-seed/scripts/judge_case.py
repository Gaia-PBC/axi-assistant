"""eval-case JUDGE helper: score a run's artifact with gaia-testbench's LLM judge
(the 1-3 multi-criterion rubric; model = $GAIA_JUDGE_MODEL or claude-opus-5).

Usage: judge_case.py <workdir_abs> <task_id>
Writes <workdir_abs>/quality.json (full QualityResult) and prints a compact JSON
{overall, <criterion>: score, ...} to stdout. Per-criterion score is 1|2|3 or
null (a criterion the judge skipped, e.g. verification with no trace).
"""
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/acer01/axi-assistant/user-data/soul-benchmarks")
from gaia_testbench.tasks import get_task  # noqa: E402
from gaia_testbench import quality  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: judge_case.py <workdir_abs> <task_id>")
    workdir, task_id = sys.argv[1:3]
    wd = Path(workdir)
    try:
        task = get_task(task_id)
        qr = asyncio.run(quality.score_artifact(wd, task.prompt, task.rubric))
    except Exception:
        import traceback
        (wd / "judge_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    (wd / "quality.json").write_text(json.dumps(dataclasses.asdict(qr), indent=2), encoding="utf-8")
    out = {"overall": qr.score}
    for c in qr.criteria:
        out[c.category] = c.score
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
