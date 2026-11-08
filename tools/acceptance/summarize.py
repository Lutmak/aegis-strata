#!/usr/bin/env python3
"""summarize.py [runs-dir] -> a Markdown table of every run's meta.json (one row per run, newest last), and a per-model
pass count. Used for the step-13 write-up."""
import json, sys, pathlib
runs = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).parent / "runs")
rows = []
for d in sorted(runs.iterdir()):
    m = d / "meta.json"
    if not m.exists(): continue
    j = json.loads(m.read_text()); j["run"] = d.name; j["notes"] = (d / "harness-notes.txt").read_text().strip().replace("\n", "; ") if (d / "harness-notes.txt").exists() else ""
    rows.append(j)
print("| Run | Model | Task | T | Pass | Wall s | Turns | Tool calls | In tok | Out tok | Errors | Judge | Harness notes |")
print("| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
for j in rows:
    judge = {k: v for k, v in j["judge"].items() if k != "pass"}
    print(f"| `{j['run']}` | {j['model']} | {j['task']} | {j['temperature']} | {'**pass**' if j['judge'].get('pass') else 'fail'} | {j['wall_s']} | {j['assistant_turns']} | {j['tool_calls']} | {j['input_tokens']} | {j['output_tokens']} | {len(j['errors'])} | {json.dumps(judge)[:120].replace('|', '/')} | {j['notes'][:160]} |")
print()
by = {}
for j in rows: by.setdefault(j["model"], []).append(j)
print("| Model | Runs | Passed | Tasks passed |"); print("| --- | ---: | ---: | --- |")
for m, js in by.items():
    print(f"| {m} | {len(js)} | {sum(1 for j in js if j['judge'].get('pass'))} | {', '.join(sorted({j['task'] + ('@' + str(j['temperature']) if j['temperature'] != 0.6 else '') for j in js if j['judge'].get('pass')}))} |")
