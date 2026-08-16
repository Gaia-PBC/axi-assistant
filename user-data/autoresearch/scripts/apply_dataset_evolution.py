"""teacher APPLY helper: commit one benchmark evolution (IMPLEMENTATION.md §2.8).

Given a task id whose tasks/<id>/task.toml the EVOLVE step just wrote into
gaia-testbench, this:
  1. VALIDATES it loads via gaia-testbench's own get_task() (id==stem, [rubric]
     present and non-empty) — a bad toml aborts here, nothing is bumped.
  1b. VALIDATES the deterministic check files WRITE_CHECKS may have written
     alongside it: answer_schema.json parses and looks like a schema,
     validator.py compiles, [[validators]] have runnable `run` strings, and a
     `$HIDDEN` reference actually has a hidden/ directory to resolve to. This
     exists because the alternative discovery point is RESCORE, which costs
     $40 per target — a syntax error in validator.py must not be found by
     spending $80.
  2. APPENDS the id to benchmark-manifest.json `tasks` (idempotent).
  3. BUMPS `version` (seed -> v2 -> v3 ...) and appends a `history` entry.

Check files are validated by PATH, not through Task attributes. `answer_schema`
and `validator_script` are properties of the feat/implement-validation branch
only, and this script has to keep working against whichever gaia checkout
SOUL_BENCH happens to point at.
There is no fan-out to regenerate any more: the eval is `evaluate batch --tasks
<dir>`, which discovers tasks from the directory, so a newly added task is picked
up on the next run with no flowchart edit.
Prints the new benchmark version. Retiring a task instead of adding one:
pass --retire <id> (drops it from `tasks`, still bumps + regens).

Usage: apply_dataset_evolution.py <new_task_id> [--note TEXT]
       apply_dataset_evolution.py --retire <task_id> [--note TEXT]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
MANIFEST = REAL_AUTORES / "benchmark-manifest.json"
SCRIPTS = REAL_AUTORES / "scripts"
PY = "/home/acer01/axi-assistant/.venv/bin/python"
SOUL_BENCH = "/home/acer01/axi-assistant/user-data/soul-benchmarks"
TASKS_DIR = Path(SOUL_BENCH) / "tasks"

#: A task must be judgeable on at least these. `verification` is deliberately
#: NOT here: the judge disables that criterion (quality.DISABLED_CRITERIA), so
#: it is null on every new run and requiring it would reject a task for
#: omitting text nothing reads.
REQUIRED_CRITERIA = {"correctness", "code_quality"}

#: Every optional piece a task directory may carry. Presence is the
#: declaration -- no key anywhere announces them, gaia just looks (tasks.py).
#: Listed so an unrecognised entry can be reported rather than ignored, which
#: is how a misspelled `fixtures/` becomes a task that silently stages nothing.
KNOWN_ENTRIES = {
    "task.toml",            # required
    "fixture",              # staged into the agent's tree before it starts
    "prepare.sh",           # setup, run in a container with network
    "hidden",               # tests the agent never sees, mounted at $HIDDEN
    "validate",             # validator helper scripts
    "answer_schema.json",   # JSON Schema for the structured answer channel
    "validator.py",         # deterministic scorer for the x3 answer axis
    "README.md",            # notes, carried and ignored
}


def _next_version(v: str) -> str:
    if v == "seed":
        return "v2"
    m = re.fullmatch(r"v(\d+)", v or "")
    if m:
        return f"v{int(m.group(1)) + 1}"
    return f"{v}-next"


def _validate_checks(task_id: str, task, problems: list[str]) -> None:
    """Every deterministic check a task directory can carry.

    Appends to `problems` rather than raising, so one run reports EVERY fault
    at once. The teacher authors these files in a single agent turn; making it
    round-trip once per mistake wastes a turn per fault.

    The whole point is to fail HERE. The next discovery point is RESCORE, at
    $40 a target -- a validator.py with a syntax error must not cost $80 to
    notice.
    """
    d = TASKS_DIR / task_id
    where = f"tasks/{task_id}"

    unknown = sorted(p.name for p in d.iterdir() if p.name not in KNOWN_ENTRIES)
    if unknown:
        problems.append(
            f"{where}: unrecognised entr{'y' if len(unknown) == 1 else 'ies'} {unknown}. "
            f"gaia discovers optional pieces by exact name, so a misspelling is "
            f"silently ignored rather than erroring. Known: {sorted(KNOWN_ENTRIES)}"
        )

    schema_p, validator_p = d / "answer_schema.json", d / "validator.py"
    hidden_d, fixture_d, validate_d = d / "hidden", d / "fixture", d / "validate"
    prepare_p = d / "prepare.sh"

    # -- the structured-answer channel: schema and scorer are a PAIR --------
    if schema_p.exists():
        try:
            schema = json.loads(schema_p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"{where}/answer_schema.json is not valid JSON: {e}")
        else:
            if not isinstance(schema, dict):
                problems.append(f"{where}/answer_schema.json must be a JSON object, got {type(schema).__name__}")
            elif not ({"type", "properties", "$ref", "oneOf", "anyOf"} & set(schema)):
                problems.append(
                    f"{where}/answer_schema.json has none of type/properties/$ref/oneOf/anyOf, "
                    f"so it constrains nothing and the agent's answer is unchecked"
                )
    if validator_p.exists():
        src = validator_p.read_text(encoding="utf-8")
        try:
            compile(src, str(validator_p), "exec")
        except SyntaxError as e:
            problems.append(f"{where}/validator.py does not compile: line {e.lineno}: {e.msg}")
        if not src.strip():
            problems.append(f"{where}/validator.py is empty")

    if validator_p.exists() and not schema_p.exists():
        problems.append(
            f"{where}: validator.py with no answer_schema.json. Without a schema the agent is "
            f"never asked for a structured answer, so the answer-accuracy axis scores 0.0 on "
            f"every cell forever -- blaming the agent for a gap in the task"
        )
    if schema_p.exists() and not validator_p.exists():
        problems.append(
            f"{where}: answer_schema.json with no validator.py. The answer is collected and "
            f"then nothing scores it"
        )

    # -- strict validators, and the hidden tests they reach through $HIDDEN --
    validators = getattr(task, "validators", ()) or ()
    for v in validators:
        if not (v.run or "").strip():
            problems.append(f"{where}: validator {v.id!r} has an empty `run`")
    uses_hidden = any("$HIDDEN" in (v.run or "") for v in validators)

    if uses_hidden and not hidden_d.is_dir():
        problems.append(
            f"{where}: a validator references $HIDDEN but there is no hidden/ directory. "
            f"$HIDDEN would expand to nothing and the check runs against the wrong path"
        )
    if hidden_d.exists():
        if not hidden_d.is_dir():
            problems.append(f"{where}/hidden exists but is not a directory")
        elif not any(hidden_d.iterdir()):
            problems.append(f"{where}/hidden is empty")
        elif not uses_hidden:
            problems.append(
                f"{where}: hidden/ has files but no validator references $HIDDEN, so nothing "
                f"ever runs them. Either wire a [[validators]] entry or drop the directory"
            )

    # -- the pieces that shape the agent's tree ----------------------------
    if fixture_d.exists():
        if not fixture_d.is_dir():
            problems.append(f"{where}/fixture exists but is not a directory")
        elif not any(fixture_d.rglob("*")):
            problems.append(f"{where}/fixture is empty, so the agent still opens a bare tree")
    if validate_d.exists() and not validate_d.is_dir():
        problems.append(f"{where}/validate exists but is not a directory")
    if prepare_p.exists():
        text = prepare_p.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            problems.append(f"{where}/prepare.sh is empty")
        elif not text.lstrip().startswith("#!"):
            problems.append(
                f"{where}/prepare.sh has no shebang. It is executed directly, so the "
                f"interpreter must not be left to chance"
            )

    if not validators and not validator_p.exists():
        print(
            f"apply_dataset_evolution: NOTE {where} carries no deterministic check "
            f"(no [[validators]], no validator.py). It is judge-only, contributes nothing "
            f"to solved_rate, and is the easiest kind of task to overfit.",
            file=sys.stderr,
        )


def _validate_task(task_id: str) -> None:
    """Load the task through gaia-testbench itself; raise SystemExit on any problem.

    Reports every fault found, not just the first, matching gaia's own loader
    contract ("a config either loads completely or is rejected with every
    problem listed at once").
    """
    sys.path.insert(0, SOUL_BENCH)
    try:
        from gaia_testbench.tasks import get_task  # noqa: E402
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"apply_dataset_evolution: cannot import gaia-testbench: {e!r}")
    if not (TASKS_DIR / task_id).is_dir():
        raise SystemExit(f"apply_dataset_evolution: tasks/{task_id}/ does not exist")
    try:
        task = get_task(task_id)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"apply_dataset_evolution: tasks/{task_id}/task.toml failed to load: {e!r}")

    problems: list[str] = []
    have = {k for k, v in task.rubric.items() if (v or "").strip()}
    missing = REQUIRED_CRITERIA - have
    if missing:
        problems.append(
            f"tasks/{task_id}/task.toml [rubric] missing or empty: {sorted(missing)} "
            f"(has {sorted(have)}) -- a task with no rubric is never judged"
        )
    if not task.prompt.strip():
        problems.append(f"tasks/{task_id}/task.toml has an empty prompt")

    _validate_checks(task_id, task, problems)

    if problems:
        raise SystemExit(
            "apply_dataset_evolution: refusing to add "
            f"{task_id!r} -- {len(problems)} problem(s):\n  - "
            + "\n  - ".join(problems)
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", nargs="?", help="new task id to add (tasks/<id>/task.toml must exist)")
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

    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
