#!/usr/bin/env python3
"""Forensic analysis of axi-master transcript - extract all user/assistant text."""
import json
import sys

path = "/home/pride/.claude/projects/-home-pride-coding-projects-personal-assistant/84324347-9d3b-48d4-b151-425871b048b5.jsonl"

records = []
for i, line in enumerate(open(path)):
    try:
        rec = json.loads(line)
        rec['_line'] = i + 1
        records.append(rec)
    except Exception as e:
        print(f"Line {i+1} error: {e}", file=sys.stderr)

def extract_text(content):
    """Extract text from content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get('type')
                if t == 'text':
                    parts.append(item.get('text', ''))
                elif t == 'tool_use':
                    name = item.get('name', '?')
                    inp = item.get('input', {})
                    parts.append(f"[TOOL_USE: {name}] input={json.dumps(inp)[:2000]}")
                elif t == 'tool_result':
                    c = item.get('content', '')
                    if isinstance(c, list):
                        c = ' '.join(extract_text(x) if isinstance(x, (str, list, dict)) else str(x) for x in c)
                    parts.append(f"[TOOL_RESULT] {str(c)[:2000]}")
            else:
                parts.append(str(item))
        return '\n'.join(parts)
    return str(content)

# Print brief summary of each message
for r in records:
    t = r.get('type')
    if t not in ('user', 'assistant'):
        continue
    msg = r.get('message', {})
    role = msg.get('role', '?') if isinstance(msg, dict) else '?'
    content = msg.get('content', '') if isinstance(msg, dict) else ''
    text = extract_text(content)
    ts = r.get('timestamp', '')
    sidechain = r.get('isSidechain', False)
    # Truncate for overview
    preview = text[:300].replace('\n', ' | ')
    print(f"L{r['_line']:4d} {ts} {role:10s} sc={sidechain}  {preview}")
