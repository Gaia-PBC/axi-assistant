"""Central tunables loader for the autoresearch loop (Phase 3+4).

Reads <AUTORES_ROOT>/tunables.json. Precedence for every knob:
    env override   >   tunables.json   >   hard default
so a test can force a value with an env var without editing the file, the file
is the durable tuned value, and a missing/broken file still yields safe defaults.

AUTORES_ROOT (env, default the real autoresearch dir) also selects WHICH loop
state a script reads — this is the hook the Phase-4 §4.3 sandbox uses to point a
candidate optimizer at a throwaway scratch dictionary instead of the real one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_AUTORES_ROOT = "/home/acer01/axi-assistant/user-data/autoresearch"


def autores_root() -> Path:
    return Path(os.environ.get("AUTORES_ROOT", DEFAULT_AUTORES_ROOT))


def _load() -> dict:
    try:
        return json.loads((autores_root() / "tunables.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "", "false", "no", "off")


def get_float(key: str, env_key: str, default: float) -> float:
    if env_key in os.environ:
        try:
            return float(os.environ[env_key])
        except ValueError:
            pass
    v = _load().get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def get_bool(key: str, env_key: str, default: bool) -> bool:
    if env_key in os.environ:
        return os.environ[env_key].strip().lower() not in _FALSE
    v = _load().get(key)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in _TRUE
    return default


def get_list(key: str, env_key: str, default: list[str]) -> list[str]:
    if env_key in os.environ:
        return [x.strip() for x in os.environ[env_key].split(",") if x.strip()]
    v = _load().get(key)
    if isinstance(v, list):
        return [str(x) for x in v]
    return list(default)


# Convenience accessors (single source of the key<->env-var mapping).
def l2_threshold() -> float:
    return get_float("L2_THRESHOLD", "GAIA_L2_THRESHOLD", 0.7)


def l3_enabled() -> bool:
    return get_bool("L3_ENABLED", "GAIA_L3_ENABLED", False)


def ucb_c() -> float:
    return get_float("UCB_C", "AUTORES_UCB_C", 1.0)


def fanout_k() -> int:
    """Number of run-experiment lineages the orchestrator fans out per epoch
    (orchestrator.json INIT_FANOUT_K -> the inner loop spawns lineage-1..K).
    Clamped to >= 1 so an epoch always runs at least one lineage."""
    return max(1, int(get_float("FANOUT_K", "AUTORES_FANOUT_K", 3.0)))


def sample_strategy() -> str:
    """Parent-sampling strategy for sample-config / ucb_pick.py:
    'thompson' (default, weighted sampling) or 'argmax' (old deterministic UCB)."""
    v = os.environ.get("AUTORES_SAMPLE_STRATEGY")
    if v is None:
        v = _load().get("SAMPLE_STRATEGY")
    v = (v or "thompson").strip().lower()
    return v if v in ("thompson", "argmax") else "thompson"


def thompson_sd_floor() -> float:
    """Floor on a scored bundle's posterior sd (its stderr) when drawing a
    Thompson sample, so a 1-eval / zero-stderr bundle still gets exploration
    noise instead of collapsing to a point mass at its mean."""
    return get_float("THOMPSON_SD_FLOOR", "AUTORES_THOMPSON_SD_FLOOR", 0.05)


def idea_stale_seconds() -> float:
    """How long a `claim` may sit unterminated before the fold reaps it back to
    `future`. Defaults to orchestrator.json's 21600s join timeout: a lineage
    cannot outlive its own join, so an older claim is definitionally dead."""
    return get_float("IDEA_STALE_SECONDS", "AUTORES_IDEA_STALE_SECONDS", 21600.0)


def idea_gen_min() -> int:
    """Minimum hypotheses the mutator generates at mutate-config step 2c before
    filtering. Large enough that filtering is a real selection; small enough not
    to dominate the mutator's turn."""
    return max(1, int(get_float("IDEA_GEN_MIN", "AUTORES_IDEA_GEN_MIN", 5.0)))


def sample_seed() -> int | None:
    """Optional RNG seed for sample-config — TESTS / repro ONLY, env-only on
    purpose. Leave UNSET in production: a fixed seed makes every ucb_pick call
    in an epoch return the identical parent, collapsing the N-way fan-out to N
    copies of the same lineage."""
    v = os.environ.get("AUTORES_SAMPLE_SEED")
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


if __name__ == "__main__":
    # Quick introspection: print the resolved tunables for the current AUTORES_ROOT.
    print(json.dumps({
        "autores_root": str(autores_root()),
        "L2_THRESHOLD": l2_threshold(),
        "L3_ENABLED": l3_enabled(),
        "UCB_C": ucb_c(),
        "FANOUT_K": fanout_k(),
        "SAMPLE_STRATEGY": sample_strategy(),
        "THOMPSON_SD_FLOOR": thompson_sd_floor(),
        "IDEA_STALE_SECONDS": idea_stale_seconds(),
        "IDEA_GEN_MIN": idea_gen_min(),
        "SAMPLE_SEED": sample_seed(),
    }, indent=2))
