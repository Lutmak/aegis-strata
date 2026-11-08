#!/usr/bin/env python3
"""Turn pi's --mode json event stream (stdin) into a readable tail for the tmux window."""
import json, sys
for line in sys.stdin:
    try: ev = json.loads(line)
    except Exception: sys.stdout.write(line); sys.stdout.flush(); continue
    t = ev.get("type", "")
    if t == "message_end":
        m = ev.get("message", {})
        if m.get("role") == "assistant":
            u = m.get("usage") or {}
            for c in m.get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip(): print(f"\n[assistant] {c['text'].strip()[:1500]}")
                elif c.get("type") == "toolCall": print(f"[tool call] {c.get('name')} {json.dumps(c.get('arguments', {}))[:300]}")
            print(f"  (in {u.get('input')} out {u.get('output')} stop {m.get('stopReason')})")
    elif t == "tool_execution_end":
        r = ev.get("result", {}); txt = ""
        for c in (r.get("content") or []):
            if c.get("type") == "text": txt += c.get("text", "")
        print(f"[tool result] {txt.strip()[:400]}")
    sys.stdout.flush()
