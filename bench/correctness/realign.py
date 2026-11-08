#!/usr/bin/env python3
"""Re-align strata/<arm>/<seq>.pos.tsv rows to the reference answer (answer[k:], k in 0..3) -> .scored.tsv + .offset.txt.
realign.py <arm> [seq ...]"""
import pathlib, sys
ROOT = pathlib.Path.home() / 'us2-results/correctness'
arm = sys.argv[1]; only = set(sys.argv[2:])
def answer_path(p, seq):
    fixed = p.parent / f'{seq}.answer.ids'
    return fixed if fixed.exists() else p.parent / f'{seq}.answer.flash-next.ids'
for p in sorted(ROOT.glob('inputs/*/*.prompt.ids')):
    seq = p.name[:-len('.prompt.ids')]
    if only and seq not in only: continue
    pos = ROOT / 'strata' / arm / f'{seq}.pos.tsv'
    if not pos.exists(): continue
    ans = list(map(int, answer_path(p, seq).read_text().strip().split(',')))
    rows = [l.rstrip('\n').split('\t') for l in open(pos)]
    tg = [int(r[1]) for r in rows]
    start = None
    for k in range(4):
        a = ans[k:]
        start = next((i for i in range(len(tg) - len(a) + 1) if tg[i:i + len(a)] == a), None)
        if start is not None: break
    if start is None: print(f'{seq}: {len(rows)} rows, no match'); continue
    (pos.with_name(f'{seq}.scored.tsv')).write_text('\n'.join('\t'.join(r) for r in rows[start:start + len(ans) - k]) + '\n')
    (pos.with_name(f'{seq}.offset.txt')).write_text(f'{k}\n')
    print(f'{seq}: {len(rows)} rows, answer[{k}:] at row {start} (pos {rows[start][0]}), {len(ans) - k} scored')
