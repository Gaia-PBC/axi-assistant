"""Trace $DECK variable commands to their source session files.
Uses the same JSONL parsing approach as recover_minflow_ops.py.
"""
import json
import os
import re
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
results = []

for root, dirs, files in os.walk(CLAUDE_PROJECTS):
    for fname in files:
        if not fname.endswith(".jsonl"):
            continue
        filepath = Path(root) / fname
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
                    if ts < "2026-03-24":
                        continue

                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue

                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        cmd = block.get("input", {}).get("command", "")
                        has_var = "$DECK" in cmd or "$D " in cmd or "${DECK" in cmd or "${IDS" in cmd
                        if "minflow" in cmd and has_var:
                            rel = os.path.relpath(str(filepath), str(CLAUDE_PROJECTS))
                            project = rel.split("/")[0]
                            results.append((ts, project, str(filepath), cmd[:200]))
        except (OSError, UnicodeDecodeError):
            continue

results.sort()
seen = {}
for ts, proj, path, cmd in results:
    seen.setdefault(proj, []).append((ts, path, cmd))

print(f"Total $DECK ops: {len(results)}")
print()

# Extract DECK=<value> from the full command
deck_var_re = re.compile(r'(?:DECK|D)="?([a-z0-9]+)"?')
deck_assignments = {}  # deck_id -> count
no_assignment = []

for ts, proj, path, cmd in results:
    m = deck_var_re.search(cmd)
    if m:
        deck_id = m.group(1)
        deck_assignments.setdefault(deck_id, []).append((ts, proj, cmd[:100]))
    else:
        no_assignment.append((ts, proj, cmd[:200]))

print("=== DECK variable assignments found ===")
for deck_id, ops in sorted(deck_assignments.items(), key=lambda x: -len(x[1])):
    projects = set(p for _, p, _ in ops)
    print(f"  {deck_id} ({len(ops)} ops) — projects: {projects}")

print(f"\n=== No DECK assignment found ({len(no_assignment)} ops) ===")
for ts, proj, cmd in no_assignment[:10]:
    print(f"  [{ts}] {proj}")
    print(f"    {cmd[:180]}")
    print()
