#!/usr/bin/env python3
"""Reference runs (CORRECTNESS-PLAN.md 3.1): llama.cpp 3cf03257 CUDA build, tools/qwen35/q36_llama_dump.
ref-run.py <model-tag> <gguf> gpu|cpu [seq ...]
  gpu: --ngl 99, greedy for n_answer steps (or --force on a given answer), writes <seq>.gpu.full + <seq>.gpu.gen.txt and
       inputs/<set>/<seq>.answer.ids when the set has none (the GPU run's own greedy answer).
  cpu: --ngl 0, --force on the answer, writes <seq>.cpu.full.
Full logits are f32 [steps][vocab]; every run also gets <seq>.<mode>.log (stderr) and command.sh."""
import hashlib, json, os, pathlib, subprocess, sys, time
# US2_CORR_ROOT / US2_DUMP: another model's root and reference build (DeepSeek, GLM: 2026-10-19)
ROOT = pathlib.Path(os.environ.get('US2_CORR_ROOT', pathlib.Path.home() / 'us2-results/correctness'))
DUMP = pathlib.Path(os.environ.get('US2_DUMP', pathlib.Path.home() / 'llamacpp-ref/tools/q36_llama_dump'))
tag, gguf, mode = sys.argv[1], sys.argv[2], sys.argv[3]
only = set(sys.argv[4:])

def answer_path(p, seq, tag):
    """The scored answer: the fixed one (<seq>.answer.ids, the real-text set) when it exists, else the reference
    model's own greedy answer <seq>.answer.<tag>.ids (a different file is a different model, so a different answer)."""
    fixed = p.parent / f'{seq}.answer.ids'
    return fixed if fixed.exists() else p.parent / f'{seq}.answer.{tag}.ids'
OUT = ROOT / 'reference' / tag; OUT.mkdir(parents=True, exist_ok=True)
seqs = sorted(ROOT.glob('inputs/*/*.prompt.ids'))
for p in seqs:
    seq = p.name[:-len('.prompt.ids')]; setname = p.parent.name
    if only and seq not in only: continue
    meta = json.load(open(p.parent / f'{seq}.json'))
    prompt = p.read_text().strip()
    ans_path = answer_path(p, seq, tag)
    n = meta['n_answer']
    nctx = ((meta['prompt_tokens'] + n + 8 + 255) // 256) * 256
    env = dict(os.environ, STRATA_DUMP_CTX=str(nctx))
    cmd = [str(DUMP), '--model', gguf, '--tokens', f'@{p}', '--ngl', '99' if mode == 'gpu' else '0',
           '--dump-full-logits', str(OUT / f'{seq}.{mode}.full')]
    if mode == 'cpu' or ans_path.exists():
        ans = ans_path.read_text().strip().split(',')
        # --force N ids runs N+1 steps; the engines score N steps, so force all but the last and let step N be greedy
        forced = OUT / f'{seq}.force.ids'; forced.write_text(','.join(ans[:-1]) + '\n')
        cmd += ['--force', f'@{forced}']
    else:
        cmd += ['--n', str(n)]
    t0 = time.time()
    # errors='replace': the DeepSeek vocabulary's pieces are not all valid UTF-8 in llama.cpp's load log (2026-10-19)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, errors='replace')
    dt = time.time() - t0
    (OUT / f'{seq}.{mode}.log').write_text(r.stderr)
    with open(OUT / 'command.sh', 'a') as f:
        f.write(f'STRATA_DUMP_CTX={nctx} ' + ' '.join(cmd) + '\n')
    if r.returncode != 0:
        print(f'{setname}/{seq} {mode}: EXIT {r.returncode} after {dt:.0f} s; see {seq}.{mode}.log', flush=True); continue
    gen = [l for l in r.stdout.splitlines() if l.startswith('gen:')][0].split()[1:]
    (OUT / f'{seq}.{mode}.gen.txt').write_text(','.join(gen) + '\n')
    if mode == 'gpu' and not ans_path.exists():
        ans_path.write_text(','.join(gen[:n]) + '\n')
    full = OUT / f'{seq}.{mode}.full'
    sha = hashlib.sha256(full.read_bytes()).hexdigest()
    (OUT / f'{seq}.{mode}.full.sha256').write_text(f'{sha}  {full.name}\n')
    print(f'{setname}/{seq} {mode}: {len(gen)} steps, nctx {nctx}, {dt:.0f} s, {full.stat().st_size/1e6:.0f} MB', flush=True)
