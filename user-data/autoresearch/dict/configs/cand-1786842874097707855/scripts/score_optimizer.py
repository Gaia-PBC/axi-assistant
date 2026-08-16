"""Phase-4 §4.3 — score a candidate OPTIMIZER bundle in a sandboxed scratch dict.

An optimizer, unlike an inert target, must RUN to be scored: it spawns children
and writes bundles. So we score it by running its orchestrator against a
throwaway, write-once scratch dictionary (AUTORES_ROOT isolation) seeded with a
held-out target, then measuring how good the configs IT PRODUCES are (PLAN.md
§4.3 / IMPLEMENTATION.md §4). It can emit junk (self-correcting) but can't touch
the real dict/ or escape its scratch — the real dict is never on its AUTORES_ROOT.

optimizer_score.aggregate = the BEST target aggregate in the sandbox dict after
the run (the quality ceiling the optimizer's search reached). A better optimizer
produces higher-scoring configs, so this lands it on the same 0-1 quality scale as
target bundles — the shared population UCB samples from (PLAN.md §2d/§3.1).

Usage:
  score_optimizer.py <candidate_optimizer_dir> [--held-out ID] [--idea-file PATH]
                     [--model M] [--sandbox DIR] [--no-run] [--engine-timeout S]
  --no-run : seed the sandbox + score whatever is already in it, WITHOUT invoking
             the engine (deterministic; used by tests to exercise seed+score+
             isolation without LLM spend). The real scoring omits it.
Writes <candidate_optimizer_dir>/score.json and prints the aggregate.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
ENGINE = "/home/acer01/axi-assistant/user-data/flowcoder-core/.venv/bin/flowcoder-engine"
AXI_ROOT = "/home/acer01/axi-assistant"
DEFAULT_HELDOUT = "baseline-target"
DEFAULT_IDEA = ("seed-swap-main-sonnet: Swap main_model to claude-sonnet-5 — the "
                "single-knob edit proven to improve the baseline (+0.089).")


def seed_sandbox(sandbox: Path, held_out_id: str, idea_text: str) -> None:
    """Build a throwaway autoresearch root: dict + one held-out target + one idea."""
    if sandbox.exists():
        shutil.rmtree(sandbox)
    (sandbox / "dict/configs").mkdir(parents=True)
    (sandbox / "scratch").mkdir(parents=True)

    src = REAL_AUTORES / "dict/configs" / held_out_id
    if not (src / "manifest.json").exists():
        raise SystemExit(f"score_optimizer: held-out target {held_out_id!r} not found in the real dict")
    dst = sandbox / "dict/configs/held-out-target"
    shutil.copytree(src, dst)
    # Re-stamp lineage so the sandbox bundle is self-consistent (id == folder name).
    m = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    m["id"] = "held-out-target"
    m["parent_id"] = None
    (dst / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")

    # A productive single-knob idea so a working optimizer has something to find.
    (sandbox / "research-ideas.md").write_text(
        "# Research Ideas — sandbox (Phase-4 optimizer scoring)\n\n"
        f"- [ ] {idea_text}\n", encoding="utf-8")
    # L3 off in the sandbox (cost); copy the benchmark manifest so evals match.
    shutil.copy(REAL_AUTORES / "tunables.json", sandbox / "tunables.json")
    tun = json.loads((sandbox / "tunables.json").read_text(encoding="utf-8"))
    tun["L3_ENABLED"] = False
    (sandbox / "tunables.json").write_text(json.dumps(tun, indent=2), encoding="utf-8")
    shutil.copy(REAL_AUTORES / "benchmark-manifest.json", sandbox / "benchmark-manifest.json")
    (sandbox / "results.tsv").write_text(
        "timestamp\tparent_id\tidea_id\tcandidate_id\tcand_score\tparent_score\tkept\tcost_usd\n",
        encoding="utf-8")
    (sandbox / "epoch.log").write_text("", encoding="utf-8")
    (sandbox / "epoch-signals.log").write_text("", encoding="utf-8")


def run_optimizer(candidate: Path, sandbox: Path, model: str, timeout_s: int) -> dict:
    """Run the candidate optimizer's /orchestrator against the sandbox (AUTORES_ROOT)."""
    import os
    search_path = str(candidate / "flowchart")
    cmd = [ENGINE, "--search-path", search_path,
           "--output-format", "stream-json", "--input-format", "stream-json",
           "--model", model, "--permission-mode", "acceptEdits",
           "--cwd", AXI_ROOT, "--verbose"]
    stdin_msg = json.dumps({"type": "user",
                            "message": {"role": "user", "content": "/orchestrator"}}) + "\n"
    env = {**os.environ, "AUTORES_ROOT": str(sandbox)}
    log = sandbox / "optimizer-run.log"
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=stdin_msg.encode(), capture_output=True,
                              env=env, timeout=timeout_s)
        log.write_bytes(proc.stdout + b"\n===STDERR===\n" + proc.stderr)
        return {"ran": True, "returncode": proc.returncode, "duration_s": round(time.monotonic() - t0, 1)}
    except subprocess.TimeoutExpired as e:
        log.write_bytes((e.stdout or b"") + b"\n===TIMEOUT===\n" + (e.stderr or b""))
        return {"ran": True, "returncode": None, "timeout": True, "duration_s": timeout_s}


