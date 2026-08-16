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


def sample_kinds() -> list[str]:
    return get_list("SAMPLE_KINDS", "AUTORES_SAMPLE_KINDS", ["target"])


if __name__ == "__main__":
    # Quick introspection: print the resolved tunables for the current AUTORES_ROOT.
    print(json.dumps({
        "autores_root": str(autores_root()),
        "L2_THRESHOLD": l2_threshold(),
        "L3_ENABLED": l3_enabled(),
        "UCB_C": ucb_c(),
        "SAMPLE_KINDS": sample_kinds(),
    }, indent=2))
