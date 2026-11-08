#!/bin/bash
# rejudge.sh <run-dir>... : run judge.sh again on finished runs (after a judge fix) and update judge.json + meta.json.
set -u; HERE=$(cd "$(dirname "$0")" && pwd)
for RUN in "$@"; do
  task=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task"])' "$RUN/meta.json")
  "$HERE/judge.sh" "$task" "$RUN/work" > "$RUN/judge.json" 2> "$RUN/judge.err"; jrc=$?
  python3 - "$RUN" "$jrc" <<'PY'
import json, sys, pathlib
run, jrc = pathlib.Path(sys.argv[1]), int(sys.argv[2]); meta = json.loads((run / "meta.json").read_text())
try: judge = json.loads((run / "judge.json").read_text().strip().splitlines()[-1])
except Exception: judge = {"pass": False, "why": "judge output unreadable"}
meta["judge"], meta["judge_exit"] = judge, jrc; (run / "meta.json").write_text(json.dumps(meta, indent=1))
print(run.name, json.dumps(judge))
PY
done
