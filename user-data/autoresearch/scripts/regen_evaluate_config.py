"""Regenerate evaluate-config.json's task fan-out from benchmark-manifest.json.

The benchmark's task set is data (benchmark-manifest.json `tasks`), but the engine
fans out over one static `spawn` block per task. So when the teacher evolves the
benchmark (appends a held-out task, PLAN.md §2e), this script rewrites
evaluate-config.json's L2 + L3 spawn blocks + connections to match the manifest —
keeping the object loop and the teacher's rescore on the SAME evolving task set.

Only flowchart.blocks + flowchart.connections are regenerated; id/name/description/
arguments/metadata are preserved verbatim from the existing file. The L2->L3 cost
cascade topology (WIRE -> L2 fanout -> JOIN -> REDUCE -> GATE ->(true) L3 fanout
-> JOIN -> REDUCE -> WRITE ; GATE->(false) WRITE) is preserved exactly — only the
per-task spawn blocks scale with the manifest.

Usage: regen_evaluate_config.py [--manifest PATH] [--flowchart PATH] [--check]
  --check : regenerate in memory, engine-validate, and diff task coverage vs the
            manifest WITHOUT writing (used by tests). Exit 1 if invalid/mismatched.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
SEED_FC = REAL_AUTORES / "flowchart/evaluate-config.json"


def _safe(task_id: str) -> str:
    return task_id.replace("-", "_")


def _spawn(level: str, task_id: str) -> dict:
    lvl_lc = level.lower()  # "l2" | "l3"
    gaia_level = "L2" if level == "L2" else "axi"
    cwd = f"{{{{eval_scratch}}}}/case-{task_id}" if level == "L2" else f"{{{{eval_scratch}}}}/case-l3-{task_id}"
    return {
        "id": f"{lvl_lc}_{_safe(task_id)}",
        "type": "spawn",
        "name": f"{level}_{task_id}",
        "agent_name": f"case-{lvl_lc}-{task_id}",
        "command_name": "eval-case",
        "arguments": f"$1 {task_id} {gaia_level} {cwd}",
        "cwd": cwd,
        "cost_variable": f"c_{lvl_lc}_{_safe(task_id)}",
        "inherit_variables": True,
    }


def build_graph(existing: dict, tasks: list[str]) -> dict:
    """Return a new flowchart dict (blocks + connections) for the given task list."""
    old_blocks = existing["flowchart"]["blocks"]
    # Reuse the fixed (non-per-task) blocks verbatim so their bodies never drift.
    fixed = {k: old_blocks[k] for k in
             ("start", "wire", "join_l2", "reduce_l2", "gate", "join_l3", "reduce_l3", "write", "end")}

    blocks = dict(fixed)
    l2_ids, l3_ids = [], []
    for t in tasks:
        b2, b3 = _spawn("L2", t), _spawn("L3", t)
        blocks[b2["id"]] = b2
        blocks[b3["id"]] = b3
        l2_ids.append(b2["id"])
        l3_ids.append(b3["id"])

    conns = []
    n = 0

    def edge(src, dst, **extra):
        nonlocal n
        n += 1
        c = {"id": f"c{n}", "source_block_id": src, "target_block_id": dst,
             "source_port": extra.get("source_port", "out"), "target_port": "in"}
        c.update({k: v for k, v in extra.items() if k != "source_port"})
        return c

    conns.append(edge("start", "wire"))
    conns.append(edge("wire", l2_ids[0]))
    for a, b in zip(l2_ids, l2_ids[1:]):
        conns.append(edge(a, b))
    conns.append(edge(l2_ids[-1], "join_l2"))
    conns.append(edge("join_l2", "reduce_l2"))
    conns.append(edge("reduce_l2", "gate"))
    conns.append(edge("gate", l3_ids[0], source_port="true", is_true_path=True,
                      label="l2 cleared threshold + L3 enabled"))
    conns.append(edge("gate", "write", source_port="false", is_true_path=False,
                      label="L2-only score (Phase 1 default)"))
    for a, b in zip(l3_ids, l3_ids[1:]):
        conns.append(edge(a, b))
    conns.append(edge(l3_ids[-1], "join_l3"))
    conns.append(edge("join_l3", "reduce_l3"))
    conns.append(edge("reduce_l3", "write"))
    conns.append(edge("write", "end"))

    new = json.loads(json.dumps(existing))  # deep copy
    new["flowchart"]["blocks"] = blocks
    new["flowchart"]["connections"] = conns
    new["flowchart"]["start_block_id"] = "start"
    return new


def _tasks_from_flowchart(fc: dict) -> list[str]:
    """Extract the L2 task coverage from a flowchart (for verification)."""
    out = []
    for b in fc["flowchart"]["blocks"].values():
        if b.get("type") == "spawn" and b["id"].startswith("l2_"):
            out.append(b["arguments"].split()[1])  # "$1 <task> L2 <cwd>"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=REAL_AUTORES / "benchmark-manifest.json")
    ap.add_argument("--flowchart", type=Path, default=SEED_FC)
    ap.add_argument("--check", action="store_true", help="validate + diff without writing")
    args = ap.parse_args()

    tasks = json.loads(args.manifest.read_text(encoding="utf-8"))["tasks"]
    if not tasks:
        raise SystemExit("regen_evaluate_config: manifest has no tasks")
    existing = json.loads(args.flowchart.read_text(encoding="utf-8"))
    new = build_graph(existing, tasks)

    # Always engine-validate the regenerated flowchart before it can be written.
    sys.path.insert(0, "/home/acer01/axi-assistant")
    from flowcoder_flowchart.io import load_command  # noqa: E402
    from flowcoder_flowchart.validation import validate  # noqa: E402
    cmd = load_command(new)
    r = validate(cmd.flowchart)
    covered = _tasks_from_flowchart(new)
    ok = r.valid and covered == tasks
    if args.check:
        print(f"tasks={tasks}")
        print(f"covered={covered}")
        print(f"blocks={len(new['flowchart']['blocks'])} valid={r.valid} errors={r.errors}")
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1

    if not ok:
        raise SystemExit(f"regen_evaluate_config: refusing to write — valid={r.valid} "
                         f"errors={r.errors} covered={covered} tasks={tasks}")
    args.flowchart.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    print(f"regen: wrote {args.flowchart} — {len(tasks)} tasks, {len(new['flowchart']['blocks'])} blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
