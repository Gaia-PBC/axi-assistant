#!/usr/bin/env python3
"""Extract full content of specific JSONL lines."""
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
        pass

def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get('type')
                if t == 'text':
                    parts.append(f"--- TEXT ---\n{item.get('text', '')}")
                elif t == 'tool_use':
                    name = item.get('name', '?')
                    inp = item.get('input', {})
                    parts.append(f"--- TOOL_USE: {name} ---\ninput=\n{json.dumps(inp, indent=2)}")
                elif t == 'tool_result':
                    c = item.get('content', '')
                    if isinstance(c, list):
                        c = '\n'.join(extract_text(x) if isinstance(x, (str, list, dict)) else str(x) for x in c)
                    parts.append(f"--- TOOL_RESULT ---\n{str(c)}")
                elif t == 'thinking':
                    parts.append(f"--- THINKING ---\n{item.get('thinking', '')}")
            else:
                parts.append(str(item))
        return '\n'.join(parts)
    return str(content)

target_lines = [int(x) for x in sys.argv[1:]]
for r in records:
    if r['_line'] not in target_lines:
        continue
    msg = r.get('message', {})
    role = msg.get('role', '?') if isinstance(msg, dict) else '?'
    content = msg.get('content', '') if isinstance(msg, dict) else ''
    ts = r.get('timestamp', '')
    print(f"{'='*80}")
    print(f"LINE {r['_line']} | {ts} | role={role} | type={r.get('type')}")
    print(f"{'='*80}")
    print(extract_text(content))
    print()
