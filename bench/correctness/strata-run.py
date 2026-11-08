#!/usr/bin/env python3
"""Configuration A arms (CORRECTNESS-PLAN.md 3.4) on the A6000 VM: a fresh `strata --serve` per sequence, the whole
sequence (prompt + reference answer) sent as one GEN 1 request, STRATA_LOGPOS scoring every position read through the
verify windows (--short-read covers the sequence). strata-run.py <arm> [seq ...]
arms: fp16    = --kv fp16, suffix drafter on (arm 1)
      t4      = fp16 + --suffix-draft 0 (arm 2; pos.tsv log-probabilities must equal arm 1's)
      int8    = --kv int8 (arm 3)
      int8str = --kv int8 --kv-resident 20480 --short-read 1024, the 31K sequence (arm 4; equals arm 3 on those rows)
      pf512   = fp16 + --prefill 512 --short-read 512 on the long set (arm 5, chunk effect)
      pin     = fp16 + --expert-cache 24380 (the GPU expert cache pinned to one slot count; see below)
      pin2    = pin again, a second process (T0: the same bytes from the same placement)
      pint4   = pin + --suffix-draft 0 (T0: the window size, with the placement held)
      pincpu  = pin + --pcie-frac 0 (every missed expert computed on the CPU, the AVX-2 kernels: the CPU tier arm)
      pingpu  = pin + --pcie-frac 1 (every missed expert fetched over PCIe and computed on the GPU)
On this VM the expert set does NOT fit the card even with --resident-experts (48 GB): the engine keeps 24380-24576
GPU cache slots (--expert-cache auto, sized from the free VRAM at start, so it varies by a few hundred slots from one
process to the next) and a 3.4-3.7 GiB complement in page-locked RAM, whose misses are split between PCIe (GPU
arithmetic) and the CPU pool (--pcie-frac 0.55: 45 % on the CPU, which rounds an expert differently, docs/DETAILS.md
#410). So the bits depend on the slot count: two processes with different counts differ (fp16 vs t4, 2026-10-18);
the pin* arms hold the count (--expert-cache 24380 in max-blob units came out as every expert, 24576; with all of
them on the card no decode miss happens and pin = pin2 = pint4 = pincpu = pingpu bitwise, 2026-10-18).
      cpu     = fp16 + --expert-cache 16000 --pcie-frac 0: a fixed, smaller cache (about 13 % of the experts left in
                RAM), every miss computed on the CPU (AVX-2 kernels): the CPU tier arm, deterministic placement
      cpu2    = cpu again, a second process (T0: the CPU tier repeats)
      pcie    = fp16 + --expert-cache 16000 --pcie-frac 1: the same placement, every miss fetched over PCIe and
                computed on the GPU (T0 against pin: a fetched expert equals a cached one) Writes strata/<arm>/<seq>.pos.tsv, .scored.tsv (rows aligned to the answer),
.engine.log, .done.txt, command.sh."""
import os, pathlib, subprocess, sys, time
# Paths (the defaults are the homelab VM's; set them for another machine): US2_MODELS, the directory holding
# qwen36/<file>.gguf (required); US2_BUILD, the UnbiasedStrata tree with build/bin (default: this script's
# repository); US2_RESULTS, where results go (default ~/us2-results/correctness).
def _models():
    m = os.environ.get('US2_MODELS')
    if not m:
        sys.exit('set US2_MODELS to the directory holding qwen36/')
    return pathlib.Path(m)
BUILD = pathlib.Path(os.environ.get('US2_BUILD') or pathlib.Path(__file__).resolve().parents[2])
ROOT = pathlib.Path(os.environ.get('US2_RESULTS') or pathlib.Path.home() / 'us2-results/correctness')
sys.path.insert(0, str(BUILD / 'tools/qwen35'))
from q36_harness import Eng
EXE = str(BUILD / 'build/bin/strata')
M = str(_models())
BASE = ['--pack', f'{M}/pack-iq3-xxs', '--native', f'{M}/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf',
        '--ple-gguf', f'{M}/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf',
        '--expert-profile', f'{M}/pack-iq3-xxs/expert-profile.bin', '--resident-experts', '--prefill', 'auto',
        '--spec', '4', '--spec-min-p', '0.5', '--mtp', f'{M}/mtp', '--max-context', '65536', '--ple-io', 'mmap',
        '--prompt-cache', '0', '--adapt-every', '0', '--pcie-frac', '0.55']
ARMS = {'fp16':    (['--kv', 'fp16', '--short-read', '4096'], None),
        't4':      (['--kv', 'fp16', '--short-read', '4096', '--suffix-draft', '0'], None),
        'int8':    (['--kv', 'int8', '--short-read', '4096'], None),
        'int8str': (['--kv', 'int8', '--kv-resident', '20480', '--short-read', '1024'], {'d31k'}),
        'pf512':   (['--kv', 'fp16', '--prefill', '512', '--short-read', '512'], {'d4k', 'd16k', 'd31k', 'r16k'}),
        'pin':     (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '24380'], None),
        'pin2':    (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '24380'], {'p00', 'p01', 'lru', 'puzzle', 'd4k'}),
        'pint4':   (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '24380', '--suffix-draft', '0'], {'p00', 'p01', 'lru', 'puzzle', 'd4k'}),
        'pincpu':  (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '24380', '--pcie-frac', '0'], None),
        'pingpu':  (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '24380', '--pcie-frac', '1'], {'p00', 'p01', 'lru', 'puzzle', 'd4k'}),
        'cpu':     (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '16000', '--pcie-frac', '0'], None),
        'cpu2':    (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '16000', '--pcie-frac', '0'], {'p00', 'p01', 'lru', 'puzzle', 'd4k'}),
        'pcie':    (['--kv', 'fp16', '--short-read', '4096', '--expert-cache', '16000', '--pcie-frac', '1'], {'p00', 'p01', 'lru', 'puzzle', 'd4k'})}
