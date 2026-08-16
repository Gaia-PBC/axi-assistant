"""sample-config PICK: deterministic UCB argmax over the derived dictionary.

Usage: ucb_pick.py [--c FLOAT] [--kinds LIST|all] [--kind ONE] [--dict-dir DIR]
Picks argmax(aggregate + c*stderr), treating an UNSCORED bundle (n_evals==0)
as +inf so it must be explored first (PLAN.md §3.1 uncertainty-aware sampling).
Prints the chosen bundle id (nothing else) so a flowchart bash block can capture
it as parent_id. Deterministic math replaces IMPLEMENTATION.md §2.7's LLM PICK:
UCB is closed-form, so an LLM adds cost + variance without benefit.

Knobs default to the central tunables (env override > tunables.json > default):
  --c      -> tunables.ucb_c()       (default 1.0)
  --kinds  -> tunables.sample_kinds() (default ["target"]); Phase-4 recursion sets
             ["target","optimizer"] so optimizer bundles are sampled too.
  dict-dir -> <AUTORES_ROOT>/dict/configs; AUTORES_ROOT lets the §4.3 sandbox point
             the scan at a throwaway scratch dictionary.
--kind (singular) is kept as a back-compat alias for a single kind; --kinds=all
disables kind filtering entirely.
"""
import argparse
import sys
from pathlib import Path

# The derive_dictionary CODE module always lives at the real autoresearch root
# (data location follows AUTORES_ROOT via --dict-dir; the code does not move).
REAL_AUTORES = "/home/acer01/axi-assistant/user-data/autoresearch"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import tunables`
sys.path.insert(0, REAL_AUTORES)  # for `import derive_dictionary`
import derive_dictionary as dd  # noqa: E402
import tunables  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--c", type=float, default=None, help="UCB exploration weight (default: tunables UCB_C)")
    ap.add_argument("--kinds", default=None,
                    help="comma-separated bundle kinds to sample (default: tunables SAMPLE_KINDS); 'all' = no filter")
    ap.add_argument("--kind", choices=["target", "optimizer"], default=None,
                    help="[back-compat] single kind; equivalent to --kinds <kind>")
    ap.add_argument("--dict-dir", type=Path, default=None,
                    help="dict/configs dir (default: <AUTORES_ROOT>/dict/configs)")
    args = ap.parse_args()

    c = args.c if args.c is not None else tunables.ucb_c()
    dict_dir = args.dict_dir or (tunables.autores_root() / "dict" / "configs")
    if args.kinds is not None:
        kinds = None if args.kinds.strip().lower() == "all" else [k.strip() for k in args.kinds.split(",") if k.strip()]
    elif args.kind is not None:
        kinds = [args.kind]
    else:
        kinds = tunables.sample_kinds()

    rows = [r for r in dd.scan(dict_dir) if (kinds is None or r["kind"] in kinds)]
    if not rows:
        raise SystemExit(f"ucb_pick: no bundles of kinds {kinds!r} in {dict_dir}")

    def ucb(r: dict) -> float:
        if not r["scored"]:
            return float("inf")  # unexplored -> highest priority
        return r["aggregate"] + c * (r["stderr"] or 0.0)

    best = max(rows, key=ucb)
    sys.stdout.write(best["id"])  # no trailing newline: clean parent_id
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
