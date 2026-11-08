#!/bin/bash
# run-task.sh <model-id> <task> [tag]
# One acceptance run: pi (in the us2-acceptance Docker image, tools on) does <task> in a fresh work dir against the
# fork server that models.tsv maps <model-id> to, a tmux window tails it, then judge.sh scores the result.
# Output: runs/<date>-<model>-<task>[-tag]/ {work/, pi.jsonl, pi.log, judge.json, meta.json, server-before/after.json}
set -u
MODEL=${1:?model id (models.tsv column 1)}; TASK=${2:?task dir under tasks/}; TAG=${3:-}
HERE=$(cd "$(dirname "$0")" && pwd); IMG=us2-acceptance:0.85.1; TMUX_SESSION=us2-acceptance
PORT=$(awk -F'\t' -v m="$MODEL" '$1==m {print $2}' "$HERE/models.tsv"); [ -n "$PORT" ] || { echo "no port for $MODEL in models.tsv" >&2; exit 2; }
[ -f "$HERE/tasks/$TASK/prompt.md" ] || { echo "no tasks/$TASK/prompt.md" >&2; exit 2; }
TEMP=${TEMP:-0.6}; TOPK=${TOPK:-20}; TOPP=${TOPP:-0.95}; SEED=${SEED:-7}; THINKING=${THINKING:-medium}; TIMEOUT_S=${TIMEOUT_S:-5400}
RUN="$HERE/runs/$(date -u +%Y%m%dT%H%MZ)-$MODEL-$TASK${TAG:+-$TAG}"; mkdir -p "$RUN/work" "$RUN/pi-config/agent"
# Served model id: the alias the server announces (first id in /v1/models).
SERVED=$(curl -s -m 5 "http://127.0.0.1:$PORT/v1/models" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
[ -n "$SERVED" ] || { echo "no server on 127.0.0.1:$PORT" >&2; exit 3; }
curl -s -m 5 "http://127.0.0.1:$PORT/props" > "$RUN/server-before.json" 2>/dev/null || true
# pi config dir (PI_CODING_AGENT_DIR): provider -> the fork server through Docker's host alias.
python3 - "$RUN/pi-config/agent" "$SERVED" "$PORT" "$TEMP" "$TOPK" "$TOPP" "$SEED" <<'PY'
import json, sys, pathlib
d, served, port, temp, topk, topp, seed = sys.argv[1:]; d = pathlib.Path(d)
(d / "models.json").write_text(json.dumps({"providers": {"UnbiasedStrata 2": {
    "baseUrl": f"http://host.docker.internal:{port}/v1", "api": "openai-completions", "apiKey": "local",
    "models": [{"id": served, "name": served, "reasoning": True, "input": ["text"], "contextWindow": 32768, "maxTokens": 24576,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "samplingParams": {"temperature": float(temp), "top_k": int(topk), "top_p": float(topp), "min_p": 0, "presence_penalty": 0, "repeat_penalty": 1, "seed": int(seed)},
                "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": True, "maxTokensField": "max_tokens", "requiresReasoningContentOnAssistantMessages": True}}]}}}, indent=1))
(d / "settings.json").write_text(json.dumps({"defaultProvider": "UnbiasedStrata 2", "defaultModel": served, "theme": "dark",
    "quietStartup": True, "enableSkillCommands": False}, indent=1))
(d / "auth.json").write_text("{}\n")
PY
# seed files (e.g. the bugfix package) go into the work dir
[ -d "$HERE/tasks/$TASK/seed" ] && cp -R "$HERE/tasks/$TASK/seed/." "$RUN/work/"
NAME="us2-$(echo "$MODEL-$TASK${TAG:+-$TAG}" | tr -c 'a-zA-Z0-9-\n' '-' | cut -c1-50)-$$"
echo "run dir $RUN"; echo "model $SERVED on 127.0.0.1:$PORT, container $NAME, temp $TEMP seed $SEED thinking $THINKING"
START=$(date +%s); date -u +%FT%TZ > "$RUN/started"
cp "$HERE/tasks/$TASK/prompt.md" "$RUN/pi-config/prompt.md"
# the container: pi, print mode, JSON events on stdout, prompt on stdin, tools on, no host extensions/skills/context files
docker run -d --name "$NAME" --add-host=host.docker.internal:host-gateway --network bridge \
  -e PI_CODING_AGENT_DIR=/pi-config/agent -e PI_OFFLINE=1 -e HOME=/home/node -v "$RUN/pi-config":/pi-config -v "$RUN/work":/work -w /work \
  --memory 2g --cpus 2 --entrypoint /bin/bash $IMG -c \
  "pi -p --mode json --no-extensions --no-skills --no-context-files --no-session --provider 'UnbiasedStrata 2' --model '$SERVED' --thinking $THINKING \"\$(cat /pi-config/prompt.md)\"" > "$RUN/container-id" 2> "$RUN/docker-run.err" || { cat "$RUN/docker-run.err" >&2; exit 4; }
