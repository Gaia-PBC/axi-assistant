"""teacher RESCORE helper: print the absolute dir of the rank-th top-scored TARGET
bundle (1-based, by aggregate desc). Ranks past the end fall back to the top-1 so a
fixed-K rescore fan-out (RESCORE_1/RESCORE_2/...) degrades to re-scoring the top
bundle harmlessly rather than erroring when fewer than K bundles are scored.

Usage: top_k_dir.py <rank>   (prints one absolute path, no trailing newline)
"""
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
sys.path.insert(0, str(REAL_AUTORES))
import derive_dictionary as dd  # noqa: E402


def main() -> int:
    rank = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    dict_dir = REAL_AUTORES / "dict" / "configs"
    rows = [r for r in dd.scan(dict_dir) if r["kind"] == "target" and r["scored"]]
    if not rows:
        raise SystemExit("top_k_dir: no scored target bundles")
    rows.sort(key=lambda r: -(r["aggregate"] or 0.0))
    idx = min(rank, len(rows)) - 1  # clamp; ranks past the end -> last... use top-1 fallback below
    if rank > len(rows):
        idx = 0  # fall back to the top bundle
    sys.stdout.write(str(dict_dir / rows[idx]["id"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
