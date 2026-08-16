"""eval-case RUN helper: run ONE gaia-testbench task through an adapter, scoring
a specific candidate BUNDLE (its own flowchart/kb/model).

Usage: run_case.py <bundle_dir> <task_id> <workdir_abs> <level>
  level: L2 | flowcoder_cli  (cheap, no Discord)   or   axi | L3 (real instance)

Writes the agent's trace + artifact into <workdir_abs> and a machine-readable
<workdir_abs>/run-result.json {task, level, cost_usd, error, workdir}. Prints the
same JSON to stdout. Exit 1 on an adapter error (so the flowchart can branch).

Builds the Config in-process from the base config.yaml (paths + real-Claude
model) with bundle_dir overridden -> parallel-safe (no shared temp config file).
"""
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/acer01/axi-assistant/user-data/soul-benchmarks")
# The L3 (axi) adapter loads axi_test.py, which imports the `axi` package — put the
# axi-assistant root on sys.path so an L3 eval-case works regardless of cwd (L2 is
# unaffected; it never touches `axi`).
sys.path.insert(0, "/home/acer01/axi-assistant")
from gaia_testbench.config import load_config  # noqa: E402
from gaia_testbench.tasks import get_task  # noqa: E402
from gaia_testbench.adapters.flowcoder_cli import FlowcoderCliAdapter  # noqa: E402


def resolve_bundle_env(bundle_dir) -> dict:
    """Env overrides needed to actually run this bundle's model.

    A soul-config's model need not be Claude. For an ollama/vLLM model,
    resolve_runtime auto-routes on the id (it is unique to one non-anthropic
    provider) and returns ANTHROPIC_BASE_URL / ANTHROPIC_MODEL / auth / context.
    Without them the eval subprocess quietly hits the Anthropic endpoint with an
    id it does not serve, and the candidate is discarded for a harness gap rather
    than a bad hypothesis.

    Returns {} for an Anthropic model, an unpinned model, an unreadable manifest,
    or an ambiguous route — in every one of those cases the plain `--model` path
    is already correct, so degrading to no override is the safe default.
    """
    try:
        manifest = json.loads((Path(bundle_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    model = (manifest.get("main_model") or "").strip()
    if not model:
        return {}
    try:
        from axi.config import resolve_runtime
        _, env, _ = resolve_runtime(model)
    except Exception:  # axi absent, or an ambiguous multi-provider id
        return {}
    return env


def _make_adapter(level: str, cfg):
    if level in ("L2", "flowcoder_cli"):
        return FlowcoderCliAdapter(cfg)
    if level in ("axi", "L3"):
        from gaia_testbench.adapters.axi_adapter import AxiAdapter
        return AxiAdapter(cfg)
    raise SystemExit(f"run_case: unknown level {level!r}")


def main() -> int:
    if len(sys.argv) < 5:
        raise SystemExit("usage: run_case.py <bundle_dir> <task_id> <workdir_abs> <level>")
    bundle_dir, task_id, workdir, level = sys.argv[1:5]
    bundle = Path(bundle_dir).resolve()
    cfg = dataclasses.replace(load_config(), bundle_dir=bundle,
                              env=resolve_bundle_env(bundle))
    task = get_task(task_id)
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    res = asyncio.run(_make_adapter(level, cfg).run(task, wd, "soul"))
    out = {"task": task_id, "level": level, "cost_usd": res.cost_usd,
           "error": res.error, "workdir": str(wd)}
    (wd / "run-result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out))
    return 1 if res.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
