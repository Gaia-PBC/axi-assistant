"""run-experiment RECORD: append this experiment to results.tsv from decision.json.

Usage: record_experiment.py <out_dir_abs> [timestamp]
Reads <exp_dir>/decision.json (exp_dir = parent of out_dir) and appends one row to
autoresearch/results.tsv. Header-safe: writes the header first if the file is new.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import tunables  # sibling module (script dir on sys.path); honors AUTORES_ROOT for the sandbox

AR = str(tunables.autores_root())
HEADER = "timestamp\tparent_id\tidea_id\tcandidate_id\tcand_score\tparent_score\tkept\tcost_usd\n"


def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.4f}"
    return str(x)


def main() -> int:
    out_dir = Path(sys.argv[1])
    # Real wall-clock, not a literal: the mutator's evidence pack reasons over
    # "what was tried and when", and a frozen date makes that column useless.
    ts = sys.argv[2] if len(sys.argv) > 2 else dt.datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005 — local, matches `date +%FT%T`
    dec = json.loads((out_dir.parent / "decision.json").read_text(encoding="utf-8"))
    tsv = Path(AR) / "results.tsv"
    if not tsv.exists() or not tsv.read_text(encoding="utf-8").startswith("timestamp"):
        tsv.write_text(HEADER, encoding="utf-8")
    row = "\t".join([
        ts, dec["parent_id"], dec.get("idea_id", ""), dec["candidate_id"],
        _fmt(dec["cand_score"]), _fmt(dec["parent_score"]), str(dec["kept"]).lower(), "",
    ]) + "\n"
    with tsv.open("a", encoding="utf-8") as fh:
        fh.write(row)
    print(json.dumps(dec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
