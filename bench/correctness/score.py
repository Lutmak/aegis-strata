#!/usr/bin/env python3
"""Scorer (CORRECTNESS-PLAN.md 3.3, 3.4, 3.6). Per position: llama.cpp GPU argmax, its top-2 gap (nats), the engine's
argmax and whether it agrees, top-20 overlap, KL(P || Q) with P = llama.cpp GPU over its top 20 + one bucket for the
rest, Q = the engine's printed top 20 (+ rest = 1 - sum). The floor on the same positions: KL(GPU || CPU) over the full
vocabulary and, for a like-for-like figure, over the same top-20 + rest bucket.

score.py floor <gpu.full> <cpu.full>                              -> one line of JSON
score.py poly  <gpu.full> <cpu.full> <top.txt>  [out.tsv]         -> per-step TSV (stdout or file) + summary JSON on stderr
score.py strata <gpu.full> <cpu.full> <pos.tsv> <first_pos> [out.tsv]  (pos.tsv rows whose position >= first_pos, in order)

Q for an id in P's top 20 that the engine did not print: the engine's rest mass spread evenly over those ids plus
the rest bucket (an upper bound on each missing id's probability is the engine's 20th probability; the spread is
used, and the number of such ids is reported as 'missing').
T1 gate: argmax equal at every position with gap >= 0.5. T2 gate: mean KL <= 1.5 x floor (full-vocab) on the positions."""
import json, os, sys
import numpy as np

V = int(os.environ.get('US2_VOCAB', 248320))   # Qwen 248320; DeepSeek V4 Flash 129280; GLM 5.3 Flash 154880

def load_full(path):
    a = np.fromfile(path, dtype=np.float32)
    assert a.size % V == 0, (path, a.size)
    return a.reshape(-1, V).astype(np.float64)

def logsoftmax(row):
    m = row.max(); return row - (m + np.log(np.exp(row - m).sum()))

def topk_dist(lp, k=20):
    idx = np.argpartition(-lp, k)[:k]; idx = idx[np.argsort(-lp[idx])]
    return idx, np.exp(lp[idx])

def kl_bucketed(p_idx, p_prob, q_map, q_rest):
    """P over its top-20 + rest; Q from a dict id->prob plus its rest mass."""
    missing = [i for i in p_idx if int(i) not in q_map]
    share = q_rest / (len(missing) + 1) if missing else q_rest
    kl = 0.0; q_used = 0.0
    for i, p in zip(p_idx, p_prob):
        q = q_map.get(int(i), share); q_used += q
        kl += p * np.log(p / max(q, 1e-300))
    p_rest = max(1.0 - p_prob.sum(), 1e-300); q_rest_b = max(1.0 - q_used, 1e-300)
    kl += p_rest * np.log(p_rest / q_rest_b)
    return kl, len(missing)

def floor(gpu, cpu):
    n = min(len(gpu), len(cpu)); kls = []; kl_b = []; agree = 0
    for s in range(n):
        lp = logsoftmax(gpu[s]); lq = logsoftmax(cpu[s])
        kls.append(float((np.exp(lp) * (lp - lq)).sum()))
        agree += int(lp.argmax() == lq.argmax())
        pi, pp = topk_dist(lp); qi, qp = topk_dist(lq)
        k, _ = kl_bucketed(pi, pp, dict(zip(map(int, qi), qp)), 1.0 - qp.sum()); kl_b.append(k)
    return dict(steps=n, kl_full_mean=float(np.mean(kls)), kl_full_p99=float(np.percentile(kls, 99)),
                kl_top20_mean=float(np.mean(kl_b)), top1_agree=agree / n)

def read_engine(kind, path, first_pos=0):
    """-> list of (engine_argmax, {id: prob}) per scored step, in order."""
    out = []
    for line in open(path):
        if kind == 'poly':
            if not line.startswith('top '): continue
            pairs = [t.split(':') for t in line.split()[1:]]
            m = {int(i): float(p) for i, p in pairs}
        else:
            f = line.rstrip('\n').split('\t')
            if len(f) < 9 or int(f[0]) < first_pos: continue
            m = {int(t.split(':')[0]): float(np.exp(float(t.split(':')[1]))) for t in f[8:]}
        top = max(m, key=m.get)
        out.append((top, m))
    return out

