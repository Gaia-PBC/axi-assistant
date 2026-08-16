"""mutate-config STAMP guard: reject a candidate bundle that references a model
NOT in the allowlist (models.json), so a mutation can never ship a hallucinated or
unservable model to a paid eval.

Scans every model id the bundle actually uses:
  - manifest.main_model
  - manifest.spawn_block_models values (metadata)
  - every flowchart/*.json spawn block "model" (the real doer knob)
A null/absent model is fine (inherits the process default). Exit 1 + a clear
message on ANY invalid id, so the STAMP step (continue_on_error:false) aborts the
mutation rather than emitting a broken candidate.

Usage: validate_models.py <bundle_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import models_lib  # noqa: E402


def _flowchart_model_refs(bundle: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    fc = bundle / "flowchart"
    if not fc.is_dir():
        return out
    for f in sorted(fc.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for b in d.get("flowchart", {}).get("blocks", {}).values():
            if b.get("type") == "spawn" and b.get("model"):
                out.append((f"{f.name}:{b.get('id')}.model", b["model"]))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: validate_models.py <bundle_dir>")
    bundle = Path(sys.argv[1])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

    refs: list[tuple[str, str]] = []
    if manifest.get("main_model"):
        refs.append(("manifest.main_model", manifest["main_model"]))
    for k, v in (manifest.get("spawn_block_models") or {}).items():
        if v:
            refs.append((f"manifest.spawn_block_models.{k}", v))
    refs += _flowchart_model_refs(bundle)

    bad = [(where, mid) for where, mid in refs if not models_lib.is_valid(mid)]
    if bad:
        for where, mid in bad:
            print(f"validate_models: {where} = {mid!r} is NOT an allowed model", file=sys.stderr)
        print(f"validate_models: allowed = {sorted(models_lib.model_ids())} "
              f"(edit models.json to add one)", file=sys.stderr)
        return 1
    print(f"validate_models: ok — {len(refs)} model ref(s) all in the allowlist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
