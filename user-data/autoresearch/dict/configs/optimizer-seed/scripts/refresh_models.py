"""Regenerate models.json from the provider/model registry (single source of truth).

The registry (axi/providers.py — `list_models()`, currently on the unmerged
provider-model-registry branch) does live discovery of on-box ollama/vLLM models
plus a hardcoded Anthropic table. This script pulls that authoritative list and
merges it with our hand-maintained annotations (tier / rel_cost — which discovery
can't provide), then writes models.json.

Today (registry unmerged) `from axi.providers import list_models` fails, so this
degrades to a clear no-op message and leaves the hand-seeded models.json in place.
Run it after the registry lands to switch models.json to live discovery.

Usage: refresh_models.py [--include-local]   (default: Anthropic models only —
the loop optimizes Axi's Claude soul; --include-local adds ollama/vLLM ids too)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
MODELS_JSON = REAL_AUTORES / "models.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-local", action="store_true",
                    help="also allowlist ollama/vLLM discovered models (default: Anthropic only)")
    args = ap.parse_args()

    sys.path.insert(0, "/home/acer01/axi-assistant")
    try:
        from axi.providers import list_models  # type: ignore
    except Exception as e:  # noqa: BLE001
        print(f"refresh_models: provider/model registry not available ({e!r}).\n"
              f"  models.json stays hand-maintained until axi.providers merges — "
              f"edit it by hand to add a verified model. No changes made.", file=sys.stderr)
        return 0

    discovered = list_models()  # {provider_name: [{id, context_window, reasoning?}, ...]}
    existing = json.loads(MODELS_JSON.read_text(encoding="utf-8")) if MODELS_JSON.exists() else {}
    ann = {m["id"]: m for m in existing.get("models", [])}  # preserve tier/rel_cost annotations

    keep_providers = None if args.include_local else {"anthropic"}
    models: list[dict] = []
    for provider, plist in discovered.items():
        if keep_providers is not None and provider not in keep_providers:
            continue
        for m in plist:
            mid = m.get("id")
            if not mid:
                continue
            prev = ann.get(mid, {})
            models.append({
                "id": mid,
                "tier": prev.get("tier", provider),          # keep our tier annotation; else provider name
                "rel_cost": prev.get("rel_cost"),            # discovery can't give cost — leave for human
                "reasoning": m.get("reasoning", prev.get("reasoning", False)),
                "context_window": m.get("context_window"),
                "verified": True,                            # discovered => the box can serve it
            })

    out = {
        "_doc": existing.get("_doc", "regenerated from the provider/model registry"),
        "generated_by": "refresh_models.py from axi.providers.list_models()",
        "models": sorted(models, key=lambda x: x["id"]),
        "candidates": existing.get("candidates", []),
    }
    MODELS_JSON.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"refresh_models: wrote {len(models)} model(s) to {MODELS_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
