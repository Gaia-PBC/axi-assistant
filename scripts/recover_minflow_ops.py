#!/usr/bin/env python3
"""
Recover MinFlow mutation history from Claude Code session logs.

Scans all JSONL session files under ~/.claude/projects/, extracts minflow
mutation commands (card add/done/update/delete/reorder, deck update/create,
undo, redo), pairs them with their tool results to filter out failures,
and outputs a chronologically sorted replay log.

Usage:
    python scripts/recover_minflow_ops.py [--since YYYY-MM-DD] [--replay] [--dry-run]

Options:
    --since DATE    Only include operations on or after this date (default: 2026-03-24)
    --replay        Output as executable shell commands (default: human-readable log)
    --dry-run       With --replay, prefix each command with 'echo' for safe testing
    --json          Output as JSON array for programmatic consumption
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Patterns for mutation commands (not reads)
MUTATION_PATTERNS = [
    r"minflow\s+card\s+add\b",
    r"minflow\s+card\s+done\b",
    r"minflow\s+card\s+update\b",
    r"minflow\s+card\s+delete\b",
    r"minflow\s+card\s+reorder\b",
    r"minflow\s+deck\s+update\b",
    r"minflow\s+deck\s+create\b",
    r"minflow\s+deck\s+delete\b",
    r"minflow\s+undo\b",
    r"minflow\s+redo\b",
]
MUTATION_RE = re.compile("|".join(MUTATION_PATTERNS))

# Patterns for read-only commands to explicitly exclude
READ_PATTERNS = [
    r"minflow\s+deck\s+list\b",
    r"minflow\s+deck\s+get\b",
    r"minflow\s+card\s+list\b",
]
READ_RE = re.compile("|".join(READ_PATTERNS))


def is_mutation_command(cmd: str) -> bool:
    """Check if a command contains a minflow mutation (and isn't just a read)."""
    if not MUTATION_RE.search(cmd):
        return False
    # Skip commands where minflow appears only inside a string literal
    # (e.g., Python scripts that mention "minflow card add" as a pattern)
    stripped = cmd.strip()
    # If the whole command is a heredoc Python/node script, check if minflow
    # appears as an actual command (start of line) vs inside a string
    if stripped.startswith(("python", "node", "cat ")) and "<<" in stripped:
        # Check if there's a top-level minflow command outside the heredoc
        heredoc_start = stripped.find("<<")
        before_heredoc = stripped[:heredoc_start]
        if MUTATION_RE.search(before_heredoc):
            return True
        return False
    # Also skip if it's a grep/search command that mentions minflow as a pattern
    if stripped.startswith(("grep", "rg ", "find ")) and "minflow" in stripped:
        return False
    return True


def resolve_deck_vars(raw_cmd: str, extracted_cmd: str) -> str:
    """Resolve $DECK / $D variables using assignments from the raw command.

    Many commands look like: DECK="mm5jypnollx4dmq2i5h" && minflow card add "$DECK" ...
    The extraction step strips the assignment, leaving $DECK unresolved.
    This function finds the assignment and substitutes it back.
    """
    for pattern in [
        r'\bDECK="([a-z0-9]+)"',
        r'\bDECK=([a-z0-9]+)\b',
        r'\bD="([a-z0-9]+)"',
        r'\bD=([a-z0-9]+)\b',
    ]:
        m = re.search(pattern, raw_cmd)
        if m:
            deck_id = m.group(1)
            result = extracted_cmd
            if 'DECK' in pattern:
                result = result.replace('"$DECK"', deck_id)
                result = result.replace('$DECK', deck_id)
                result = result.replace('${DECK}', deck_id)
            else:
                result = result.replace('"$D"', deck_id)
                result = result.replace('$D ', deck_id + ' ')
                result = result.replace('${D}', deck_id)
            return result
    return extracted_cmd


def extract_minflow_command(full_cmd: str) -> str:
    """Extract the core minflow command from a shell command.

    Handles cases like:
    - "minflow card done deck123 card456 2>&1"
    - "# comment\nminflow card add deck123 ..."
    - "minflow card add ... 2>&1 | python -c ..."
    - "minflow card add ... 2>&1 | head -5"
    - Multi-command with &&
    """
    # Split on && and ; to handle chained commands
    parts = re.split(r'\s*&&\s*|\s*;\s*', full_cmd)
    for part in parts:
        # Strip leading comments
        lines = part.strip().split('\n')
        combined = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            combined.append(line)

        rejoined = '\n'.join(combined).strip()
        if not MUTATION_RE.search(rejoined):
            continue

        # Remove pipe to other commands (| python -c ..., | head -5, etc.)
        # But be careful not to strip pipes inside quoted strings
        # Find the minflow command start
        match = MUTATION_RE.search(rejoined)
        if not match:
            continue

        # Find where "minflow" starts
        cmd_start = rejoined.rfind("minflow", 0, match.end())
        if cmd_start == -1:
            cmd_start = 0

        remainder = rejoined[cmd_start:]

        # Track quote state to find unquoted pipe
        in_single = False
        in_double = False
        escaped = False
        pipe_pos = None
        for i, ch in enumerate(remainder):
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == '|' and not in_single and not in_double:
                pipe_pos = i
                break

        if pipe_pos is not None:
            remainder = remainder[:pipe_pos]

        # Remove stderr/stdout redirects and shell noise
        remainder = re.sub(r'\s*2>&1\s*$', '', remainder)
        remainder = re.sub(r'\s*2>/dev/null\s*$', '', remainder)
        remainder = re.sub(r'\s*>\s*/dev/null\s*$', '', remainder)
        remainder = remainder.strip()

        # If the extraction captured trailing script lines (echo, for, etc.),
        # truncate at the first line that doesn't look like a continuation
        lines = remainder.split('\n')
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            # Stop if we hit a non-continuation line
            if clean_lines and (
                stripped.startswith('echo ')
                or stripped.startswith('done')
                or stripped.startswith('for ')
                or re.match(r'^[A-Z_]+=', stripped)
            ):
                break
            clean_lines.append(line)
        remainder = '\n'.join(clean_lines).strip()

        return remainder

    return full_cmd.strip()


def scan_jsonl(filepath: Path, since_ts: str) -> list[dict]:
    """Scan a JSONL file for minflow mutations.

    Returns list of {timestamp, command, tool_use_id, session_file, success}
    """
    tool_uses = {}  # tool_use_id -> {timestamp, command, raw_command}
    tool_results = {}  # tool_use_id -> {is_error, exit_code}

    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line or "minflow" not in line:
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = obj.get("timestamp", "")
                if ts < since_ts:
                    continue

                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue

                    btype = block.get("type")

                    if btype == "tool_use":
                        inp = block.get("input", {})
                        cmd = inp.get("command", "")
                        if is_mutation_command(cmd):
                            tid = block.get("id", "")
                            extracted = extract_minflow_command(cmd)
                            resolved = resolve_deck_vars(cmd, extracted)
                            tool_uses[tid] = {
                                "timestamp": ts,
                                "raw_command": cmd,
                                "command": resolved,
                            }

                    elif btype == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if tid in tool_uses:
                            is_err = block.get("is_error", False)
                            rc = block.get("content", "")
                            # Check for error indicators in output
                            if isinstance(rc, str):
                                output = rc
                            elif isinstance(rc, list):
                                output = " ".join(
                                    item.get("text", "")
                                    for item in rc
                                    if isinstance(item, dict)
                                )
                            else:
                                output = ""

                            has_error = (
                                is_err
                                or "Error:" in output
                                or "error:" in output
                                or "Traceback" in output
                                or "not found" in output.lower()
                                and "command not found" in output.lower()
                            )

                            tool_results[tid] = {
                                "is_error": has_error,
                                "output_preview": output[:200],
                            }

                # Also check toolUseResult on the parent object
                tur = obj.get("toolUseResult")
                if tur and isinstance(tur, dict):
                    # Match to any pending tool_use via sourceToolAssistantUUID
                    source = obj.get("sourceToolAssistantUUID", "")
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tid = block.get("tool_use_id", "")
                            if tid in tool_uses and tid not in tool_results:
                                stderr = tur.get("stderr", "")
                                interrupted = tur.get("interrupted", False)
                                tool_results[tid] = {
                                    "is_error": interrupted or bool(stderr and "error" in stderr.lower()),
                                    "output_preview": tur.get("stdout", "")[:200],
                                }
    except (OSError, UnicodeDecodeError):
        return []

    # Build results - pair tool_uses with their results
    ops = []
    for tid, info in tool_uses.items():
        result = tool_results.get(tid)
        success = True
        if result and result["is_error"]:
            success = False

        ops.append({
            "timestamp": info["timestamp"],
            "command": info["command"],
            "raw_command": info["raw_command"],
            "tool_use_id": tid,
            "session_file": str(filepath),
            "success": success,
            "result_preview": result["output_preview"] if result else "(no result found)",
        })

    return ops


def main():
    parser = argparse.ArgumentParser(description="Recover MinFlow operations from Claude session logs")
    parser.add_argument("--since", default="2026-03-24", help="Start date (YYYY-MM-DD, default: 2026-03-24)")
    parser.add_argument("--replay", action="store_true", help="Output as executable shell commands")
    parser.add_argument("--dry-run", action="store_true", help="With --replay, prefix commands with echo")
    parser.add_argument("--json", action="store_true", help="Output as JSON array")
    parser.add_argument("--include-failures", action="store_true", help="Include failed operations (marked with # FAILED)")
    parser.add_argument("--verbose", action="store_true", help="Show result previews")
    args = parser.parse_args()

    since_ts = f"{args.since}T00:00:00.000Z"

    # Find all JSONL files
    jsonl_files = []
    if CLAUDE_PROJECTS.exists():
        for root, dirs, files in os.walk(CLAUDE_PROJECTS):
            for fname in files:
                if fname.endswith(".jsonl"):
                    jsonl_files.append(Path(root) / fname)

    print(f"Scanning {len(jsonl_files)} session files...", file=sys.stderr)

    all_ops = []
    for i, fpath in enumerate(jsonl_files):
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(jsonl_files)}...", file=sys.stderr)
        ops = scan_jsonl(fpath, since_ts)
        all_ops.extend(ops)

    # Deduplicate by tool_use_id (same op might appear in main + subagent logs)
    seen = set()
    unique_ops = []
    for op in all_ops:
        if op["tool_use_id"] not in seen:
            seen.add(op["tool_use_id"])
            unique_ops.append(op)

    # Sort chronologically
    unique_ops.sort(key=lambda x: x["timestamp"])

    # Filter
    if not args.include_failures:
        successful = [op for op in unique_ops if op["success"]]
        failed_count = len(unique_ops) - len(successful)
    else:
        successful = unique_ops
        failed_count = 0

    print(f"Found {len(unique_ops)} total operations, {len(successful)} successful, {failed_count} failed (excluded)", file=sys.stderr)

    # Count unresolved vars (needs to run after the flag loop below, but we calculate early for stats)
    unresolved_count = 0
    for op in successful:
        cmd = op["command"]
        notes_pos = cmd.find("--notes")
        done_pos = cmd.find("--done")
        check_end = min(notes_pos if notes_pos >= 0 else len(cmd),
                        done_pos if done_pos >= 0 else len(cmd))
        if re.search(r'(?<!\\)\$[A-Z_{\[]', cmd[:check_end]):
            unresolved_count += 1

    if unresolved_count:
        print(f"  ({unresolved_count} commands have unresolved shell variables — flagged for manual review)", file=sys.stderr)

    if not args.replay and not args.json:
        print(f"\nNOTE: This log cannot be blindly replayed. Cards created after Mar 24 get new IDs,", file=sys.stderr)
        print(f"so subsequent done/update/delete/reorder ops referencing those IDs would fail.", file=sys.stderr)
        print(f"Use --json to get structured data for building a smarter replay, or use this", file=sys.stderr)
        print(f"log as a reference to manually reconstruct the workspace.\n", file=sys.stderr)

    # Flag commands with unresolved variables
    UNRESOLVED_VAR_RE = re.compile(r'(?<!\\)\$[A-Z_{\[]')
    for op in successful:
        cmd = op["command"]
        # Check for unresolved vars NOT inside --notes or --done quoted values
        # Simple heuristic: if $VAR appears before the first --notes/--done flag, it's structural
        notes_pos = cmd.find("--notes")
        done_pos = cmd.find("--done")
        check_region = cmd[:min(notes_pos if notes_pos >= 0 else len(cmd),
                                done_pos if done_pos >= 0 else len(cmd))]
        op["has_unresolved_var"] = bool(UNRESOLVED_VAR_RE.search(check_region))

    if args.json:
        json.dump(successful, sys.stdout, indent=2)
        print()
    elif args.replay:
        for op in successful:
            cmd = op["command"]
            if op["has_unresolved_var"]:
                print(f"# WARNING: unresolved variable — review manually")
                print(f"# TIMESTAMP: {op['timestamp']}")
                print(f"# {cmd}")
            elif args.dry_run:
                print(f"echo '[DRY RUN] would execute:' && echo {repr(cmd[:100])}")
            elif not op["success"]:
                print(f"# FAILED: {cmd}")
            else:
                print(cmd)
    else:
        for op in successful:
            status = "OK" if op["success"] else "FAIL"
            ts = op["timestamp"]
            cmd = op["command"]
            flag = " [UNRESOLVED_VAR]" if op["has_unresolved_var"] else ""
            print(f"[{ts}] [{status}]{flag} {cmd}")
            if args.verbose and op.get("result_preview"):
                preview = op["result_preview"][:100].replace("\n", " ")
                print(f"  -> {preview}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
