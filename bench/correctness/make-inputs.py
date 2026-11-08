#!/usr/bin/env python3
"""Step 12 inputs (CORRECTNESS-PLAN.md 3.2). Writes inputs/<set>/<seq>.prompt.ids (comma-separated token ids) and, for
the real-text set, .answer.ids. Tokenizer: llama-tokenize from the reference build on the Qwen3.6 file (Flash-Next's
tokenizer gives the same ids: checked on prompt 1, 2026-10-18). Chat prompts use Qwen's ChatML; thinking off = empty
<think> block, as tools/qwen35/parity_prompts.ids."""
import json, os, pathlib, subprocess, sys
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
TOK = pathlib.Path.home() / 'llamacpp-ref/build/bin/llama-tokenize'
MODEL = str(_models() / 'qwen36/Qwen3.6-35B-A3B-UD-Q3_K_M.gguf')
SRC = ROOT / 'inputs-src'; OUT = ROOT / 'inputs'

def tokenize(text):
    p = SRC / '_tmp.txt'; p.write_text(text)
    r = subprocess.run([str(TOK), '-m', MODEL, '--ids', '--no-bos', '-f', str(p)], capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith('[')][-1]
    return json.loads(line)

def chat(q, think):
    return ('<|im_start|>user\n' + q + '<|im_end|>\n<|im_start|>assistant\n' +
            ('<think>\n' if think else '<think>\n\n</think>\n\n'))

def write(setname, seq, prompt, answer=None, n_answer=None):
    d = OUT / setname; d.mkdir(parents=True, exist_ok=True)
    (d / f'{seq}.prompt.ids').write_text(','.join(map(str, prompt)) + '\n')
    if answer is not None:
        (d / f'{seq}.answer.ids').write_text(','.join(map(str, answer)) + '\n')
    meta = {'prompt_tokens': len(prompt), 'n_answer': len(answer) if answer else n_answer}
    (d / f'{seq}.json').write_text(json.dumps(meta) + '\n')
    print(setname, seq, meta)

# short chat: upstream's 10 prompts, 128 answer tokens
ref_ids = [list(map(int, l.split(','))) for l in open(BUILD / 'tools/qwen35/parity_prompts.ids') if l.strip()]
for i, q in enumerate(json.load(open(SRC / 'short.json'))):
    ids = tokenize(chat(q, False))
    assert ids == ref_ids[i], (i, ids, ref_ids[i])   # the retokenized prompt must equal upstream's file
    write('short', f'p{i:02d}', ids, n_answer=128)
# code (thinking off, 400) and reasoning (thinking on, 324)
write('code', 'lru', tokenize(chat((SRC / 'code.txt').read_text().strip(), False)), n_answer=400)
write('code', 'puzzle', tokenize(chat((SRC / 'reasoning.txt').read_text().strip(), True)), n_answer=324)
# long prompts from the repository's docs, 256-token continuation each; real text = last 510 of the 16K document
doc = tokenize((SRC / 'longdoc.txt').read_text())
print('longdoc tokens', len(doc)); assert len(doc) >= 31744
for name, n in (('d4k', 4096), ('d16k', 16384), ('d31k', 31744)):
    write('long', name, doc[:n], n_answer=256)
write('realtext', 'r16k', doc[:16384 - 510], answer=doc[16384 - 510:16384])
