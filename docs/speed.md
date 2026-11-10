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
