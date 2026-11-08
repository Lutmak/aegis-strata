#!/usr/bin/env python3
"""Score every engine run that has both reference files. score-all.py <poly|strata> <tag-or-arm-root> ...
  poly:   score-all.py poly <file-tag> <ref-tag> <arm>      e.g. poly qwen36-q3km qwen36-q3km base
  strata: score-all.py strata <ref-tag> <arm>               (ref-tag is the Flash-Next reference directory)
Writes scores/<tag>/<arm>/<seq>.tsv + <seq>.json and summary.md (per set: steps, top-1, KL, floor, ratio, T1, T2)."""
import json, os, pathlib, subprocess, sys
ROOT = pathlib.Path(os.environ.get('US2_CORR_ROOT', pathlib.Path.home() / 'us2-results/correctness'))
kind = sys.argv[1]
if kind == 'poly':
    tag, ref, arm = sys.argv[2:5]; runs = ROOT / 'poly' / tag / arm; glob = '*.top.txt'; strip = '.top.txt'
else:
    ref, arm = sys.argv[2:4]; tag = 'flash-next'; runs = ROOT / 'strata' / arm; glob = '*.scored.tsv'; strip = '.scored.tsv'
out = ROOT / 'scores' / tag / arm; out.mkdir(parents=True, exist_ok=True)
sets = {p.name[:-len('.prompt.ids')]: p.parent.name for p in ROOT.glob('inputs/*/*.prompt.ids')}
rows = []
for f in sorted(runs.glob(glob)):
    seq = f.name[:-len(strip)]
    g, c = ROOT / 'reference' / ref / f'{seq}.gpu.full', ROOT / 'reference' / ref / f'{seq}.cpu.full'
    if not (g.exists() and c.exists()): print(f'{seq}: reference incomplete'); continue
    cmd = ['python3', str(ROOT / 'score.py'), kind, str(g), str(c), str(f)] + (['0'] if kind == 'strata' else []) + [str(out / f'{seq}.tsv')]
    r = subprocess.run(cmd, capture_output=True, text=True)
    js = [l for l in r.stderr.splitlines() if l.startswith('{')]
    if not js: print(f'{seq}: scorer failed: {r.stderr[-300:]}'); continue
    s = json.loads(js[-1]); s['seq'] = seq; s['set'] = sets.get(seq, '?')
    (out / f'{seq}.json').write_text(json.dumps(s, indent=1) + '\n'); rows.append(s)
    print(f"{s['set']}/{seq}: {s['steps']} steps, top1 {s['top1_agree']:.3f} (floor {s['floor']['top1_agree']:.3f}), "
          f"KL {s['kl_top20_mean']:.5f} vs floor top20 {s['floor']['kl_top20_mean']:.5f} (x{s['t2_ratio_vs_top20_floor']:.2f}; full-vocab floor {s['floor']['kl_full_mean']:.5f}), "
          f"T1 {s['T1']} ({s['t1_positions']} positions, {len(s['t1_failures'])} fail), T2 {s['T2']}")
# summary
lines = [f'# Scores: {tag} / {arm} against reference {ref}', '',
         '| Set | Seq | Steps | Top-1 agree | Floor top-1 | KL top20 (mean / p99) | Floor KL top20 | Ratio | Floor KL full (mean / p99) | T1 (positions >= 0.5 nat / failures) | T2 |',
         '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
for s in sorted(rows, key=lambda s: (s['set'], s['seq'])):
    fl = s['floor']
    lines.append(f"| {s['set']} | {s['seq']} | {s['steps']} | {s['top1_agree']:.3f} | {fl['top1_agree']:.3f} | {s['kl_top20_mean']:.5f} / {s['kl_top20_p99']:.4f} | "
                 f"{fl['kl_top20_mean']:.5f} | {s['t2_ratio_vs_top20_floor']:.2f} | {fl['kl_full_mean']:.5f} / {fl['kl_full_p99']:.4f} | {s['T1']} ({s['t1_positions']} / {len(s['t1_failures'])}) | {s['T2']} |")
if rows:
    import statistics
    agg = {}
    for s in rows: agg.setdefault(s['set'], []).append(s)
    lines += ['', '## Per set (steps-weighted)', '', '| Set | Steps | Top-1 agree | Floor top-1 | KL top20 | Floor KL top20 | Ratio | T1 failures | T2 |', '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for k, v in sorted(agg.items()):
        n = sum(s['steps'] for s in v)
        w = lambda key, sub=None: sum((s[key] if sub is None else s[sub][key]) * s['steps'] for s in v) / n
        kl, flk = w('kl_top20_mean'), w('kl_top20_mean', 'floor')
        fails = sum(len(s['t1_failures']) for s in v)
        lines.append(f"| {k} | {n} | {w('top1_agree'):.3f} | {w('top1_agree','floor'):.3f} | {kl:.5f} | {flk:.5f} | {kl/flk if flk else float('nan'):.2f} | {fails} | {'pass' if kl <= 1.5*flk else 'FAIL'} |")
    fails = [(s['seq'], f) for s in rows for f in s['t1_failures']]
    lines += ['', f"T1 failures (seq, step, llama argmax, engine argmax, gap): {fails if fails else 'none'}"]
(out / 'summary.md').write_text('\n'.join(lines) + '\n'); print('summary ->', out / 'summary.md')
