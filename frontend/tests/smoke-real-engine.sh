#!/bin/bash
# First end-to-end checks of the UnbiasedStrata 2 front end against a real engine. Raw responses are kept in $OUT.
#   us2-smoke.sh http://127.0.0.1:18090 <outdir> <model-alias>
set -u
URL=$1; OUT=$2; MODEL=$3; mkdir -p "$OUT"
j() { python3 -c 'import json,sys; d=json.load(sys.stdin); r=eval(sys.argv[1]); print(json.dumps(r) if isinstance(r,(dict,list)) else r)' "$1" 2>/dev/null; }
post() { curl -s -m "${3:-300}" "$URL$1" -H 'content-type: application/json' -d "$2"; }

echo "== /health";    curl -s "$URL/health" | tee "$OUT/health.json"; echo
echo "== /v1/models"; curl -s "$URL/v1/models" | tee "$OUT/models.json" | j '[(m["id"], m.get("context_length")) for m in d["data"]]'

echo "== chat, greedy, no thinking (the ids are compared with upstream later)"
post /v1/chat/completions "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Name three prime numbers greater than 100, one per line, nothing else.\"}],\"max_tokens\":64,\"temperature\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}" > "$OUT/chat-greedy.json"
j '{"content": d["choices"][0]["message"]["content"], "finish": d["choices"][0]["finish_reason"], "usage": d["usage"], "timings": d.get("timings")}' < "$OUT/chat-greedy.json" || cat "$OUT/chat-greedy.json"

echo "== chat, sampled as upstream (temp 0.6 top_p 0.95 top_k 20), thinking on"
post /v1/chat/completions "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 17 * 23? Answer with the number only.\"}],\"max_tokens\":512,\"temperature\":0.6,\"top_p\":0.95,\"top_k\":20}" > "$OUT/chat-thinking.json"
j '{"reasoning_chars": len(d["choices"][0]["message"].get("reasoning_content") or ""), "content": d["choices"][0]["message"]["content"], "finish": d["choices"][0]["finish_reason"], "completion_tokens": d["usage"]["completion_tokens"], "timings": d.get("timings")}' < "$OUT/chat-thinking.json" || cat "$OUT/chat-thinking.json"

echo "== streaming: chunks, reasoning deltas, content, finish"
curl -s -N -m 300 "$URL/v1/chat/completions" -H 'content-type: application/json' -d "{\"model\":\"$MODEL\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Write one sentence about the sea.\"}],\"max_tokens\":200,\"temperature\":0.6,\"top_p\":0.95,\"top_k\":20}" > "$OUT/stream.sse"
python3 - "$OUT/stream.sse" <<'PY'
import json,sys
chunks=[json.loads(l[6:]) for l in open(sys.argv[1]) if l.startswith('data: {')]
done=any(l.strip()=='data: [DONE]' for l in open(sys.argv[1]))
r=''.join(c['choices'][0]['delta'].get('reasoning_content') or '' for c in chunks if c['choices'])
t=''.join(c['choices'][0]['delta'].get('content') or '' for c in chunks if c['choices'])
fin=[c['choices'][0]['finish_reason'] for c in chunks if c['choices'] and c['choices'][0].get('finish_reason')]
print(json.dumps({"chunks":len(chunks),"reasoning_chars":len(r),"content":t,"finish":fin,"done":done}))
PY

echo "== tool call: one function, pi-shaped request"
post /v1/chat/completions "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Read the file /etc/hostname using the read tool.\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"read\",\"description\":\"Read a file\",\"parameters\":{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"},\"offset\":{\"type\":\"integer\"},\"limit\":{\"type\":\"integer\"}},\"required\":[\"path\"]}}}],\"max_tokens\":512,\"temperature\":0.6,\"top_p\":0.95,\"top_k\":20}" > "$OUT/toolcall.json"
j '{"tool_calls": d["choices"][0]["message"].get("tool_calls"), "content": d["choices"][0]["message"].get("content"), "finish": d["choices"][0]["finish_reason"]}' < "$OUT/toolcall.json" || cat "$OUT/toolcall.json"

echo "== /health after"; curl -s "$URL/health"; echo