# tmux window to watch it (session created on first use)
if command -v tmux > /dev/null; then
  tmux has-session -t $TMUX_SESSION 2> /dev/null || tmux new-session -d -s $TMUX_SESSION -n hub "echo 'us2 acceptance runs; one window per run'; bash"
  tmux new-window -d -t $TMUX_SESSION -n "$(echo "$MODEL" | cut -c1-14)/$TASK" "docker logs -f $NAME 2>&1 | python3 $HERE/pretty.py; echo; echo '[container exited] run dir $RUN'; bash"
fi
# watchdog: a tool call that runs more than TOOL_CAP_S (default 300 s) is killed inside the container, as a person at the
# terminal would do; pi then sees the tool fail and goes on. Logged to harness-notes.txt.
TOOL_CAP_S=${TOOL_CAP_S:-300}
( while docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -q true; do
    docker exec "$NAME" sh -c "ps -eo pid=,etimes=,comm= | awk '\$1>1 && \$2>$TOOL_CAP_S && \$3!=\"ps\" && \$3!=\"sh\" && \$3!=\"awk\" {print \$1, \$2, \$3}'" 2>/dev/null | while read -r pid et comm; do
      [ -n "$pid" ] && { docker exec "$NAME" kill -9 "$pid" 2>/dev/null; echo "$(date -u +%FT%TZ) watchdog killed pid $pid ($comm) after ${et}s" >> "$RUN/harness-notes.txt"; }
    done; sleep 30; done ) &
WATCHDOG=$!
# wait, with a wall-clock cap
rc=$(timeout "$TIMEOUT_S" docker wait "$NAME" 2>/dev/null); [ -n "$rc" ] || { docker kill "$NAME" > /dev/null 2>&1; rc=124; echo "TIMEOUT after ${TIMEOUT_S}s" >&2; }
END=$(date +%s); kill $WATCHDOG 2>/dev/null
docker logs "$NAME" > "$RUN/pi.jsonl" 2> "$RUN/pi.log"; docker rm "$NAME" > /dev/null 2>&1
curl -s -m 5 "http://127.0.0.1:$PORT/props" > "$RUN/server-after.json" 2>/dev/null || true
# judge
"$HERE/judge.sh" "$TASK" "$RUN/work" > "$RUN/judge.json" 2> "$RUN/judge.err"; jrc=$?
# meta: tokens and turns from the pi event stream
python3 - "$RUN" "$MODEL" "$SERVED" "$TASK" "$PORT" "$rc" "$jrc" "$((END-START))" "$TEMP" "$SEED" "$THINKING" <<'PY'
import json, sys, pathlib
run, model, served, task, port, rc, jrc, secs, temp, seed, thinking = sys.argv[1:]; run = pathlib.Path(run)
inp = out = turns = tools = 0; errs = []
for line in (run / "pi.jsonl").read_text(errors="replace").splitlines():
    try: ev = json.loads(line)
    except Exception: continue
    t = ev.get("type", "")
    if t == "message_end":
        m = ev.get("message", {}); u = m.get("usage") or {}
        if m.get("role") == "assistant":
            turns += 1; inp += int(u.get("input") or 0); out += int(u.get("output") or 0)
            if m.get("stopReason") == "error": errs.append(str(m.get("errorMessage", ""))[:200])
    elif t == "tool_execution_end": tools += 1
try: judge = json.loads((run / "judge.json").read_text().strip().splitlines()[-1])
except Exception: judge = {"pass": False, "why": "judge output unreadable"}
meta = dict(model=model, served_id=served, task=task, port=int(port), pi_exit=int(rc), wall_s=int(secs), assistant_turns=turns,
            tool_calls=tools, input_tokens=inp, output_tokens=out, errors=errs, temperature=float(temp), seed=int(seed),
            thinking=thinking, judge=judge, judge_exit=int(jrc))
(run / "meta.json").write_text(json.dumps(meta, indent=1))
print(json.dumps({k: meta[k] for k in ("model", "task", "pi_exit", "wall_s", "assistant_turns", "tool_calls", "input_tokens", "output_tokens")}), "judge:", json.dumps(judge))
PY
exit $jrc
