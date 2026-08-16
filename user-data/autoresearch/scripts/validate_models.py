"""mutate-config STAMP guard: reject a candidate bundle that references a model
NOT in the allowlist (models.json), so a mutation can never ship a hallucinated or
unservable model to a paid eval.

Scans every model id the bundle actually uses, in the gaia-testbench config
layout (soul-benchmarks 23dfcd4):
  - config-meta.json "model"          (the main session model)
  - every sibling *.json spawn block "model"  (the real doer knob)

Exit 1 + a clear message on ANY invalid id, so the caller
(continue_on_error:false) aborts the lineage rather than emitting a broken
candidate.

This gate's failure mode matters more than most: it is the only thing between a
hallucinated model id and a paid run. So it fails LOUDLY rather than passing
quietly when it cannot find anything to check -- a bundle with no
`config-meta.json` is an old-layout bundle (manifest.main_model +
flowchart/*.json), and scanning it would find zero refs and "pass". That is
reported as an error, not silence.

Usage: validate_models.py <bundle_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import models_lib  # noqa: E402


def _flowchart_model_refs(bundle: Path) -> list[tuple[str, str]]:
    """Spawn-block models across the config's flowcharts.

    Flowcharts are flat siblings now, so this globs the bundle root and skips
    whatever does not parse as a flowchart (manifest.json, score.json, ...) --
    the same leniency gaia-testbench's own loader applies.
    """
    out: list[tuple[str, str]] = []
    for f in sorted(bundle.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict):
            continue
        for b in d.get("flowchart", {}).get("blocks", {}).values():
            if isinstance(b, dict) and b.get("type") == "spawn" and b.get("model"):
                out.append((f"{f.name}:{b.get('id')}.model", b["model"]))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: validate_models.py <bundle_dir>")
    bundle = Path(sys.argv[1])

    meta_path = bundle / "config-meta.json"
    if not meta_path.is_file():
        print(
            f"validate_models: {meta_path} not found. This gate only understands the "
            f"gaia-testbench config layout; an old-layout bundle would scan to zero "
            f"model refs and pass silently, which defeats the gate.",
            file=sys.stderr,
        )
        return 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    refs: list[tuple[str, str]] = []
    if meta.get("model"):
        refs.append(("config-meta.model", meta["model"]))
    refs += _flowchart_model_refs(bundle)

    # Fail closed. Every loadable config has at least one model ref --
    # config-meta.json's "model" is required and non-empty by gaia-testbench's
    # own loader -- so finding none means this scanner is looking in the wrong
    # places, i.e. the layout moved underneath it again. Reporting "ok — 0 refs"
    # would be this gate failing open at exactly the moment it stopped working,
    # which is the one outcome it exists to prevent. (Raised by axi-master.)
    if not refs:
        print(
            f"validate_models: found no model refs under {bundle} — layout drift? "
            f"Expected at least config-meta.json 'model'. Refusing to pass a bundle "
            f"this scanner cannot actually read.",
            file=sys.stderr,
        )
        return 1

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