def score(gpu, cpu, eng, out, label):
    n = min(len(gpu), len(eng))
    if len(eng) != len(gpu): print(f'note: {len(gpu)} reference steps, {len(eng)} engine steps; scoring {n}', file=sys.stderr)
    fl = floor(gpu[:n], cpu[:n])
    rows = []; kls = []; agree = 0; t1_pos = 0; t1_fail = []; overlaps = []; missing_total = 0
    for s in range(n):
        lp = logsoftmax(gpu[s]); pi, pp = topk_dist(lp)
        lq = logsoftmax(cpu[s]); qi, qp = topk_dist(lq)
        f_full = float((np.exp(lp) * (lp - lq)).sum())
        f_b, _ = kl_bucketed(pi, pp, dict(zip(map(int, qi), qp)), 1.0 - qp.sum())
        cpu_agree = int(lq.argmax() == pi[0])
        gap = float(lp[pi[0]] - lp[pi[1]])
        e_top, q = eng[s]
        kl, miss = kl_bucketed(pi, pp, q, 1.0 - sum(q.values())); missing_total += miss
        ov = len(set(map(int, pi)) & set(q)) / 20
        a = int(e_top == int(pi[0])); agree += a; kls.append(kl); overlaps.append(ov)
        if gap >= 0.5:
            t1_pos += 1
            if not a: t1_fail.append((s, int(pi[0]), e_top, gap))
        rows.append(f'{s}\t{int(pi[0])}\t{gap:.4f}\t{e_top}\t{a}\t{ov:.2f}\t{kl:.6f}\t{miss}\t{cpu_agree}\t{f_full:.6f}\t{f_b:.6f}')
    out.write('step\tllama_argmax\tllama_top2_gap\tengine_argmax\tagree\ttop20_overlap\tkl_top20\tmissing\tcpu_agree\tfloor_kl_full\tfloor_kl_top20\n' + '\n'.join(rows) + '\n')
    s = dict(label=label, steps=n, top1_agree=agree / n, kl_top20_mean=float(np.mean(kls)), kl_top20_p99=float(np.percentile(kls, 99)),
             top20_overlap_mean=float(np.mean(overlaps)), missing_ids=missing_total, t1_positions=t1_pos, t1_failures=t1_fail,
             floor=fl, t2_ratio_vs_full_floor=float(np.mean(kls)) / fl['kl_full_mean'] if fl['kl_full_mean'] > 0 else None,
             t2_ratio_vs_top20_floor=float(np.mean(kls)) / fl['kl_top20_mean'] if fl['kl_top20_mean'] > 0 else None)
    s['T1'] = 'pass' if not t1_fail else 'FAIL'
    # T2 like for like: the engine prints its top 20, so its KL is the bucketed one; the floor used for the gate is the
    # same bucketed KL between the two reference runs. The full-vocabulary floor is reported beside it.
    s['T2'] = 'pass' if s['t2_ratio_vs_top20_floor'] is not None and s['t2_ratio_vs_top20_floor'] <= 1.5 else 'FAIL'
    print(json.dumps(s), file=sys.stderr)

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'floor':
        print(json.dumps(floor(load_full(sys.argv[2]), load_full(sys.argv[3]))))
    elif mode == 'poly':
        gpu, cpu = load_full(sys.argv[2]), load_full(sys.argv[3]); eng = read_engine('poly', sys.argv[4])
        out = open(sys.argv[5], 'w') if len(sys.argv) > 5 else sys.stdout
        score(gpu, cpu, eng, out, sys.argv[4])
    elif mode == 'strata':
        gpu, cpu = load_full(sys.argv[2]), load_full(sys.argv[3]); eng = read_engine('strata', sys.argv[4], int(sys.argv[5]))
        # <seq>.offset.txt beside the scored rows: the rows start at reference step k (a resumed read loses step 0)
        import os
        off_path = sys.argv[4].replace('.scored.tsv', '.offset.txt')
        k = int(open(off_path).read().strip()) if os.path.exists(off_path) else 0
        if k: print(f'note: engine rows start at reference step {k}', file=sys.stderr)
        out = open(sys.argv[6], 'w') if len(sys.argv) > 6 else sys.stdout
        score(gpu[k:], cpu[k:], eng, out, sys.argv[4])
