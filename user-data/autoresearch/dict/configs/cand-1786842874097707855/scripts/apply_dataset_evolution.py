"""teacher APPLY helper: commit one benchmark evolution (IMPLEMENTATION.md §2.8).

Given a task id whose tasks/<id>.toml the EVOLVE step just wrote into
gaia-testbench, this:
  1. VALIDATES it loads via gaia-testbench's own get_task() (id==stem, [rubric]
     with the 3 criteria present) — a bad toml aborts here, nothing is bumped.
  2. APPENDS the id to benchmark-manifest.json `tasks` (idempotent).
  3. BUMPS `version` (seed -> v2 -> v3 ...) and appends a `history` entry.
  4. REGENERATES evaluate-config.json's fan-out to cover the new task set.
Prints the new benchmark version. Retiring a task instead of adding one:
pass --retire <id> (drops it from `tasks`, still bumps + regens).

Usage: apply_dataset_evolution.py <new_task_id> [--note TEXT]
       apply_dataset_evolution.py --retire <task_id> [--note TEXT]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
MANIFEST = REAL_AUTORES / "benchmark-manifest.json"
SCRIPTS = REAL_AUTORES / "dict/configs/optimizer-seed/scripts"
PY = "/home/acer01/axi-assistant/.venv/bin/python"
SOUL_BENCH = "/home/acer01/axi-assistant/user-data/soul-benchmarks"
REQUIRED_CRITERIA = {"correctness", "verification", "code_quality"}


def _next_version(v: str) -> str:
    if v == "seed":
        return "v2"
    m = re.fullmatch(r"v(\d+)", v or "")
    if m:
        return f"v{int(m.group(1)) + 1}"
    return f"{v}-next"


def _validate_task(task_id: str) -> None:
    """Load the task through gaia-testbench itself; raise SystemExit on any problem."""
    sys.path.insert(0, SOUL_BENCH)
    try:
        from gaia_testbench.tasks import get_task  # noqa: E402
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"apply_dataset_evolution: cannot import gaia-testbench: {e!r}")
    try:
        task = get_task(task_id)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"apply_dataset_evolution: tasks/{task_id}.toml failed to load: {e!r}")
    have = set(task.rubric.keys())
    missing = REQUIRED_CRITERIA - have
    if missing:
        raise SystemExit(f"apply_dataset_evolution: tasks/{task_id}.toml [rubric] missing {sorted(missing)} "
                         f"(has {sorted(have)}) — a task with no rubric is never judged")
    if not task.prompt.strip():
        raise SystemExit(f"apply_dataset_evolution: tasks/{task_id}.toml has an empty prompt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", help="new task id to add (tasks/<id>.toml must exist)")
    ap.add_argument("--retire", metavar="TASK_ID", help="retire (drop) a task from the benchmark instead")
    ap.add_argument("--note", default="", help="history note")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tasks: list[str] = list(manifest["tasks"])
    added, retired = [], []

    if args.retire:
        if args.retire not in tasks:
            raise SystemExit(f"apply_dataset_evolution: {args.retire!r} not in the benchmark; nothing to retire")
        tasks = [t for t in tasks if t != args.retire]
        retired = [args.retire]
    elif args.task_id:
        _validate_task(args.task_id)
        if args.task_id in tasks:
            print(f"apply_dataset_evolution: {args.task_id!r} already in the benchmark (no-op); version unchanged")
            print(manifest["version"])
            return 0
        tasks.append(args.task_id)
        added = [args.task_id]
    else:
        raise SystemExit("apply_dataset_evolution: give a task_id to add or --retire <id>")

    new_version = _next_version(manifest.get("version", "seed"))
    manifest["tasks"] = tasks
    manifest["version"] = new_version
    manifest.setdefault("history", []).append({
        "version": new_version, "tasks_added": added, "tasks_retired": retired, "note": args.note,
    })
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Regenerate evaluate-config's fan-out (validates internally; raises if invalid).
    out = subprocess.run([PY, str(SCRIPTS / "regen_evaluate_config.py")], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"apply_dataset_evolution: regen failed — {out.stdout}{out.stderr}")
    print(out.stdout.strip(), file=sys.stderr)
    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
