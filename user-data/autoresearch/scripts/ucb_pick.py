"""sample-config PICK: uncertainty-aware parent sampling over the derived dictionary.

Usage: ucb_pick.py [--strategy thompson|argmax] [--c FLOAT]
                   [--sd-floor FLOAT] [--seed INT] [--dict-dir DIR]

Replaces the old deterministic UCB argmax (which always returned the single
top-scoring bundle). Default strategy is THOMPSON sampling: draw one sample from
each scored bundle's posterior over its mean score -- Normal(aggregate,
max(stderr, sd_floor)) -- and pick the argmax of the draws. This weights
selection toward high-scoring bundles while still occasionally picking a
lower-mean / higher-uncertainty bundle (exploration), instead of deterministically
choosing the top score. Because sample-config runs this once per lineage, a
stochastic pick is what makes the orchestrator's N-way fan-out actually diverse.

UNSCORED bundles (n_evals == 0) keep the old must-explore guarantee: if any exist,
one is chosen uniformly at random *before* any scored bundle is considered
(preserving PLAN.md §3.1 uncertainty-aware exploration; randomized so a fan-out
spreads across several never-tried bundles rather than hammering one).

'argmax' strategy reproduces the exact old behavior: argmax(aggregate + c*stderr),
unscored treated as +inf.

Prints ONLY the chosen bundle id (no trailing newline) so a flowchart bash block
can capture it as parent_id.

Determinism: unseeded by default -> each process picks independently (required for
diverse fan-out). AUTORES_SAMPLE_SEED / --seed force a seed for TESTS ONLY; a fixed
seed makes every call in an epoch identical, collapsing the fan-out.

Knobs default to the central tunables (env override > tunables.json > default):
  --strategy -> tunables.sample_strategy()  (default 'thompson')
  --c        -> tunables.ucb_c()            (default 1.0; used by 'argmax' only)
  --sd-floor -> tunables.thompson_sd_floor()(default 0.05)
  --seed     -> AUTORES_SAMPLE_SEED          (default: unseeded)
  dict-dir   -> <AUTORES_ROOT>/dict/configs; AUTORES_ROOT is the general root
               override used across the loop's data paths.

The dictionary is one uniform population: every bundle is sampled, with no
target/optimizer split.
"""
import argparse
import random
import sys
from pathlib import Path

# The derive_dictionary CODE module always lives at the real autoresearch root
# (data location follows AUTORES_ROOT via --dict-dir; the code does not move).
REAL_AUTORES = "/home/acer01/axi-assistant/user-data/autoresearch"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import tunables`
sys.path.insert(0, REAL_AUTORES)  # for `import derive_dictionary`
import derive_dictionary as dd  # noqa: E402
import tunables  # noqa: E402


def pick(rows: list[dict], strategy: str, c: float, sd_floor: float,
         rng: random.Random) -> dict:
    """Choose one bundle row. See module docstring for the semantics."""
    if strategy == "argmax":
        def ucb(r: dict) -> float:
            if not r["scored"]:
                return float("inf")  # unexplored -> highest priority
            return r["aggregate"] + c * (r["stderr"] or 0.0)
        return max(rows, key=ucb)

    # thompson (default)
    unscored = [r for r in rows if not r["scored"]]
    if unscored:  # must-explore never-scored bundles first, randomized
        return rng.choice(unscored)

    scored = [r for r in rows if r["scored"]]

    def draw(r: dict) -> float:
        sd = r["stderr"] if (r["stderr"] and r["stderr"] > 0) else sd_floor
        return rng.gauss(r["aggregate"], sd)
    return max(scored, key=draw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["thompson", "argmax"], default=None,
                    help="sampling strategy (default: tunables SAMPLE_STRATEGY)")
    ap.add_argument("--c", type=float, default=None,
                    help="UCB exploration weight, 'argmax' strategy only (default: tunables UCB_C)")
    ap.add_argument("--sd-floor", type=float, default=None,
                    help="posterior sd floor for Thompson draws (default: tunables THOMPSON_SD_FLOOR)")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed -- TESTS ONLY (default: unseeded / AUTORES_SAMPLE_SEED)")
    ap.add_argument("--dict-dir", type=Path, default=None,
                    help="dict/configs dir (default: <AUTORES_ROOT>/dict/configs)")
    args = ap.parse_args()

    strategy = args.strategy or tunables.sample_strategy()
    c = args.c if args.c is not None else tunables.ucb_c()
    sd_floor = args.sd_floor if args.sd_floor is not None else tunables.thompson_sd_floor()
    seed = args.seed if args.seed is not None else tunables.sample_seed()
    dict_dir = args.dict_dir or (tunables.autores_root() / "dict" / "configs")

    rows = dd.scan(dict_dir)
    if not rows:
        raise SystemExit(f"ucb_pick: no bundles in {dict_dir}")

    rng = random.Random(seed)  # seed=None -> OS entropy (independent per process)
    best = pick(rows, strategy, c, sd_floor, rng)
    sys.stdout.write(best["id"])  # no trailing newline: clean parent_id
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
