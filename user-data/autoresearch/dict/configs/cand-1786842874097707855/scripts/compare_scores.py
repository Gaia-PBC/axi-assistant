"""run-experiment DECIDE: compare candidate vs parent aggregate, write a
decision.json (in the experiment dir, so it survives the keep-mv), print 1|0.

Usage: compare_scores.py <out_dir_abs> <parent_id> <idea_id>
Keep (print 1) iff the candidate is scored AND (parent unscored OR cand > parent).
decision.json goes in out_dir's PARENT (the exp dir) so RECORD can read it whether
the candidate folder was moved into dict/ (keep) or deleted (discard).
"""
import json
import sys
from pathlib import Path

import tunables  # sibling module (script dir on sys.path); honors AUTORES_ROOT for the sandbox

AR = str(tunables.autores_root())


def _agg(score_path: Path):
    try:
        return json.loads(score_path.read_text(encoding="utf-8")).get("aggregate")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main() -> int:
    out_dir = Path(sys.argv[1])
    parent_id = sys.argv[2]
    idea_id = sys.argv[3] if len(sys.argv) > 3 else ""
    cand = _agg(out_dir / "score.json")
    parent = _agg(Path(AR) / "dict" / "configs" / parent_id / "score.json")
    kept = cand is not None and (parent is None or cand > parent)
    decision = {
        "candidate_id": out_dir.name,
        "parent_id": parent_id,
        "idea_id": idea_id,
        "cand_score": cand,
        "parent_score": parent,
        "kept": kept,
    }
    (out_dir.parent / "decision.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    sys.stdout.write("1" if kept else "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
