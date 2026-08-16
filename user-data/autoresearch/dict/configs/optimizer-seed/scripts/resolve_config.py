"""Resolve a gaia-testbench config directory's provider routing before eval.

A soul-config's model need not be Claude, and a MIXED soul must work: a local
main session driving an Anthropic doer, or an Anthropic main driving a local
one. Two facts make that non-trivial.

  * ANTHROPIC_BASE_URL is process-wide. The main session's routing captures
    every spawned child, so a child pinned to a different provider is sent to
    an endpoint that does not serve it. It then fails invisibly — the harness
    reports no error and the config just scores badly, which the loop records
    as a bad hypothesis rather than a routing fault.
  * flowcoder honours a per-spawn `env` map (flowcoder-core 728d3f6), and an
    override whose value is the empty string UNSETS the variable. Unsetting is
    what lets an Anthropic child escape a locally-routed parent; without it a
    child can only ever be routed further away from native, never back.

So each spawn block gets an explicit env: the provider's vars if it is pinned
to a non-Anthropic model, or unset-markers for exactly the vars the main
session set if it is pinned to an Anthropic one. A spawn with no pinned model
is left alone — it inherits the main session because its author chose that.

The transform writes a COPY. The original is what gets committed to dict/, so
box-specific endpoints never end up stored in the dictionary.

Usage: resolve_config.py <config_dir> <out_dir>
Prints {"config_dir": <resolved>, "env": {...}} — env is the main session's,
for the caller to export before invoking `evaluate`.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

META_FILENAME = "config-meta.json"


def _model_env(model: str) -> dict:
    """Provider env for a model id, or {} for native Anthropic.

    resolve_runtime auto-routes on the id alone — a model unique to one
    non-anthropic provider needs no provider named anywhere. Anything it
    cannot place falls through to anthropic (its rule 5), which is also {}.
    """
    if not model:
        return {}
    try:
        sys.path.insert(0, "/home/acer01/axi-assistant")
        from axi.config import resolve_runtime
        _, env, _ = resolve_runtime(model)
    except Exception:  # axi absent, or an ambiguous multi-provider id
        return {}
    return env


def main_model(config_dir: Path) -> str:
    """The main session's model. config-meta.json is authoritative — under the
    config-directory layout manifest.json holds lineage only."""
    try:
        meta = json.loads((Path(config_dir) / META_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return (meta.get("model") or "").strip()


def _resolve_flowchart(path: Path, parent_env: dict) -> bool:
    """Inject per-spawn env into one flowchart file. True if it changed."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    changed = False
    for block in doc.get("flowchart", {}).get("blocks", {}).values():
        if block.get("type") != "spawn":
            continue
        model = (block.get("model") or "").strip()
        if not model:
            continue  # deliberate inheritance from the main session
        env = _model_env(model)
        if not env and parent_env:
            # Anthropic child under a non-native parent: neutralise exactly the
            # vars the parent set. Empty value == unset, per flowcoder's
            # _clean_env; without this the child inherits the parent's endpoint.
            env = dict.fromkeys(parent_env, "")
        if env:
            block["env"] = env
            changed = True
    if changed:
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return changed


def resolve(config_dir: Path | str, out_dir: Path | str) -> dict:
    src, dst = Path(config_dir), Path(out_dir)
    env = _model_env(main_model(src))
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    for f in sorted(dst.glob("*.json")):
        if f.name == META_FILENAME:
            continue
        _resolve_flowchart(f, env)
    return {"config_dir": str(dst), "env": env}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--exec", dest="exec_", action="store_true",
                    help="resolve, then run the command after `--` with the main-session "
                         "env scoped to THAT process. RESOLVED_CONFIG_DIR is exported to it.")

    # Split on `--` ourselves: argparse will not hand a trailing command to a
    # nargs="*" positional once flags are in play.
    raw = list(sys.argv[1:] if argv is None else argv)
    cmd: list[str] = []
    if "--" in raw:
        i = raw.index("--")
        raw, cmd = raw[:i], raw[i + 1:]
    a = ap.parse_args(raw)

    out = resolve(a.config_dir, a.out_dir)
    if not a.exec_:
        print(json.dumps(out))
        return 0

    if not cmd:
        ap.error("--exec needs a command after `--`")
    # execvpe rather than exporting into the caller's shell. Exporting leaked
    # the local ANTHROPIC_API_KEY into host-side tooling, where the judge picked
    # it up and lost its claude.ai OAuth — the routing must reach the evaluated
    # child and nothing else.
    import os

    # GAIA_AGENT_ENV, not the raw vars: `evaluate` also runs the judge in-process,
    # and the judge needs the harness's own auth. Setting ANTHROPIC_* directly
    # reaches both and breaks the judge's login. The adapter forwards this blob to
    # the agent subprocess only.
    child = {
        **os.environ,
        "GAIA_AGENT_ENV": json.dumps(out["env"]),
        "RESOLVED_CONFIG_DIR": out["config_dir"],
    }
    os.execvpe(cmd[0], cmd, child)
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
