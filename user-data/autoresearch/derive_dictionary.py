#!/usr/bin/env python3
"""Derive the autoresearch dictionary: scan dict/configs/*/ -> id->score table.

PLAN.md 4.2 / IMPLEMENTATION.md 0 & 2.7: the dictionary has NO shared registry
file. It is DERIVED at read time by scanning each bundle folder's manifest.json
(id, parent_id, main_model, dataset_version) + score.json (aggregate,
stderr, n_evals, cost_usd, benchmark_version). This is the exact scan that
`sample-config.json`'s SCAN block (Phase 2) wraps to feed the UCB parent pick,
and that a human can run to read the dictionary.

Design notes:
- A bundle is any immediate subdir of dict/configs/ that contains manifest.json.
- score.json may be absent or unscored (n_evals == 0 / aggregate is null) -- such
  a bundle is listed as UNSCORED (it still exists in the dictionary; UCB explores
  it via its infinite uncertainty). Missing score.json is NOT an error.
- Ordering: scored bundles first (aggregate desc), then unscored (by id). This is
  a *presentation* order; UCB sampling (sample-config) applies its own score+c*stderr.

Usage:
  derive_dictionary.py [--dict-dir DIR] [--json]
Default DIR = <this file's dir>/dict/configs .
Default output = an aligned human table; --json emits a list for machine consumers.
Exit code 0 always on a well-formed scan (even if 0 bundles); non-zero only on a
malformed JSON file or a missing dict dir (so callers can trust a 0 exit).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIELDS_FROM_MANIFEST = ("id", "parent_id", "main_model", "dataset_version")
FIELDS_FROM_SCORE = ("aggregate", "stderr", "n_evals", "cost_usd", "benchmark_version")


def _load_json(path: Path):
    """Return parsed JSON, None if the file is absent, or exit on malformed JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"derive_dictionary: {path} is not valid JSON: {exc}")


def scan(dict_dir: Path) -> list[dict]:
    """Scan dict_dir for bundle folders -> a list of derived rows."""
    if not dict_dir.is_dir():
        raise SystemExit(f"derive_dictionary: dict dir not found: {dict_dir}")
    rows: list[dict] = []
    for d in sorted(dict_dir.iterdir()):
        if not d.is_dir():
            continue
        manifest = _load_json(d / "manifest.json")
        if manifest is None:
            print(f"  warn: {d.name}/ has no manifest.json -- skipping", file=sys.stderr)
            continue
        score = _load_json(d / "score.json") or {}
        row = {k: manifest.get(k) for k in FIELDS_FROM_MANIFEST}
        row["id"] = manifest.get("id") or d.name  # fall back to folder name
        for k in FIELDS_FROM_SCORE:
            row[k] = score.get(k)
        row["n_evals"] = row["n_evals"] or 0
        row["scored"] = row["n_evals"] > 0 and row["aggregate"] is not None
        rows.append(row)
    return rows


def _sort_key(r: dict):
    agg = r["aggregate"] if r["aggregate"] is not None else float("-inf")
    # scored first (0), by aggregate desc; unscored (1) by id
    return (0 if r["scored"] else 1, -agg if r["scored"] else 0.0, str(r["id"]))


def _fmt(v, spec: str = "") -> str:
    if v is None:
        return "-"
    if spec:
        try:
            return format(v, spec)
        except (ValueError, TypeError):
            return str(v)
    return str(v)


def render_table(rows: list[dict]) -> str:
    cols = [
        ("id", "id", lambda r: _fmt(r["id"])),
        ("aggregate", "agg", lambda r: _fmt(r["aggregate"], ".4f")),
        ("stderr", "stderr", lambda r: _fmt(r["stderr"], ".4f")),
        ("n_evals", "n_evals", lambda r: _fmt(r["n_evals"])),
        ("cost_usd", "cost_usd", lambda r: ("$" + _fmt(r["cost_usd"], ".4f")) if r["cost_usd"] is not None else "-"),
        ("parent_id", "parent_id", lambda r: _fmt(r["parent_id"])),
        ("main_model", "main_model", lambda r: _fmt(r["main_model"])),
    ]
    header = [label for _, label, _ in cols]
    body = [[render(r) for _, _, render in cols] for r in rows]
    widths = [max(len(header[i]), *(len(row[i]) for row in body)) if body else len(header[i]) for i in range(len(cols))]
    line = lambda cells: "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
    out = [line(header), line(["-" * w for w in widths])]
    out += [line(row) for row in body]
    scored = sum(1 for r in rows if r["scored"])
    out.append("")
    out.append(f"{len(rows)} bundle(s): {scored} scored, {len(rows) - scored} unscored")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Derive the autoresearch dictionary (scan bundles -> id->score table).")
    default_dir = Path(__file__).resolve().parent / "dict" / "configs"
    ap.add_argument("--dict-dir", type=Path, default=default_dir, help=f"dict/configs dir (default: {default_dir})")
    ap.add_argument("--json", action="store_true", help="emit a JSON list instead of a human table")
    args = ap.parse_args(argv)

    rows = scan(args.dict_dir)
    rows.sort(key=_sort_key)

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