arm = sys.argv[1]; only = set(sys.argv[2:])
REF_TAG = 'flash-next'

def answer_path(p, seq, tag):
    """The scored answer: the fixed one (<seq>.answer.ids, the real-text set) when it exists, else the reference
    model's own greedy answer <seq>.answer.<tag>.ids (a different file is a different model, so a different answer)."""
    fixed = p.parent / f'{seq}.answer.ids'
    return fixed if fixed.exists() else p.parent / f'{seq}.answer.{tag}.ids'
flags, default_seqs = ARMS[arm]
OUT = ROOT / 'strata' / arm; OUT.mkdir(parents=True, exist_ok=True)
for p in sorted(ROOT.glob('inputs/*/*.prompt.ids')):
    seq = p.name[:-len('.prompt.ids')]
    if only and seq not in only: continue
    if not only and default_seqs and seq not in default_seqs: continue
    ans_path = answer_path(p, seq, REF_TAG)
    if not ans_path.exists(): print(f'{seq}: no answer ids', flush=True); continue
    prompt = list(map(int, p.read_text().strip().split(','))); ans = list(map(int, ans_path.read_text().strip().split(',')))
    pos = OUT / f'{seq}.pos.tsv'
    env = {'STRATA_LOGPOS': str(pos), 'STRATA_LOGPOS_TOPK': '20', 'STRATA_ROUTE_TAIL_SKIP': '0', 'STRATA_PREFILL_CPU_SHARE': '0',
           'STRATA_IQ_MT_MIN': '1', 'CUDA_MODULE_LOADING': 'EAGER'}
    args = BASE + flags
    # --short-read N sends a fresh read through the verify windows only when the WHOLE read is at most N tokens (one
    # part per text request: generate.cpp windows_ok); a longer read goes through the batched prompt path and writes
    # no LOGPOS rows. So a sequence longer than N is sent as two requests: the prompt alone (read batched, kept as a
    # conversation checkpoint: --prompt-cache 1), then prompt + answer, which resumes and reads only the answer through
    # the windows. That is also how a chat reaches the answer positions: the document through the prompt path, the
    # answer through the windows.
    short_read = int(args[args.index('--short-read') + 1])
    two_requests = len(prompt) + len(ans) > short_read
    if two_requests:
        i = args.index('--prompt-cache'); args[i + 1] = '1'
    with open(OUT / 'command.sh', 'a') as f:
        f.write(' '.join(f'{k}={v}' for k, v in env.items()) + f' {EXE} --serve ' + ' '.join(args) +
                (f'  # GEN 1 $(cat {p}); then ' if two_requests else '  # ') + f'GEN 1 $(cat {p}),$(cat {ans_path})\n')
    t0 = time.time()
    try:
        e = Eng(EXE, args, env=env, log=str(OUT / f'{seq}.engine.log'))
        if two_requests:
            e.solo(prompt, 1)
        toks, done, ttft, wall = e.solo(prompt + ans, 1)
        e.send('QUIT'); e.p.wait(timeout=120)
    except SystemExit as ex:
        print(f'{p.parent.name}/{seq} {arm}: FAILED {ex}', flush=True); continue
    (OUT / f'{seq}.done.txt').write_text(done + '\n')
    # align: the rows whose target ids are the answer, in order
    rows = [l.rstrip('\n').split('\t') for l in open(pos)] if pos.exists() else []
    # a resumed read (two requests) starts after the prompt's last token, whose logits the first request consumed: the
    # rows then target answer[1:], so the match is tried from answer[k:] and k is written beside the rows (step offset)
    tg = [int(r[1]) for r in rows]
    start = k = None
    for k in range(4):
        a = ans[k:]
        start = next((i for i in range(len(tg) - len(a) + 1) if tg[i:i + len(a)] == a), None)
        if start is not None: break
    if start is None:
        print(f'{p.parent.name}/{seq} {arm}: {len(rows)} LOGPOS rows, answer not found in the targets; load {e.load_s:.0f} s', flush=True); continue
    (OUT / f'{seq}.scored.tsv').write_text('\n'.join('\t'.join(r) for r in rows[start:start + len(ans) - k]) + '\n')
    (OUT / f'{seq}.offset.txt').write_text(f'{k}\n')
    print(f'{p.parent.name}/{seq} {arm}: {len(rows)} rows, answer[{k}:] at row {start} (pos {rows[start][0]}), load {e.load_s:.0f} s, request {wall:.0f} s, {done}', flush=True)
