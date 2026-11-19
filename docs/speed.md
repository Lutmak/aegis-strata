# Speed settings: what is on by default here, and what is not

**Read this if you care about reproducible output, if you want this engine faster on a card that is not an
RTX 3080 Ti, or if you want to know how much of the model your answers are coming from.**

There are two different trades in this file and it matters which is which.

The **seven sm_86 switches** below are on by default. They change the order floating-point work happens in, so
they move the exact token ids while computing the same model. That is the trade the next few sections are about.

The **route tail skip** is the other one, and it is *off* by default here. It drops some of the experts a token
was routed through, so it computes less of the model. It is worth more speed than all seven switches put
together — ×1.14 to ×1.34 of decode on this card — and it changes 11% to 16% of the model's own argmaxes.
Upstream ships it on for every CUDA card; this fork does not, and
[what the route tail skip is, and what it costs here](#what-the-route-tail-skip-is-and-what-it-costs-here) is
why, with the numbers. If you only read one section, read that one.

## The seven sm_86 switches

On a card of compute capability **8.6** (the RTX 30 series), this engine turns seven environment switches on by
itself at startup. They are worth about **+9.7% decode speed** and 2 MiB of GPU memory. The engine prints the list
to stderr every time it does it, so it is never silent:

```
strata: sm_86: 7 speed switches on by default, and the token ids differ from this engine without them
(docs/speed.md; STRATA_SM86_PERF=0 turns them off, a switch you set is kept): ADAPT_NOWAIT ADAPT_LAG
SIDE_MIXER ATTN_DAILY GR_UP75 QSA_ROWS B6_MMVQ_ROWS
```

**On any other card nothing is set and that line never prints.** The table is keyed on compute capability 8.6
alone, so on a 4090, a 3060 or an A100 the engine behaves exactly as it would without this feature, and silence
on stderr is the expected and correct outcome — not a sign that something failed.

## The one thing to know about the seven

**They change the model's answers.** Not the quality of them — both settings produce a valid greedy decode of the
same model — but the exact token ids.

That sentence is true of these seven and it is **not** true of the route tail skip, which is the reason the two
live in one file and are described apart. An earlier version of this document made the claim for everything the
engine turned on by itself, while the skip was on by default and dropping about one in seven of every answer's
routed expert lookups. It was wrong, and the shape of the mistake is worth keeping: a disclosure that covers
some of what is on by default reads as covering all of it.

Why: an expert swapped into the GPU's cache becomes visible to the layer that uses it a window or two later than it
otherwise would. One token eventually diverges, and the text after it follows that token. It is not an approximation
of any kernel, and nothing is computed at lower precision.

So if you need this engine's output to match a transcript you recorded with an older build, turn them off:

```
STRATA_SM86_PERF=0 build/bin/aegis --engine build/bin/strata ...
```

The switch is an **off** switch and it fails closed: any value that is not plainly "on" (`1`, `true`, `yes`, `on`)
turns the table off, including an empty one. So `STRATA_SM86_PERF=`, `=0`, `=false` and `=no` all disable it, and
only `STRATA_SM86_PERF=1` re-enables it explicitly. If you set one of the seven switches yourself, your value is
kept and the startup line names one fewer switch.

This is a weaker guarantee than the one the AMD gfx1151 table makes next door in
`include/strata/core/arch_defaults.hpp`, which admits only switches proven byte-identical and keeps every
bit-changing switch opt-in. The sm_86 table does not meet that bar and does not claim to. It is a deliberate trade
of exact reproducibility for ~10%, made with the measurements below.

## What was measured

Reference machine: RTX 3080 Ti (12 GB), Ryzen 9 7900X, 62 GB RAM, Arch Linux, driver 615.78.08, CUDA 13.4.92.
Model: Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS. Decode measured over a 256-token answer to one fixed prompt at
temperature 0, the server restarted for every run, and one warm-up answer discarded each time.

**Two caveats on the figures in this section, both dated.** They were taken when the route tail skip was on by
default, so both arms of every pair below were answering tokens from fewer than ten experts; the skip is off now.
And they were taken with the harness configured at `--prefill auto --max-context 32768`, where it now runs the
README's line (`--prefill 4096 --max-context 131072 --vram-reserve-mib 1152`) — the prompt path's borrow differs
by 1.5 GiB between those, and the expert cache is sized against the reserve at load. The deltas are paired, so
the *relative* figures below are the best evidence there is for which of the seven switches earn their place, and
the absolute tok/s are not comparable to anything else in this file. Re-measuring the drop-one table at the
shipped line, with the skip off, is a job nobody has done.

The engine's own defaults against `STRATA_SM86_PERF=0`, A/B interleaved in one sitting:

| | decode tok/s | peak VRAM |
|---|---|---|
| switches off | 89.55, 89.64 → **89.60** | 11,608 MiB |
| switches on (the default) | 98.09, 98.40 → **98.24** | 11,610 MiB |

**+9.65%**, both pairs in favour, the two ranges nowhere near overlapping, and the spread inside one arm under
0.4%. Peak use is 11,610 MiB of 12,288, so there is **678 MiB spare**.

What each switch is worth, as the cost in tok/s of removing it from the full table (drop-one):

| switch | removing it costs | upstream's number (A4000, 3060) |
|---|---|---|
| `STRATA_ADAPT_NOWAIT=1` + `STRATA_ADAPT_LAG=2` | **−3.66** | −6.3, −0.8 |
| `STRATA_SIDE_MIXER=2` | −2.16 | −1.2, −2.3 |
| `STRATA_ATTN_DAILY=1` | −2.06 | −1.2, −1.3 |
| `STRATA_GR_UP75=1` | −1.07 | −0.9, −1.8 |
| `STRATA_QSA_ROWS=1` | −0.42 | −0.5, −1.2 |
| `STRATA_B6_MMVQ_ROWS=1` | −0.36 | 0.0, −1.7 |
| `STRATA_CPU_IQ_ALL_EXACT1=1` | **+0.02 — nothing** | −3.3, −0.6 |

Reproduce any row of that table with `--drop`, which is the only comparison that measures it — the full table
minus one switch, against the full table:

```
python3 tools/bench_switches.py decode --drop STRATA_B6_MMVQ_ROWS --pairs 4
```

The `B6_MMVQ_ROWS` row was re-measured that way and came back at **+0.38 tok/s to add it**, which is the −0.36
above within the noise of two pairs.

### A note on comparing numbers across sittings

Inside one interleaved sitting the spread within an arm is 0.3–0.4%, which is what makes the paired deltas above
trustworthy. **Between sittings the same arm has drifted by 1.6%** — more than the gain of several individual
switches. So compare arms that were interleaved together, and do not read an absolute tok/s figure from one
session against another's.

## Where this list differs from upstream's, and why that matters to you

The engine this fork came from turns **nine** switches on for sm_86, measured on an RTX 3060 and an RTX A4000. Two
of those nine are not here:

- **`STRATA_CPU_IQ_ALL_EXACT1`** was worth 3.3 points on their A4000 and **+0.02 — nothing — on this card.** It is
  not shipped as a default. This is the whole reason the table was re-measured rather than copied.
- **`STRATA_GU_SWIGLU`** needs a kernel this fork does not have.

Upstream also reports that their nine leave the token ids **bitwise identical**. That is **not true on this card**:
the output changes, as described above. Their claim may well hold on the two cards they measured.

The lesson for your hardware: **a switch's value is a property of the card, not of the architecture.** Two Ampere
cards disagreed by 3.3 points on one switch. If you are on anything that is not an RTX 3080 Ti, the table shipped
here is a starting guess.

## What the route tail skip is, and what it costs here

This one is not in the table above, it is not an environment variable, and it is **off unless you ask for it**:

```
build/bin/aegis --engine build/bin/strata ... -- ... --tail-skip 7
```

The engine prints what it decided, every time, whichever way it went — on the `--serve` path and on a one-shot
run, before the prompt is read, and whether it is on, off, off because you asked for it off, or refused:

```
strata serve: route tail skip: OFF - every routed expert is copied or computed, so each token is answered by
all ten of the experts its router chose; --tail-skip 7 drops the three weakest of the ten when the GPU cache
misses them, which is faster and is not the same model

strata serve: route tail skip: OFF - --tail-skip 0 was asked for, so every routed expert is copied or computed
and each token is answered by all ten of the experts its router chose

strata serve: route tail skip: ON at rank 7 - when the GPU cache misses an expert that every token of a verify
window routes at rank 7 or lower (counting from 0, so the 3 weakest of the ten), it is neither copied nor
computed: its contribution is zero, the other experts keep the weight the router gave them, and that layer's
output is attenuated. Tokens whose missed experts all rank higher are answered in full
```

The ON line says what the dispatch will **do**, not what every token got, because only a *missed* expert is ever
a candidate: on a card that holds every expert, nothing is skipped however this is set.

`GET /props` reports it too, as `tail_skip`, so you can ask a running server rather than trusting a command line.

### What it does

This model routes each token through **ten** of the 512 experts in each of its 48 layers — 480 expert lookups
per token. A 12 GB card holds 2,940 of the 24,576 (layer, expert) pairs, about 12%, and that 12% catches roughly
three quarters of the lookups because some experts are far more popular than others. The rest are misses, and a
miss is normally either copied over PCIe or computed on the CPU.

`--tail-skip N` is a third answer. A missed expert that **every token of a verify window** routes at rank N or
lower — the smallest of the ten router weights — is neither copied nor computed. Its contribution is zero, and
**there is no renormalisation**: the other experts keep the weight the router gave them, so the layer's output is
attenuated rather than redistributed. At rank 7 that is ranks 7, 8 and 9, so the token is answered by seven to
nine experts instead of ten.

How often that happens on this card, from the engine's own counter over one 512-token answer: **42,134 of
308,113 routed expert lookups dropped — 13.7% of all of them, and 58% of the ones the GPU cache missed.**

### What it buys: ×1.14 to ×1.34 of decode speed

Reference machine and protocol as above, at the README's command line, four interleaved pairs per row, the server
restarted for every run and one warm-up answer discarded:

| prompt | complete model | `--tail-skip 7` | ratio | pairs in favour |
|---|---|---|---|---|
| a story | 74.79 tok/s | 85.17 | **×1.139** | 4 of 4 |
| prose (the sm_86 table's prompt) | 71.64 tok/s | 96.08 | **×1.341** | 4 of 4 |
| a code answer | 86.59 tok/s | 101.47 | **×1.172** | 4 of 4 |

Peak VRAM was 11,156–11,158 MiB in **both** arms: the skip costs no memory and buys none.

```
export AEGIS_MODELS=/models
python3 tools/bench_switches.py --logdir /some/dir decode --arms on,with \
    --with-arg "--tail-skip 7" --expect "route tail skip: ON at rank 7" --pairs 4 --prompt code
```

`--logdir` is an option of the tool, not of `decode`, so it goes before the subcommand. `--expect` is required
with `--with-arg` and is not decoration: an engine argument that never arrived produces an arm that is a second
copy of the other one, and two arms agreeing reads as "the setting is worth nothing" rather than "the setting
never ran".

**Three prompts and not one, on purpose.** Within a sitting every arm here repeated its answer exactly. Across
sittings the complete model settled on a *different* continuation for the prose prompt, and the two differed in
how draftable they were — 151 of 225 drafts accepted against 133 of 254 — which moved that arm's rate by 13%.
So ×1.341 is the ratio for one continuation of one prompt, and the two rows whose continuations were stable
across sittings are the ×1.139 and the ×1.172. Upstream measured the same setting on an RTX 3060 with this same
quantisation and reports ×1.104 for a story and ×1.197 for a code answer, which is close enough to these two to
be the same result on a different card.

One number in that table is a reporting artefact and not a finding: the engine's `decode expert cache hit rate`
reads **higher** with the skip on (72.6–76.7% against 64.3–69.1%), because a skipped expert is not counted as a
miss. The cache holds exactly the same experts either way.

### What it costs: 11% to 16% of the model's own argmaxes

`tools/bench_quality.py` is the instrument, and it exists because there was not one here before: every claim
about what this setting costs used to be somebody else's card. It makes the engine read a few thousand tokens of
fixed text through the decode windows — the only path the skip is on — and write the teacher-forced
log-probability of every position, once with the skip and once without, so the two arms score the **identical**
token sequence through the same dispatch and can be paired position by position.

```
export AEGIS_MODELS=/models
python3 tools/bench_quality.py --tokens 2048 --rank 7     # and once with --floor
```

| | prose (2,440 positions) | code (2,394 positions) |
|---|---|---|
| top-1 agreement with the complete model | **89.26%** | **83.75%** |
| positions whose argmax moved | 262 of 2,440 | 389 of 2,394 |
| mean &#124;Δ log p&#124; on the text's own tokens | 0.310 nats | 0.418 nats |
| p99, max | 3.42, 6.84 | 4.36, 10.28 |
| teacher-forced perplexity ratio | ×0.845 | ×0.933 |
| routed entries dropped | 169,348 | 187,366 |
| **the same configuration run twice (the floor)** | **0.00%, 0.000 nats** | **0.00%, 0.000 nats** |

**The floor is exactly zero**, which is what makes the rest of the column readable. Two separate engine processes,
started one after the other, produced bit-identical log-probabilities at all 4,834 positions. Prompt reading is
reproducible on this engine even though decode is not, which is the whole reason the measurement is taken on this
path.

Three things to hold on to about that table:

- **The perplexity ratio is below 1, and that is not the skip being good.** Dropping experts made this particular
  text *more* likely under the model, reproducibly, by 8–17%. Whatever the mechanism, it is a 0.31–0.42 nat
  per-position perturbation whose sign on two in-distribution texts happened to be favourable; the mean absolute
  movement is the honest headline and the ratio is a curiosity. Do not read it as a quality improvement.
- **It fires harder here than in decode, for two reasons, and only one of them used to be written down.** Every
  window on this path is a full six tokens of known text with no MTP drafts, and the gate is "every token of this
  window routes it low", so it triggers more per position than a short, mostly-drafted decode window does. The
  second reason is `--adapt-every 0`, which the harness passes and production does not: it freezes the expert
  cache, so residency never improves over the run and the misses — and therefore the skips — stay at their worst.
  Both push the same way, so these figures are an **upper bound on the per-position perturbation a window sees**.
  They are *not* a bound on a generated answer: with `--spec 4 --spec-min-p 0.5` one flipped verify-window argmax
  rejects a draft and the continuation diverges from there, and a mean over teacher-forced positions says nothing
  about that compounding. An earlier version of this bullet claimed an upper bound "on what a generated answer
  sees", which is more than the method supports. Upstream's decode-path measurement on an RTX 3060 with this
  quantisation reports **97.7%** top-1 agreement and a mean KL of 0.0058 against a run-to-run floor of 0.0006 —
  their card, their path, and quoted here as theirs.
- **The text these figures were taken on is identified, now, by hash.** The scored text is read from a file in
  this repository, so it moves when that file is edited — and the prose arm was first scored on the opening of
  `README.md`, which this same change rewrote, leaving a published figure its own printed command could no
  longer reproduce. The prose source is `docs/security.md` instead, whose opening this change does not touch,
  and every run now records a `text_sha256` over the exact bytes it scored, printed and written into the JSON.
  The two figures above predate that change and are quoted as the run that produced them; re-running the command
  will print a hash, and a figure with a hash beside it is one a reader can check.
- **A distribution is not a task score.** Upstream ran 82 HumanEval problems and 30 reasoning tasks with the
  skip on and off and could not separate the settings at all. That is the most relevant evidence anyone has about
  whether the change is *noticeable*, and it says: not obviously. Nobody has run a task suite on this card.

### Why it is off here when upstream has it on

Upstream turned it on for every CUDA card in engine 0.1.42 and calls it an owner decision. Their numbers for this
exact quantisation on a 12 GB card are not alarming, and the task check above found nothing, so this is not a
claim that they were careless.

It is off here for a reason of kind rather than size. Everything else this engine switches on by itself reorders
arithmetic; this computes less of the model. The AMD gfx1151 table next door in
`include/strata/core/arch_defaults.hpp` already holds the line that bit-changing switches stay opt-in, and the
sm_86 table above is a documented exception to it that buys 9.7%. A setting that answers a token from seven
experts out of ten is a long way past that line, and 13.7% of an answer's expert lookups is not a detail an
operator should have to read a stderr line to discover. So the engine's own default is the complete model, the
README's command line does not ask for the skip, and turning it on is one flag with this section's numbers next
to it.

Two environment variables used to configure this — `STRATA_ROUTE_TAIL_SKIP` and `STRATA_TAIL_SKIP_MAX_CACHED`.
Neither is read any more. If either is set the engine says so and ignores it, which fails towards the complete
model rather than away from it.

## Checking it on your own card

`tools/bench_switches.py` is the harness these numbers came from. It restarts the server for each run, reads the
engine's own timings out of the server log rather than timing anything over HTTP, interleaves the two arms in one
sitting, and hashes the generated text so you can see whether an arm changed the answer.

```
export AEGIS_MODELS=/models          # holds IQ3_XXS/, pack-iq3-xxs/ and mtp/rt, as in the README
python3 tools/bench_switches.py decode --pairs 4
python3 tools/bench_switches.py prefill
```

Its "on" arm runs the engine with an **empty environment** and lets the engine apply its own table, so what it
measures is the default path a user actually gets; it then fails loudly if the engine did not print the startup
line, because "the defaults are on" and "the defaults silently did nothing" are otherwise indistinguishable. It
refuses to run if another process is using the GPU, or if it cannot read `nvidia-smi` to find out.

To test a set that is not shipped here, edit `SM86_SWITCHES` at the top of that file: it is what `--drop` leaves a
switch out of. If you find a better set for your card, please say so; the table is keyed on compute capability in
`src/core/nv_defaults.cpp` and a second entry costs nothing.

Two things to hold fixed, or you will measure noise instead of switches:

- **Interleave the arms and run at least four pairs.** Do not run all of one arm and then all of the other.
- **Discard a warm-up answer per server start.** The first decode after a model load is not representative.

### A caution about reproducibility

Do not read a single matching hash as proof that a switch is exact. Over the runs behind this document, the empty
environment gave one answer in **17 of 18** runs and a different one once, unexplained — so this engine is not
perfectly reproducible even with every switch off. And dropping `ADAPT_NOWAIT` while keeping `ADAPT_LAG=2` gave
**two different answers in three runs**, which is why those two ship as a pair and should not be split without
re-measuring reproducibility rather than just speed.

A `finish` other than `length` in a sample means the request did not run to the token count asked for, so its rate
and its hash describe something else; the harness prints `finish` on every line for that reason.

## An opt-in prompt kernel that is worth nothing on this pack: `STRATA_IQ4_W4A8`

`STRATA_IQ4_W4A8=1` (or `=2` for the down products alone) runs the routed experts' IQ4_XS / IQ4_NL prompt products
on `src/prefill/moe_iq4_w4a8.cu` instead of llama.cpp's MMQ. It is **off unless you set it**, and it is recorded here
because the measurement is the useful part: the kernel is genuinely faster per product and **changes the prompt rate
on this pack by nothing at all.**

`STRATA_W4A8_CFG` picks its tile width — `0` (the default, 128 tokens per CUDA block), `1` (104), `2` (112), `3`
(96). The speed figures below are all at `0`, the shipped value. All four are bit-exact on this card
(`prefill_iq4_w4a8_test --cfg 0..3`); only `0` has been measured for speed.

### It is bit-exact, which is unusual for a speed switch here

It does not belong with the seven switches above: it changes no output. The kernel declines any launch whose sums MMQ
would add in a different order (`mmq::launch_splits_k`), and what it does run is MMQ's tiling-mode sum bit for bit.
Checked on this card at six prompt lengths against three routing distributions — 0 differing words in every case, 97
to 32,768 tokens, up to 1,258,291,200 words compared. Two things make that check worth believing:

- Remove the stream-k guard and it fails at once: 16,886 differing words at 97 tokens. The exactness rests on that
  guard, not on luck.
- Under `compute-sanitizer --tool memcheck` it reports **0 errors**. That matters because it did not always: the test
  used to size its own MMQ row table exactly, and the sanitizer found 281 invalid reads past it — the same defect
  `mmq_ids_margin.hpp` exists to prevent, in the test that was meant to prove the kernel right. A sanitized run of
  `--tokens 97` takes about four minutes and is the only check that looks at the *allocations* rather than the
  arithmetic.

The verdict line names the configuration and the product counts, because "0 differ" on its own is also what a kernel
that declined every product would print.

### Per product it is faster; the gate/up products are slower when routing is flat

Kernel time against MMQ, `--bench`, on the RTX 3080 Ti. **Five repeats at 4,096 tokens and three at 8,191**, because
one run of this is not a measurement — the figures below are the range across repeats, and the first case of each
process carries the warm-up:

| | down (IQ4_NL) | gate/up (IQ4_XS) |
|---|---|---|
| uniform routing | +6.2% … +15.7% | **−3.0% … −20.6%** |
| log-normal 1 | +25.1% … +35.9% | +0.9% … +14.2% |
| log-normal 2 | +41.6% … +56.8% | +11.4% … +20.8% |

**The down products were faster in all 24 of 24 measurements.** The gate/up products were **slower in all 8 of the 8
uniform-routing measurements** and faster under skewed routing, which is what `=2` is for: it takes the down products
and leaves gate/up to MMQ. Upstream's "+9..+70% on sm_86" is the down column of a different machine, not the whole
switch.

The uniform column is the noisy one because the two times are close there (MMQ 2.93 ms against the kernel's 3.02–3.37
ms at 4,096), so a small absolute difference is a large relative one. Do not read a single `--bench` line as a result.

### End to end it is nothing

Measured against the engine's own defaults. The arms run one after the other, not interleaved — `bench_switches.py
prefill` takes one server per arm — so only the direction of a difference this small is worth anything, and the
direction is not even consistent:

| prompt tokens | default | with `=2` | with `=1` | default, that run |
|---|---|---|---|---|
| 2,103 | 1,304.7 tok/s | 1,309.1 | 1,309.1 | 1,310.3 |
| 8,613 | 2,423.5 tok/s | 2,428.7 | 2,426.1 | 2,420.8 |

Two runs of the same command: in one the kernel is 0.3% ahead, in the other the default is 0.1% ahead at the short
length. That is noise, and the honest reading is that the switch is worth nothing here.

**A rate alone cannot tell you the kernel ran.** A kernel that declined every product produces exactly the default
number, which is inside these differences. The thing that distinguishes them is the engine's own exit line, which
counts products: `ran 0 IQ4_XS and 3211 IQ4_NL products` at `=2`, and `3213` at `=1`. `bench_switches.py` now fails
the run if the engine never mentions a switch the `with` arm set, for exactly this reason.

### Why nothing, on this pack

The kernel takes **routed** expert products. `Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS` has **18** of those in IQ4_NL
(`blk.N.ffn_down_exps.weight`). Its other 58 IQ4_XS / IQ4_NL tensors are shared-expert (`ffn_down_shexp`,
`ffn_gate_shexp`, `ffn_up_shexp`) and dense projections (`attn_qkv`, `attn_gate`, `ssm_out`), which this kernel does
not claim — so `=1` and `=2` measure the same thing here, and at `=1` the engine reports `ran 0 IQ4_XS` products.

So: **leave it off on this pack.** It is worth setting on a model whose routed gate/up and down experts are IQ4_XS or
IQ4_NL — the UD-IQ4_XS sets upstream measured it on — and `=2` is the safer of the two values on this card.

### Reproducing any of the above

Both correctness checks are one target:

```
make test-gpu
```

It builds the parity test, runs it at all four tile widths, and then runs it under `compute-sanitizer` — the only
check here that looks at the allocations rather than the arithmetic, and the one that caught the test's own row
table reading out of bounds. It needs a GPU and `compute-sanitizer` on `PATH`, which is why it is not in
`make test`: that target runs with CUDA off. It exits 77, a skip, on a card the kernel has not been measured on.

**It has only been measured on sm_86.** `mmq::w4a8_arch_ok()` accepts that and nothing else, so on any other card
the kernel declines every product and MMQ runs — including cards upstream accepts (Ampere 8.0–8.8 and Blackwell
12.0, on their own parity runs). The claim here is bit-for-bit reproduction of MMQ, and nobody has checked it on an
A100, an Orin or a Blackwell card; widening the gate means running `make test-gpu` on one first. The cost of being
narrow is that such an operator gets MMQ where upstream measured the kernel to be faster.

For the speed figures, which `make test-gpu` deliberately does not run (they are not pass/fail):

```
build-gpu/bin/prefill_iq4_w4a8_test --bench --tokens 4096    # the table above, several times
python3 tools/bench_switches.py prefill --arms on,with --with STRATA_IQ4_W4A8=2
```

One caveat the test prints on every run here: `STRATA_MMQ_TAB` is off by default, so the pointer-table layout of the
kernel is not exercised by any of this.

## Prefill is not affected by any of this

Reading the prompt is a separate path and none of these switches touched it measurably. For reference, on the
machine above, with no switches:

| prompt | prefill tok/s |
|---|---|
| 497 tokens | 699–838 (noisy at this length) |
| 2,015 tokens | 1,281 |
| 8,257 tokens | 2,547 |

The rate rises with the prompt because a fixed per-request cost is spread over more tokens, so a figure quoted at
one prompt length says little about another. Measured with `reused=0` on every run — the harness sends a different
prompt of the same length for each repeat, so no prompt-cache reuse is flattering these numbers.

Those two figures were taken at `--prefill auto`. The command line the README ships sets `--prefill 4096`, which
costs about 17% of prompt speed and buys the memory headroom the next section is about.

### One switch does touch the prompt path, and it is on by default

The heading above is about the seven and the route tail skip. It is not a claim that the prompt path has no
default-on setting that changes its bits, because it has one, and this document is where the README sends
you for exactly that kind of disclosure. This file was titled "seven on by default, and one that is not"
until that eighth was written down, which is the hazard of counting in a title: the count was true of the
sm_86 table and the route tail skip, and false of the document as a whole the moment anything else was
disclosed.

`STRATA_PREFILL_CPU_SHARE` (`src/prefill/prefill.cpp#bool g_share_default`) is on by default on a CUDA build,
but only when every one of five things holds: one GPU, no `--batch` slots, no layer split, not every expert
already resident (there would be nothing for the CPU to take), and a host-memory check with 3 GiB of headroom
(`include/strata/prefill/share_rules.hpp#default_share_has_work`). An earlier draft of this paragraph said only
"one GPU and no batch slots", which is narrower than the code, and anchored on the switch's own name — which
appears ten times in that file and so is not the single greppable needle an anchor is supposed to be. When a prompt chunk is below 1,024 tokens, it hands the decode CPU pool — idle
while a prompt is being read — the non-resident experts that few of the chunk's tokens route to, instead of
streaming those experts over PCIe. Upstream measured the chunk 20% to 35% faster at 512 and 1,000 tokens on an
RTX 3060, a Tesla P100 and an RTX 5070, 8 of 8 pairs on each, for a mean KL against the share off of about 0.004.

Two things to be clear about. It is **not** one of the seven: it is upstream's own default for every CUDA build
that qualifies, not something this fork turns on, and the engine prints a line at load saying it is active and
how to stop it. And its cost has **not** been measured on this machine — 0.004 is upstream's figure on other
cards, and it sits at the same order as the run-to-run floor of the method that produced it, which is why it is
recorded here as a disclosure rather than as a measurement of this pack. `tools/bench_quality.py` is the
instrument that can settle it on this card, and `STRATA_PREFILL_CPU_SHARE=0` is the arm to compare against.

## A 128K context on a 12 GB card, and why the limit is not where you would guess

The README ships `--max-context 131072` — 128K — and the card is not what stopped it there. Two facts about this
model's geometry, both from the GGUF's own metadata:

- `context_length` is **262,144**. The trained context is not the limit either.
- `full_attention_interval` is **4**, over `block_count` **48**. Only **12 layers** keep a per-token K/V. The other
  36 are linear and carry a fixed-size state that does not grow with the conversation at all.

So the K/V cost is a **quarter** of what a dense 48-layer model of this size would want — 12 layers of 48 — and
at `--kv int8` each of those 12 layers spends 1,056 bytes per cell (`head_count_kv` 2, `key_length` 256; the
arithmetic is `include/strata/kernels/kv_q8.hpp#kv_q8_bytes_per_cell`).

The figures below are the engine's own, read off the `elastic K/V: … of … cells in VRAM (… of … GiB)` line it
prints at load, at `--kv int8 --kv-grow` on the shipped command line:

| context | K/V, the engine's figure | 12 × 1,056 × context | expert slots at load |
|---|---|---|---|
| 32,768 | 0.46 GiB | 0.39 GiB | — |
| 98,304 | 1.27 GiB | 1.16 GiB | 2,973 |
| 131,072 (**shipped**) | **1.68 GiB** | 1.55 GiB | 2,940 |
| 262,144 | ~3.3 GiB (not attempted here) | 3.09 GiB | — |

**The two columns do not match, and the per-cell figure is not the whole story.** An earlier version of this
section put the table immediately after the 1,056 bytes and joined them with "so", which invited a reader to
multiply and find the document wrong by 9% — the one thing a document that asks to be checked must not do. The
engine's figure is 9% above the bare cell arithmetic at 96K and 128K and 19% above it at 32K, because what the
context allocation holds is more than those twelve layers' cells: the MTP drafter keeps a K/V of its own, and
the pools are allocated in pages rather than per cell, which is why the gap shrinks as the context grows. The
marginal cost between the last two rows is 13,435 bytes a token against the 12,672 the cells alone would need.
Take the first column as measured and the second only as a floor.

Either way the conclusion holds and is the point of the section: quadrupling the context costs about a
gigabyte. `--kv-grow` takes it from the expert cache as the conversation actually fills rather than at load, so a
longer context is paid for in expert residency and not in peak VRAM. Both halves of that were measured on this
tree, one after the other, on an idle card:

| `--max-context` | slots at load | slots still holding experts, context full | K/V full | peak VRAM | free VRAM at worst |
|---|---|---|---|---|---|
| 98,304 | 2,973 | 2,353 | 1.27 GiB | 11,134 MiB | 1,154 MiB |
| 131,072 (**shipped**) | 2,940 | 2,074 | 1.68 GiB | 11,202 MiB | 1,086 MiB |

So 128K over 96K costs **33 slots at load, 279 more once the context is full, and 68 MiB of the floor** — and the
card is not what limits the context here. Earlier editions of this document put the 96K floor at 741 MiB and the
96K peak at 11,547 MiB; those were taken on an older tree and the pair above supersedes them.

### The constraint is the prompt path's borrow, not the K/V

The prompt path borrows expert-cache slots for its buffers and refills them afterwards (`--no-prefill-borrow`
reserves them for the session instead). The borrow scales with the chunk, and at `--prefill auto` on this pack
that is 8,192 tokens and **4.00 GiB** — which leaves a growing K/V nowhere to go but the free pool. At 96K that
cost speed: filling the context drove free VRAM to **371 MiB**, inside the band where the driver can page GPU
memory to system RAM and decode collapses to a few tokens a second. The engine warns below 512 MiB and it is
right to.

At 128K it does not cost speed, it costs the request. Measured on this tree with everything else as the README
ships it, a real 123,866-token prompt at `--prefill auto` took the expert cache to its floor, drove free VRAM to
**326 MiB**, and the engine then died mid-request:

```
strata serve: the prompt path borrows 2449 CUDA0 cache slots (4.00 GiB)
strata generate: elastic K/V: 16384 of 131072 cells in VRAM (0.25 of 1.68 GiB); the expert cache (2940 slots)
                 gives the K/V room below slot 491 as the context grows
strata: K/V grown to 131072 cells (1.68 GiB); ... - and new VRAM, the cache being at its floor
ggml-cuda (strata mmq): out of memory: cudaMalloc(&p, size)
```

The server turned that into `503 the engine exited during the request`. The same prompt at `--prefill 4096` is
answered with 1,086 MiB still free, which is why the chunk size is in the shipped line and not a tuning note.
Note the mechanism in the third line: at `auto` the cache can only lend the K/V room below slot 491, against
slot 1404 at 4096, so the K/V runs out of lendable cache and goes to the free pool instead.

`--vram-reserve-mib` does not fix this, which is the counter-intuitive part and worth recording:

| `--vram-reserve-mib` | `--prefill` | context | expert slots at load | free VRAM at worst |
|---|---|---|---|---|
| 832 | auto | 98,304 | 3,165 | 385 MiB |
| 1,400 | auto | 98,304 | 2,821 | 371 MiB |
| 1,152 | auto | 131,072 | 2,940 | 326 MiB, then **out of memory** |
| 1,152 | **4096** | 98,304 | 2,973 | 1,154 MiB |
| 1,152 | **4096** | **131,072** | 2,940 | **1,086 MiB** |

Raising the reserve by 568 MiB moved the floor by −14 MiB. The reserve sizes the expert cache *at load*; the borrow
happens per request afterwards, so a bigger reserve just hands the borrow more to take. Halving the chunk halves
the borrow — 1,536 slots, 2.49 GiB — and is what actually moves the floor. The first two rows are 96K
measurements from an older tree, kept because the mechanism they establish still holds; the last three were
measured together on this one.

### What the shipped command line measures

Same machine as above, with the engine's own default switches on, through the HTTP API:

| | |
|---|---|
| prompt, 2,047 tokens | 1,180 tok/s |
| prompt, 8,581 tokens | 2,093 tok/s |
| prompt, 34,157 tokens | 2,140 tok/s |
| prompt, 95,713 tokens | 2,196 tok/s |
| prompt, 123,866 tokens | 2,156 tok/s |
| decode, 256-token answers | 100.2, 105.8, 107.6 → **median 105.8 tok/s** (route tail skip ON — see below) |
| decode, 128K context full | **57.2 tok/s**, skip off: a 95-token answer after a 123,866-token prompt |
| free VRAM at worst | 1,086 MiB of 12,288 |

**The 256-token decode row was measured with the route tail skip on, which is no longer the default.** With the
skip off — the complete model, which is what this command line now runs — the same machine measures 74.8 tok/s on
a story, 71.6 on prose and 86.6 on a code answer, restarting the server for every run; adding `--tail-skip 7`
puts them at 85.2, 96.1 and 101.5. Those six figures and their commands are in
[what the route tail skip is, and what it costs here](#what-the-route-tail-skip-is-and-what-it-costs-here). The
context-full row **is** skip-off and is the real cost of a long conversation: 57.2 tok/s against 72-87 on a short
one, because a full 128K K/V has taken 866 slots from the expert cache and only 2,074 of 2,940 still hold
experts. Prompt rates are unaffected either way, because the skip is not on the prompt path this command line
uses.

Every prompt rate is a `reused=0` run on unique text. The decode figures are **not** comparable to the
`89.60`/`98.24` pair at the top of this file: those restart the engine for every run and reply to one fixed prompt,
which is the protocol an A/B needs, and these do not restart. They are quoted only to show the shipped line is not
slower in ordinary use. Retrieval was checked rather than assumed — a fact planted mid-document was read back
correctly from prompts of 37,593, 75,092, 93,758, 97,649 and 101,499 tokens, and at the shipped 131,072 from a
123,866-token prompt with the planted fact 69% of the way in.

One earlier measurement in this file disagreed with the ones above and has been removed rather than reconciled:
`--max-context 102400` was recorded as having a 327 MiB floor even at `--prefill 4096`, which cannot sit beside a
measured 1,086 MiB at the larger 131,072. It was taken on an older tree, 102,400 has not been re-measured here,
and nothing now ships against it. If you want that number, measure it again.
