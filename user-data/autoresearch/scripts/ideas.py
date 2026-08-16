"""Append-only event log for the autoresearch idea store.

State (`future` / `present` / `tried` / `used-as-seed` / `retired`) is a FOLD
over `$AUTORES_ROOT/ideas/events.jsonl`, never an in-place edit. Writers only
ever append one line, so concurrent lineages cannot clobber each other; only
`claim` and `render` take the flock sidecar.

Design: docs/superpowers/specs/2026-08-16-autoresearch-idea-generation-design.md
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from typing import TYPE_CHECKING

import tunables  # sibling module (script dir on sys.path); honors AUTORES_ROOT

if TYPE_CHECKING:
    from pathlib import Path

TERMINAL_OPS = ("complete", "seeded", "retire")

# Rendered-view section order: past -> in-flight -> queued -> the rest.
SECTIONS = (
    ("tried", "Tried (past)"),
    ("present", "In flight (present)"),
    ("future", "Queued (future)"),
    ("used-as-seed", "Used as seed"),
    ("retired", "Retired"),
)


class DriftError(RuntimeError):
    """research-ideas.md changed outside `render`, so overwriting would lose an edit."""


def _ideas_dir() -> Path:
    return tunables.autores_root() / "ideas"


def events_path() -> Path:
    return _ideas_dir() / "events.jsonl"


def lock_path() -> Path:
    return _ideas_dir() / ".lock"


@contextlib.contextmanager
def _locked():
    """Serialize the read-modify-write ops (claim, render).

    Appends do NOT take this — they are atomic on their own. Only test-and-set
    against the folded state needs mutual exclusion.
    """
    _ideas_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24].rstrip("-") or "idea"


def mint_id(text: str, origin: str) -> str:
    """Readable kebab id + a short digest of (origin, text).

    The digest is what stops two lineages that independently generate the same
    hypothesis from silently folding into one entry.
    """
    digest = hashlib.sha1(f"{origin}\x00{text}".encode()).hexdigest()[:6]
    return f"{_slug(text)}-{digest}"


def _append(record: dict) -> None:
    """One record == one os.write() to an O_APPEND fd.

    POSIX makes O_APPEND's seek-to-end-plus-write atomic, and Linux holds the
    inode rwsem across a single write(), so concurrent appenders neither clobber
    offsets nor interleave. Python's buffered open(..., "a") may split a record
    across syscalls, which would break that — hence the raw fd.
    """
    _ideas_dir().mkdir(parents=True, exist_ok=True)
    record.setdefault("ts", time.time())
    line = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
    fd = os.open(events_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def propose(text: str, origin: str, idea_id: str | None = None) -> str:
    idea_id = idea_id or mint_id(text, origin)
    _append({"op": "propose", "idea_id": idea_id, "origin": origin, "text": text})
    return idea_id


def claim(lineage: str, idea_id: str | None = None, purpose: str = "seed") -> str | None:
    """Test-and-set: reserve one `future` idea. Returns its id, or None.

    None is a normal outcome (empty pool, or someone else got there first) and
    the caller is expected to proceed unseeded rather than treat it as an error.
    """
    with _locked():
        state = fold()
        if idea_id is None:
            candidates = [k for k, v in state.items() if v["state"] == "future"]
            if not candidates:
                return None
            idea_id = candidates[0]
        elif state.get(idea_id, {}).get("state") != "future":
            return None
        _append({"op": "claim", "idea_id": idea_id, "lineage": lineage, "purpose": purpose})
        return idea_id


def complete(idea_id: str, candidate_id: str, score: float | None, kept: bool) -> None:
    _append({"op": "complete", "idea_id": idea_id, "candidate_id": candidate_id,
             "score": score, "kept": bool(kept)})


def seeded(idea_id: str, produced: str) -> None:
    _append({"op": "seeded", "idea_id": idea_id, "produced": produced})


def retire(idea_id: str, by: str, reason: str = "") -> None:
    _append({"op": "retire", "idea_id": idea_id, "by": by, "reason": reason})


def read_events() -> list[dict]:
    try:
        raw = events_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn line must not poison the whole fold
    return out


def _blank(idea_id: str) -> dict:
    return {"idea_id": idea_id, "text": "", "origin": "", "state": "future",
            "attempts": [], "produced": [], "lineage": None, "claim_ts": None}


def fold(events: list[dict] | None = None, now: float | None = None) -> dict[str, dict]:
    """Reduce the event log to per-idea state.

    Terminal ops (complete / seeded / retire) resolve last-wins by log order —
    a seed later tested on its own merits ends `tried`. A `present` claim older
    than IDEA_STALE_SECONDS is reaped back to `future`, so a lineage that dies
    holding an idea cannot strand it (the failure mode of the old `[~]` marks).
    """
    events = read_events() if events is None else events
    state: dict[str, dict] = {}
    for ev in events:
        idea_id = ev.get("idea_id")
        if not idea_id:
            continue
        entry = state.setdefault(idea_id, _blank(idea_id))
        op = ev.get("op")
        if op == "propose":
            entry["text"] = ev.get("text", entry["text"])
            entry["origin"] = ev.get("origin", entry["origin"])
            entry["state"] = "future"
        elif op == "claim":
            if entry["state"] != "future":
                continue  # a losing racer's record is inert, not authoritative
            entry["state"] = "present"
            entry["lineage"] = ev.get("lineage")
            entry["purpose"] = ev.get("purpose", "seed")
            entry["claim_ts"] = ev.get("ts")
        elif op == "complete":
            entry["attempts"].append({"candidate_id": ev.get("candidate_id"),
                                      "score": ev.get("score"), "kept": ev.get("kept")})
            entry["state"] = "tried"
        elif op == "seeded":
            entry["produced"].append(ev.get("produced"))
            entry["state"] = "used-as-seed"
        elif op == "retire":
            entry["state"] = "retired"
            entry["retire_reason"] = ev.get("reason", "")

    now = time.time() if now is None else now
    stale_after = tunables.idea_stale_seconds()
    for entry in state.values():
        if entry["state"] == "present" and entry["claim_ts"] is not None:
            if now - entry["claim_ts"] >= stale_after:
                entry["state"] = "future"
                entry["stale_lineage"] = entry["lineage"]
                entry["lineage"] = None
    return state


# --- rendered view -------------------------------------------------------


def markdown_path() -> Path:
    return tunables.autores_root() / "research-ideas.md"


def _hash_path() -> Path:
    return _ideas_dir() / ".render-hash"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _describe(entry: dict) -> str:
    bits = [f"`{entry['idea_id']}`"]
    who = entry.get("lineage") or entry.get("origin")
    if who:
        bits.append(f"({who})")
    bits.append(f"— {entry['text']}" if entry["text"] else "— (no text)")
    if entry["attempts"]:
        scored = [a["score"] for a in entry["attempts"] if a.get("score") is not None]
        best = f"best {max(scored):.4f}" if scored else "unscored"
        kept = "kept" if any(a.get("kept") for a in entry["attempts"]) else "discarded"
        bits.append(f"— {best}, {kept}, {len(entry['attempts'])} attempt(s)")
    if entry["produced"]:
        bits.append("→ produced: " + ", ".join(str(p) for p in entry["produced"]))
    if entry.get("retire_reason"):
        bits.append(f"— {entry['retire_reason']}")
    return "- " + " ".join(bits)


def render_markdown(state: dict[str, dict] | None = None) -> str:
    """Deterministic — no timestamps, so re-rendering unchanged state is a no-op
    and any diff is a real hand edit."""
    state = fold() if state is None else state
    out = [
        "# Research Ideas — GENERATED, do not edit",
        "",
        "Rendered from `ideas/events.jsonl` by `ideas.py render`. Hand edits are",
        "detected and refused on the next render — use `ideas.py propose` /",
        "`ideas.py retire` instead.",
        "",
    ]
    for key, title in SECTIONS:
        rows = [e for e in state.values() if e["state"] == key]
        out.append(f"## {title} ({len(rows)})")
        out.append("")
        out.extend(_describe(e) for e in rows)
        if not rows:
            out.append("- (none)")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render(force: bool = False) -> str:
    """Rewrite the markdown view. Whole-file write, so it takes the lock."""
    with _locked():
        content = render_markdown()
        md = markdown_path()
        if not force and md.exists():
            try:
                stored = _hash_path().read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                stored = ""
            current = _digest(md.read_text(encoding="utf-8"))
            if stored != current:
                raise DriftError(
                    f"{md} changed outside render "
                    f"({'no recorded hash' if not stored else 'hash mismatch'}). "
                    f"Re-run with --force to overwrite, after saving anything you want to keep."
                )
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(content, encoding="utf-8")
        _hash_path().write_text(_digest(content) + "\n", encoding="utf-8")
        return content


# --- CLI -----------------------------------------------------------------


def _bool_arg(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _float_or_none(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="append one idea to the pool")
    p.add_argument("text")
    p.add_argument("--origin", required=True, help="lineage-N | secretary | human")
    p.add_argument("--id", dest="idea_id", default=None)

    p = sub.add_parser("claim", help="reserve one future idea (exit 1 if none)")
    p.add_argument("--lineage", required=True)
    p.add_argument("--idea-id", default=None)
    p.add_argument("--purpose", default="seed", choices=["seed", "test"])

    p = sub.add_parser("complete", help="record an evaluated attempt")
    p.add_argument("--idea-id", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--score", default="")
    p.add_argument("--kept", default="false")

    p = sub.add_parser("seeded", help="mark an idea as consumed for inspiration")
    p.add_argument("--idea-id", required=True)
    p.add_argument("--produced", required=True)

    p = sub.add_parser("retire", help="take an idea out of rotation")
    p.add_argument("--idea-id", required=True)
    p.add_argument("--by", default="secretary")
    p.add_argument("--reason", default="")

    p = sub.add_parser("show", help="print folded state")
    p.add_argument("--json", action="store_true")
    p.add_argument("--state", default=None, help="filter to one state")

    p = sub.add_parser("render", help="rewrite research-ideas.md")
    p.add_argument("--force", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "propose":
        print(propose(a.text, origin=a.origin, idea_id=a.idea_id))
    elif a.cmd == "claim":
        got = claim(lineage=a.lineage, idea_id=a.idea_id, purpose=a.purpose)
        if got is None:
            print("ideas: no claimable idea (empty pool or already taken)", file=os.sys.stderr)
            return 1
        print(got)
    elif a.cmd == "complete":
        complete(a.idea_id, a.candidate, _float_or_none(a.score), _bool_arg(a.kept))
    elif a.cmd == "seeded":
        seeded(a.idea_id, a.produced)
    elif a.cmd == "retire":
        retire(a.idea_id, by=a.by, reason=a.reason)
    elif a.cmd == "show":
        state = fold()
        if a.state:
            state = {k: v for k, v in state.items() if v["state"] == a.state}
        print(json.dumps(state, indent=2, sort_keys=True) if a.json else render_markdown(state))
    elif a.cmd == "render":
        try:
            render(force=a.force)
        except DriftError as e:
            print(f"ideas: {e}", file=os.sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
