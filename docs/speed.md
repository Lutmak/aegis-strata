# Speed switches that are on by default

**Read this if you care about reproducible output, or if you want this engine faster on a card that is not an
RTX 3080 Ti.**

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

## The one thing to know

**They change the model's answers.** Not the quality of them — both settings produce a valid greedy decode of the
same model — but the exact token ids. The same prompt gives different wording with them on than with them off.

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
Model: Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS, the README's command line. Decode measured over a 256-token answer to
one fixed prompt at temperature 0, the server restarted for every run, and one warm-up answer discarded each time.

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

The parity test needs a CUDA build with the test suite on. `STRATA_ENABLE_CUDA` is off by default, and the binary
lands wherever you point the output directory, so both flags have to be there:

```
cmake -S . -B build-iq4test -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON -DSTRATA_NATIVE_EXPERTS=ON \
      -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_BUILD_TESTS=ON \
      -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$PWD/build-iq4test/bin
cmake --build build-iq4test -j --target prefill_iq4_w4a8_test
build-iq4test/bin/prefill_iq4_w4a8_test --tokens 4096            # parity, the shipped tile width
build-iq4test/bin/prefill_iq4_w4a8_test --cfg 3 --tokens 4096    # one of the other three
build-iq4test/bin/prefill_iq4_w4a8_test --bench --tokens 4096    # the table above, several times
compute-sanitizer --tool memcheck build-iq4test/bin/prefill_iq4_w4a8_test --tokens 97
python3 tools/bench_switches.py prefill --arms on,with --with STRATA_IQ4_W4A8=2
```

`make build` uses `75;86;89` and `-DSTRATA_BUILD_TESTS=OFF`; the arch is pinned to 86 above only to keep the build
short. The test exits 77, a skip, on a card `mmq::w4a8_arch_ok()` does not accept.

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
