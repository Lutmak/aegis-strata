#!/usr/bin/env python3
"""Step-12 inputs for a model whose tokenizer is not Qwen's (CORRECTNESS-PLAN.md 3.2; the DeepSeek V4 Flash and GLM 5.3
Flash runs of 2026-10-19). Same texts as make-inputs.py (inputs-src/), tokenized through the fork front end that serves
the model: /apply-template renders the model's own chat template, /tokenize gives the ids (special tokens parsed).
make-inputs-model.py <root> <server-url> <deepseek|glm>
Writes <root>/inputs/<set>/<seq>.prompt.ids, .json and realtext's .answer.ids. Raw text (long, realtext) is prefixed
with the model's sequence start: DeepSeek <|begin_of_sentence|>, GLM [gMASK]<sop>, as the chat template does."""
import json, pathlib, sys, urllib.request
root, url, fam = pathlib.Path(sys.argv[1]), sys.argv[2].rstrip('/'), sys.argv[3]
SRC = root / 'inputs-src'; OUT = root / 'inputs'

def post(path, body):
    req = urllib.request.Request(url + path, json.dumps(body).encode(), {'content-type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=600))

def tokenize(text):
    return post('/tokenize', {'content': text, 'add_special': False})['tokens']

def chat(q, think):
    """The model's own template, thinking on or off. DeepSeek: the template's `thinking` switch (off renders
    `<|Assistant|></think>`). GLM: the template always opens <think>; off is an empty block, as Qwen's."""
    if fam == 'deepseek':
        return post('/apply-template', {'messages': [{'role': 'user', 'content': q}], 'chat_template_kwargs': {'thinking': think}})['prompt']
    p = post('/apply-template', {'messages': [{'role': 'user', 'content': q}]})['prompt']
    assert p.endswith('<think>'), p[-40:]
    return p if think else p + '</think>'

PREFIX = {'deepseek': '<｜begin▁of▁sentence｜>', 'glm': '[gMASK]<sop>'}[fam]

def write(setname, seq, prompt, answer=None, n_answer=None):
    d = OUT / setname; d.mkdir(parents=True, exist_ok=True)
    (d / f'{seq}.prompt.ids').write_text(','.join(map(str, prompt)) + '\n')
    if answer is not None:
        (d / f'{seq}.answer.ids').write_text(','.join(map(str, answer)) + '\n')
    meta = {'prompt_tokens': len(prompt), 'n_answer': len(answer) if answer else n_answer}
    (d / f'{seq}.json').write_text(json.dumps(meta) + '\n')
    print(setname, seq, meta, flush=True)

for i, q in enumerate(json.load(open(SRC / 'short.json'))):
    write('short', f'p{i:02d}', tokenize(chat(q, False)), n_answer=128)
write('code', 'lru', tokenize(chat((SRC / 'code.txt').read_text().strip(), False)), n_answer=400)
write('code', 'puzzle', tokenize(chat((SRC / 'reasoning.txt').read_text().strip(), True)), n_answer=324)
doc = tokenize(PREFIX + (SRC / 'longdoc.txt').read_text())
print('longdoc tokens', len(doc)); assert len(doc) >= 31744, len(doc)
for name, n in (('d4k', 4096), ('d16k', 16384), ('d31k', 31744)):
    write('long', name, doc[:n], n_answer=256)
write('realtext', 'r16k', doc[:16384 - 510], answer=doc[16384 - 510:16384])
(root / 'inputs' / 'README.txt').write_text(f'{fam}: tokenized through {url} (fork front end /apply-template + /tokenize), texts from inputs-src/\n')
