"""Regenerate models.json from the provider/model registry (single source of truth).

The registry (``axi.providers.list_models``) does live discovery of on-box
ollama/vLLM models plus a hardcoded Anthropic table. This pulls that list,
stamps each entry with its provider, and merges it with our hand-maintained
annotations (tier / rel_cost — which discovery cannot supply).

ALL providers are included, not just anthropic: a soul-config's model_set is a
knob and the model need not be Claude. The loop optimizes Axi's soul, whatever
serves it.

Two safety properties, because this file bounds what the mutator may propose:

  * **In-use ids are never dropped.** The registry's hardcoded Anthropic table
    lags reality (it lists claude-sonnet-4-5, while every experiment in the
    dictionary ran on claude-sonnet-5). Removing an id the dictionary already
    uses would invalidate the allowlist against the loop's own history.
  * **A provider that fails discovery does not erase its models.** ollama being
    down must not silently shrink the search space.

Usage: refresh_models.py [--dry-run]
"""
from __future__ import annotations

import json
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")
MODELS_JSON = REAL_AUTORES / "models.json"

DOC = (
    "Models the mutator may propose for a bundle's main_model or a flowchart "
    "spawn-block model. Generated from the provider/model registry "
    "(axi.providers.list_models) by scripts/refresh_models.py — regenerate rather "
    "than hand-editing, except to add/adjust the `tier` and `rel_cost` annotations, "
    "which discovery cannot supply and which regeneration preserves. `provider` is "
    "the entry from user-data/providers.json that serves the id; axi auto-routes an "
    "id that is unique to one non-anthropic provider, so the eval only needs the id. "
    "`rel_cost` is a COARSE ordering hint for quality-per-cost reasoning (local "
    "models are 0 — no marginal token cost, but they trade quality and latency); the "
    "authoritative cost is each eval's measured cost_usd. ENFORCEMENT: "
    "run-experiment's VALIDATE_MODELS block gates on this list before paying for an "
    "eval, so an id absent here fails the lineage fast rather than burning a run. "
    "`candidates` are listed-but-unverified ids kept OUT of the allowlist."
)


# Bare tier aliases are NOT allowlisted: `sonnet` remaps over time, so a lineage
# attributed to it stops meaning anything. They go to `candidates` instead.
TIER_ALIASES = ("opus", "sonnet", "haiku", "fable")

# Coarse quality-per-cost ordering, inferred from the id when no hand annotation
# exists. rel_cost=None would defeat the reasoning the mutation prompt asks for.
TIER_COST = {"haiku": 1, "fable": 1, "sonnet": 5, "opus": 15, "local": 0}


def _infer_tier(model_id: str, provider: str) -> str:
    if provider != "anthropic":
        return "local"
    low = model_id.lower()
    for tier in ("haiku", "sonnet", "opus", "fable"):
        if tier in low:
            return tier
    return "anthropic"


def _iter_manifest_models(root: Path):
    for manifest in sorted((root / "dict" / "configs").glob("*/manifest.json")):
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("main_model"):
            yield m["main_model"]
        for v in (m.get("spawn_block_models") or {}).values():
            if v:
                yield v


def _iter_flowchart_models(root: Path):
    for f in sorted((root / "dict" / "configs").glob("*/flowchart/*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for b in d.get("flowchart", {}).get("blocks", {}).values():
            if b.get("type") == "spawn" and b.get("model"):
                yield b["model"]


def in_use_ids(root: Path) -> set[str]:
    """Every model id the dictionary actually references."""
    return {m for m in (*_iter_manifest_models(root), *_iter_flowchart_models(root))
            if m and "{{" not in m}


def regenerate(discovered: dict, root: Path | None = None) -> dict:
    """Merge discovery with existing annotations and write models.json.

    ``discovered`` is {provider_name: [model dicts] | {"error": ...}} — the shape
    axi.providers.list_models returns.
    """
    root = REAL_AUTORES if root is None else root
    path = MODELS_JSON if root is REAL_AUTORES else root / "models.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    prev = {m["id"]: m for m in existing.get("models", []) if m.get("id")}

    out: dict[str, dict] = {}
    aliases: dict[str, dict] = {}
    for provider, plist in discovered.items():
        if isinstance(plist, dict):  # {"error": ...} — provider unreachable
            continue
        for m in plist:
            mid = m.get("id")
            if not mid:
                continue
            old = prev.get(mid, {})
            tier = old.get("tier") or _infer_tier(mid, provider)
            entry = {
                "id": mid,
                "provider": provider,
                "tier": tier,
                "rel_cost": old.get("rel_cost") if old.get("rel_cost") is not None
                            else TIER_COST.get(tier, 0),
                "reasoning": m.get("reasoning", old.get("reasoning", False)),
                "context_window": m.get("context_window", old.get("context_window")),
                "verified": True,
            }
            if mid.lower() in TIER_ALIASES:
                aliases[mid] = {"id": mid, "provider": provider,
                                "note": "bare tier alias — remaps over time, so an experiment "
                                        "attributed to it is not reproducible. Pin a dated id."}
            else:
                out[mid] = entry

    # Never shrink below what the dictionary already uses, and never let a
    # failed provider erase entries we previously recorded for it.
    reachable = {p for p, v in discovered.items() if not isinstance(v, dict)}
    for mid, old in prev.items():
        if mid in out:
            continue
        if mid in in_use_ids(root) or old.get("provider") not in reachable:
            out[mid] = {
                **old,
                # Legacy entries predate the provider field. A free-form id falls
                # through to native anthropic (resolve_runtime rule 5), so that is
                # the correct default — leaving it unset would render as '?' for a
                # model the loop is actively using.
                "provider": old.get("provider") or "anthropic",
                "verified": old.get("verified", False),
                "retained": True,
            }

    result = {
        "_doc": DOC,
        "generated_by": "scripts/refresh_models.py from axi.providers.list_models()",
        "models": sorted(out.values(), key=lambda x: (x.get("provider") or "", x["id"])),
        "candidates": sorted(
            {**{c["id"]: c for c in existing.get("candidates", []) if c.get("id")},
             **aliases}.values(),
            key=lambda x: x["id"]),
    }
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    sys.path.insert(0, "/home/acer01/axi-assistant")
    try:
        from axi.providers import list_models
    except Exception as e:  # noqa: BLE001
        print(f"refresh_models: registry unavailable ({e!r}); models.json unchanged.",
              file=sys.stderr)
        return 1

    discovered = list_models()
    for provider, v in discovered.items():
        if isinstance(v, dict):
            print(f"refresh_models: provider {provider!r} unreachable ({v.get('error')}) — "
                  f"its existing entries are RETAINED, not dropped", file=sys.stderr)
    if a.dry_run:
        print(json.dumps({p: (v if isinstance(v, dict) else [m["id"] for m in v])
                          for p, v in discovered.items()}, indent=2))
        return 0
    result = regenerate(discovered)
    print(f"refresh_models: wrote {len(result['models'])} model(s) to {MODELS_JSON}")
    for m in result["models"]:
        flag = "  (retained, not rediscovered)" if m.get("retained") else ""
        print(f"  {m.get('provider', '?'):<14} {m['id']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
