"""Transform flowchart spawn blocks carrying a ``provider`` field into
``env`` maps the flowcoder engine understands.

The engine parses command JSONs itself from the search paths, so Axi cannot
inject resolved env into a block the engine already parsed. Instead, at
engine launch we scan the command JSONs, rewrite provider blocks to carry
their resolved env, and write the transformed commands to a shadow dir that
is prepended to the search paths (first-match-wins makes shadows
authoritative).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from axi import config
from axi.config import resolve_runtime

log = logging.getLogger(__name__)

SHADOW_DIR = os.path.join(config.AXI_USER_DATA, "flowcharts-resolved")


def _iter_command_files(search_paths: list[str]):
    for sp in search_paths:
        if not os.path.isdir(sp):
            continue
        for fname in sorted(os.listdir(sp)):
            if fname.endswith(".json"):
                yield os.path.join(sp, fname)


def _transform_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Return the rewritten block, or None if unchanged/error."""
    if block.get("type") != "spawn" or "provider" not in block:
        return None
    provider = block["provider"]
    model = block.get("model") or ""

    try:
        _, env, _ = resolve_runtime(model, provider=provider)
    except ValueError as e:
        log.warning("Flowchart provider block skipped: %s", e)
        return None
    new_block = dict(block)
    new_block.pop("provider", None)
    new_block["env"] = env
    return new_block


def transform_commands(search_paths: list[str]) -> str:
    """Rewrite provider spawn blocks into env maps; return shadow dir path
    ('' if nothing was transformed)."""
    written: list[str] = []
    for path in _iter_command_files(search_paths):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            log.warning("Skipping unreadable command %s: %s", path, e)
            continue
        blocks = data.get("flowchart", {}).get("blocks", {})
        changed = False
        for key, block in blocks.items():
            if not isinstance(block, dict):
                continue
            new_block = _transform_block(block)
            if new_block is not None:
                blocks[key] = new_block
                changed = True
        if not changed:
            continue
        os.makedirs(SHADOW_DIR, exist_ok=True)
        out_path = os.path.join(SHADOW_DIR, os.path.basename(path))
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        written.append(out_path)
    if not written:
        return ""
    return SHADOW_DIR