def score_from_sandbox(sandbox: Path, held_out_id: str = "held-out-target") -> dict:
    """optimizer_score = best target aggregate the optimizer's population reached."""
    sys.path.insert(0, str(REAL_AUTORES))
    import derive_dictionary as dd
    rows = [r for r in dd.scan(sandbox / "dict/configs") if r["kind"] == "target"]
    scored = [r for r in rows if r["scored"]]
    produced = [r for r in scored if r["id"] != held_out_id]  # configs the optimizer created + kept
    baseline = next((r["aggregate"] for r in scored if r["id"] == held_out_id), None)
    best = max((r["aggregate"] for r in scored), default=None)
    improvement = (best - baseline) if (best is not None and baseline is not None) else None
    return {
        "aggregate": best,
        "baseline_aggregate": baseline,
        "improvement": improvement,
        "produced_configs": [{"id": r["id"], "aggregate": r["aggregate"]} for r in produced],
        "n_bundles_in_sandbox": len(rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path, help="candidate optimizer bundle dir")
    ap.add_argument("--held-out", default=DEFAULT_HELDOUT, help="real target id to seed the sandbox with")
    ap.add_argument("--idea-file", type=Path, help="file whose first line is the sandbox mutation idea")
    ap.add_argument("--model", default="claude-sonnet-5", help="orchestrator main-session model")
    ap.add_argument("--sandbox", type=Path, default=None, help="scratch sandbox dir (default: scratch/opt-score-<ts>)")
    ap.add_argument("--no-run", action="store_true", help="seed + score only; skip the engine (test mode)")
    ap.add_argument("--engine-timeout", type=int, default=5400)
    args = ap.parse_args()

    cand = args.candidate.resolve()
    manifest = json.loads((cand / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "optimizer":
        raise SystemExit(f"score_optimizer: {cand} is kind={manifest.get('kind')!r}, expected 'optimizer'")
    model = manifest.get("main_model") or args.model
    idea = (args.idea_file.read_text(encoding="utf-8").splitlines()[0]
            if args.idea_file and args.idea_file.exists() else DEFAULT_IDEA)

    sandbox = args.sandbox or (REAL_AUTORES / "scratch" / f"opt-score-{int(time.time()*1000)}")
    sandbox = sandbox.resolve()
    seed_sandbox(sandbox, args.held_out, idea)

    run_info = {"ran": False, "note": "--no-run: engine skipped (test mode)"}
    if not args.no_run:
        run_info = run_optimizer(cand, sandbox, model, args.engine_timeout)

    sc = score_from_sandbox(sandbox)
    score = {
        "aggregate": sc["aggregate"],
        "kind": "optimizer",
        "optimizer_score": sc,
        "n_evals": len(sc["produced_configs"]),
        "stderr": None,
        "benchmark_version": json.loads((REAL_AUTORES / "benchmark-manifest.json").read_text())["version"],
        "sandbox": str(sandbox),
        "run": run_info,
        "model": model,
    }
    (cand / "score.json").write_text(json.dumps(score, indent=2), encoding="utf-8")
    print(sc["aggregate"] if sc["aggregate"] is not None else "null")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
