#!/usr/bin/env python3
"""reference/<tag>/<seq>.<gpu|cpu>.full (f32 [steps][vocab], kept on the box) -> <seq>.<mode>.top20.tsv committed:
per step the argmax, the top-2 gap in nats and the 20 likeliest ids with their log-probabilities (id:logp)."""
import os, pathlib, sys
import numpy as np
V = int(os.environ.get('US2_VOCAB', 248320))
for f in sorted(pathlib.Path(sys.argv[1]).glob('*.full')):
    out = f.with_name(f.name[:-5] + '.top20.tsv')
    if out.exists(): continue
    a = np.fromfile(f, dtype=np.float32).reshape(-1, V).astype(np.float64)
    lines = ['step\targmax\ttop2_gap\ttop20(id:logp)']
    for s in range(len(a)):
        r = a[s]; m = r.max(); lp = r - (m + np.log(np.exp(r - m).sum()))
        idx = np.argpartition(-lp, 20)[:20]; idx = idx[np.argsort(-lp[idx])]
        lines.append(f'{s}\t{int(idx[0])}\t{lp[idx[0]] - lp[idx[1]]:.4f}\t' + ' '.join(f'{int(i)}:{lp[i]:.5f}' for i in idx))
    out.write_text('\n'.join(lines) + '\n'); print(out.name, len(a), 'steps')
