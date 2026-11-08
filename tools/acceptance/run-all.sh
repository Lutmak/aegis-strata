#!/bin/bash
# run-all.sh <model-id> [tag] : the step-13 set for one model, sequentially (the server takes one request at a time):
# md-toc, kvstore, bugfix, minesweeper at the model card's sampling defaults (seed 7), then minesweeper again at
# temperature 1.0. Summary lines go to runs/summary.tsv.
set -u
MODEL=${1:?model id}; TAG=${2:-}; HERE=$(cd "$(dirname "$0")" && pwd)
for t in md-toc kvstore bugfix minesweeper; do
  "$HERE/run-task.sh" "$MODEL" "$t" "${TAG:-}" 2>&1 | tail -1
done
TEMP=1.0 "$HERE/run-task.sh" "$MODEL" minesweeper "${TAG:+$TAG-}t1" 2>&1 | tail -1
python3 - "$HERE/runs" "$MODEL" <<'PY'
import json, sys, pathlib
runs, model = pathlib.Path(sys.argv[1]), sys.argv[2]; out = runs / "summary.tsv"
if not out.exists(): out.write_text("run\tmodel\ttask\tpass\tpi_exit\twall_s\tturns\ttool_calls\tinput_tokens\toutput_tokens\ttemperature\tjudge\n")
seen = set(out.read_text().splitlines())
for d in sorted(runs.glob(f"*-{model}-*")):
    m = d / "meta.json"
    if not m.exists(): continue
    j = json.loads(m.read_text())
    line = "\t".join(str(x) for x in (d.name, j["model"], j["task"], j["judge"].get("pass"), j["pi_exit"], j["wall_s"], j["assistant_turns"], j["tool_calls"], j["input_tokens"], j["output_tokens"], j["temperature"], json.dumps(j["judge"])))
    if line not in seen: out.open("a").write(line + "\n")
print(out.read_text())
PY
