"""Available-models source for the config-generator (mutate-config).

Reads <autoresearch>/models.json — the allowlist of models a mutation may propose
for a bundle's main_model or a flowchart spawn-block model. Provides the allowlist,
a validity check, and a compact prompt rendering. See models.json's _doc.

Models are a GLOBAL fact of the box (they don't change per sandbox), so this reads
the REAL autoresearch dir, not AUTORES_ROOT — unlike tunables/dict/results which the
§4.3 sandbox redirects. Regenerate models.json from the provider/model registry via
scripts/refresh_models.py once it merges.
"""
from __future__ import annotations

import json
from pathlib import Path

REAL_AUTORES = Path("/home/acer01/axi-assistant/user-data/autoresearch")


def _load() -> dict:
    try:
        return json.loads((REAL_AUTORES / "models.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"models": [], "candidates": []}


def models() -> list[dict]:
    return _load().get("models", [])


def model_ids() -> set[str]:
    return {m["id"] for m in models() if m.get("id")}


def is_valid(model_id: str) -> bool:
    """A null/empty model is valid (means 'inherit the process default')."""
    if not model_id:
        return True
    return model_id in model_ids()


def provider_for(model_id: str) -> str | None:
    """Which provider serves this id, or None if it is not allowlisted."""
    for m in models():
        if m.get("id") == model_id:
            return m.get("provider")
    return None


def render_for_prompt() -> str:
    """The allowlist as the mutator sees it.

    Provider is shown because a soul-config's model need not be Claude — a local
    (ollama/vLLM) model is a legitimate knob value, and its cost profile is
    completely different from a hosted one.
    """
    rows = [
        f"  - {m['id']}  (provider={m.get('provider','?')}, tier={m.get('tier','?')}, "
        f"rel_cost={m.get('rel_cost','?')}, reasoning={m.get('reasoning','?')})"
        for m in models()
    ]
    return "\n".join(rows) if rows else "  (no models listed — models.json missing or empty)"


if __name__ == "__main__":
    print(render_for_prompt())
