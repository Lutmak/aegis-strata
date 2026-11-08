# Model acceptance runs (PLAN.md step 13)

A model family is accepted when a coding agent gets real work done through the fork's server with that model, under
the sampling the model card asks for, and when the API-only checks (`frontend/tests/acceptance-real-engine.sh`) pass
with decode and prompt-reading speed within 15 % of the step-6/9 figures for the same box.

## What runs

The agent is [pi](https://pi.dev) 0.85.1 in a Docker container (`Dockerfile`, image `us2-acceptance:0.85.1`: node 22,
pi, python3 + pytest, git, make, gcc; user `node`, no network but the model server). It gets one prompt
(`tasks/<task>/prompt.md`), its tools on, a fresh work directory, and no extensions, skills or context files. The
model server is outside the container, reached as `host.docker.internal:<port>`; `models.tsv` maps the model id to
the port on this Mac (an ssh tunnel to the box that serves it).

| Task | Work | Judge (`judge.sh`) |
| --- | --- | --- |
| `md-toc` | `mdtoc.py`, a Markdown table-of-contents CLI with GitHub anchors, fenced-block skipping, setext headings, depth filters, and its own pytest file | 4 hidden pytest cases (`tasks/md-toc/hidden/`), run in the image |
| `kvstore` | `kvstore.py`, an HTTP key-value store (PUT/GET/DELETE, list, /health, 1 MiB limit, persistence across restart) | `tasks/kvstore/blackbox.py`: starts it, drives every route, restarts it, checks persistence |
| `bugfix` | three planted bugs in a seeded package (`tasks/bugfix/seed/`: interval merge, slugify, duration parsing); tests are the spec | the seed's tests plus hidden ones; `tests/test_pkg.py` must be byte-identical to the seed |
| `minesweeper` | a single-file browser Minesweeper (9x9, 10 mines, flood fill, flags, counter, timer, new game) | headless Chromium (lookingglass): 81 `.cell`s, a click reveals cells, no console error; screenshot kept |

`run-task.sh <model> <task> [tag]` does one run: writes a pi config dir (provider `UnbiasedStrata 2`, sampling
temperature 0.6, top_k 20, top_p 0.95, seed 7; `TEMP=`, `SEED=`, `THINKING=` override), starts the container, opens
a window in tmux session `us2-acceptance` that tails the agent's turns (`tmux attach -t us2-acceptance`), waits
(`TIMEOUT_S`, default 90 min), judges, and writes `runs/<utc>-<model>-<task>[-tag]/`: `pi.jsonl` (pi's JSON event
stream), `pi.log`, `work/`, `judge.json`, `meta.json` (exit code, wall time, turns, tool calls, token counts, errors),
`server-before/after.json` (`/props`), and the pi config used. `run-all.sh <model>` runs the four tasks at the
defaults and minesweeper again at temperature 1.0, then appends to `runs/summary.tsv`.

## Reading a run

`meta.json` → `judge.pass` is the verdict. A failed judge with `pi_exit` 0 is the model's work falling short; `pi_exit`
non-zero or `errors` non-empty points at the server (`server-after.json`, the server log on the box). The pretty
tail in the tmux window is `pretty.py` over `pi.jsonl`.
