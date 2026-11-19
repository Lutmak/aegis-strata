"""tools/bench_quality.py - what the route tail skip costs in model quality, measured on this card.

WHY THIS EXISTS. tools/bench_switches.py measures speed. Until this file there was no instrument here for the
other half, so every claim about what the skip costs had to be borrowed from somebody else's hardware -- and a
project whose pitch is that its claims are checkable should not be quoting a stranger's card for the one setting
that changes how much of the model runs.

WHAT IT MEASURES, AND WHY THIS WAY. The skip drops a missed expert that every token of a verify window routes at
rank N or lower. So the comparison has to put the same text through the same verify windows twice, once with the
skip and once without, and look at the distributions position by position. Three ways to do that were considered:

  - `--dump-logits`: rejected. Its rows come from the per-token loop, which a native pack never runs -- the
    speculative loop takes over at the first generated token. It would have produced nothing, or prompt-only rows.

  - `--spec-follow` (the engine's own teacher-forced top-1 counter over generated text): a real instrument, and
    the first thing tried. Rejected as the primary one because the floor swamps it. Two runs of the SAME
    configuration disagreed on 11 and 13 of 512 tokens on this card -- decode here is not reproducible run to run
    -- and the skip itself moved 23 of 512. An effect the same order as the noise needs many more samples than a
    few 512-token runs to separate, and an argmax throws away the size of each disagreement.

  - `--short-read N` with `STRATA_LOGPOS` (what this does): the engine reads up to N prompt tokens through the
    decode windows instead of the batched prefill path, and STRATA_LOGPOS makes it write the teacher-forced
    log-probability of every position it reads. Both arms then score the IDENTICAL token sequence through the
    same dispatch the skip lives in, so the comparison is paired per position, every position counts rather than
    only the ones where an argmax flipped, and a 2,000-token prompt yields 2,000 scored positions from one
    request instead of 512 from a whole generation.

WHAT IT DOES NOT MEASURE. The windows here carry no MTP drafts and are full, where a decode window is up to six
tokens and mostly drafted, so how OFTEN the skip fires differs from decode even though the arithmetic is the
same. And this is a distribution check, not a task score: upstream ran 82 code problems and 30 reasoning tasks
and could not separate the settings at all, which is worth knowing before reading much into a small number here.

    export AEGIS_MODELS=/home/lutmak/models/aegis
    python3 tools/bench_quality.py --tokens 2048 --rank 7

It needs a built server and engine, the model, and an idle GPU. These runs are OUTSIDE the engine's usual
containment in one respect: the engine child is granted write access to the directory STRATA_LOGPOS points at,
because that is where it puts the measurement. `make test-gpu` is the same kind of exception. Nothing here runs
as part of `make test`; its own unit tests do.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_switches import (  # noqa: E402  (the path insert above has to come first)
    BASE,
    MODELS,
    PROJECT,
    SHARD1,
    SHARD2,
    gpu_is_idle,
    start,
    Arm,
)

# The text to score, and where it comes from. Prose and code, because upstream found the cost uneven across
# them -- code and reasoning prompts moved least, chat and non-English most -- and a coding agent is what this
# server is for, so a figure averaged over chat alone would be the wrong number for this machine's traffic.
#
# The text is taken from files in this repository rather than embedded here. Two reasons: a few thousand tokens
# of filler in a tool is a few thousand tokens nobody will read, and repeated or synthetic text is the wrong
# material for this measurement -- a model is very confident on a repeating passage, which compresses exactly the
# range of log-probabilities the comparison is looking at. Within one run the text cannot move: both arms read
# the same bytes, and compare() refuses to pair them if they did not.
#
# Across runs it can, and that is a real hazard for a published figure, so two things guard it. The prose source
# is docs/security.md, not README.md: the README was the first choice and was the wrong one, because it is among
# the most-edited files here and the scored slice is a file's OPENING -- the very part a change to this project
# is most likely to touch. It duly moved, in the same change that added this tool, which left a published figure
# that its own printed command could no longer reproduce. docs/security.md is long enough that 2,048 tokens stay
# inside one stable stretch of it, and -- the second thing to get wrong here -- it is a file the project actually
# publishes. The first replacement chosen was one of the internal documents, which exists in the working tree but
# is excluded from the release, so this tool would have worked here and failed for anyone reading the shipped
# source. check_docs.py refused it on exactly that ground.
# Second, every run records `text_sha256` (below) over the exact bytes it scored, so a figure can be checked
# against the text that produced it instead of trusted.
SOURCES: dict[str, tuple[str, str]] = {
    "prose": ("docs/security.md",
              "Read the following project documentation, then explain in careful prose what its engine does "
              "with an expert that does not fit in GPU memory.\n\n"),
    "code": ("src/core/expert_source.cpp",
             "Read the following C++ from an inference engine, then explain in careful prose how it decides "
             "what to do with a routed expert that is not resident on the GPU.\n\n"),
}


def prompt_text(name: str, approx_tokens: int) -> str:
    """The instruction plus enough of the source file to come to roughly `approx_tokens` tokens.

    Four characters per token is a rough average over English and C++ and does not need to be right: what the
    measurement needs is a few thousand positions of real text, and the harness reports how many it actually
    scored rather than assuming the estimate held.
    """
    rel, instruction = SOURCES[name]
    path = PROJECT / rel
    body = path.read_text(errors="replace")
    return instruction + body[: max(0, approx_tokens * 4)]


@dataclass
class Row:
    """One scored position: the engine's teacher-forced view of the token the text actually has there.

    The log-probabilities are finite by construction. That is enforced here rather than at the parse, because
    `compare` sorts the deltas and `sorted` with a NaN in it is not an order: the order statistics then report
    the comforting end of a non-order, and one NaN delta printed `max |delta log p| 0.00000` -- "nothing moved"
    -- on data whose movement is undefined. An infinity did the same the other way, making the perplexity ratio
    0.0, which reads as "the skip made the text more likely". A comment saying the parse checks it would not
    have held: `compare` is the published numbers' entry point and a test or a later caller can reach it directly.
    """
    pos: int
    target: int
    target_logprob: float
    top: int
    top_logprob: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.target_logprob) and math.isfinite(self.top_logprob)):
            raise ValueError(f"position {self.pos} has a non-finite log-probability "
                             f"(target {self.target_logprob!r}, top {self.top_logprob!r}), so it cannot be scored")


def parse_logpos(text: str) -> list[Row]:
    """Read STRATA_LOGPOS output.

    Columns, as src/core/verify.cpp#window_logprobs writes them:
    pos, target, target logprob, top, top logprob, hit, extra logprob, target logprob without extra.
    Only the first five are used here; the last two exist to take a chat model's end-of-turn mass out of a
    comparison against a different engine, which is not what this does.
    """
    rows: list[Row] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        f = line.split("\t")
        # Eight, not "at least five". STRATA_LOGPOS_TOPK appends `id:logprob` pairs AFTER the eight, so a longer
        # line is fine, but a shorter one is a format that is not the one documented above -- and accepting five
        # would read whatever now sits in column 5 as `top_logprob` and produce a plausible wrong number.
        if len(f) < 8:
            raise ValueError(f"STRATA_LOGPOS line has {len(f)} fields, expected at least 8 "
                             f"(pos, target, target logprob, top, top logprob, hit, extra, without): {line!r}")
        try:
            rows.append(Row(int(f[0]), int(f[1]), float(f[2]), int(f[3]), float(f[4])))
        except ValueError as e:
            raise ValueError(f"STRATA_LOGPOS line cannot be scored ({e}): {line!r}") from e
    return rows


@dataclass
class Comparison:
    n: int
    top1_agreement: float       # fraction of positions where the two arms' argmax is the same token
    nll_ref: float              # mean negative log-probability of the text under the reference arm
    nll_arm: float
    perplexity_ratio: float     # exp(nll_arm - nll_ref): >1 means the text got less likely
    mean_abs_delta: float       # mean |logprob difference| on the text's own tokens
    p99_abs_delta: float
    max_abs_delta: float


def compare(ref: list[Row], arm: list[Row]) -> Comparison:
    """Pair the two arms position by position.

    It refuses rather than aligning: two runs that scored different positions, or the same positions over
    different text, are not a comparison, and silently intersecting them would turn a configuration mistake into
    a plausible-looking number. The positions and the target ids both have to match, because the first catches a
    different prompt length and the second catches a different prompt.
    """
    if not ref or not arm:
        raise ValueError(f"nothing to compare: {len(ref)} reference rows, {len(arm)} arm rows")
    if len(ref) != len(arm):
        raise ValueError(f"the arms scored different numbers of positions ({len(ref)} and {len(arm)}): they did "
                         f"not read the same text, so there is nothing to pair")
    for a, b in zip(ref, arm):
        if a.pos != b.pos or a.target != b.target:
            raise ValueError(f"the arms disagree about position {a.pos} vs {b.pos} or its token "
                             f"{a.target} vs {b.target}: they did not read the same text")
    # A repeated position is not a second sample of it. The engine can replay a window after a state restore, and
    # a duplicated position would be weighted twice in every mean below -- while also defeating the coverage check
    # in `run_arm`, which only catches too FEW rows and would read the padding as the prompt having been scored.
    if len({r.pos for r in ref}) != len(ref):
        raise ValueError(f"the reference arm scored {len(ref)} rows over only {len({r.pos for r in ref})} distinct "
                         f"positions: a replayed window would be counted twice in every mean")
    n = len(ref)
    agree = sum(1 for a, b in zip(ref, arm) if a.top == b.top)
    nll_ref = -sum(r.target_logprob for r in ref) / n
    nll_arm = -sum(r.target_logprob for r in arm) / n
    # Every log-probability was checked finite at parse time (parse_logpos), so `sorted` here is a real order.
    # It has to be: `sorted` with a NaN in it is not an order at all, and the order statistics below then come
    # out of the reassuring end -- a NaN delta printed `max 0.00000`, which reads as "this position did not move".
    deltas = sorted(abs(a.target_logprob - b.target_logprob) for a, b in zip(ref, arm))
    return Comparison(
        n=n,
        top1_agreement=agree / n,
        nll_ref=nll_ref,
        nll_arm=nll_arm,
        perplexity_ratio=math.exp(nll_arm - nll_ref),
        mean_abs_delta=sum(deltas) / n,
        p99_abs_delta=deltas[min(n - 1, int(0.99 * n))],
        max_abs_delta=deltas[-1],
    )


def skipped_entries(log_text: str) -> int:
    """The engine's own count of routed entries it dropped, 0 when it never printed the line.

    This is the check that makes the whole measurement mean anything. An arm that was supposed to skip and did
    not is indistinguishable, from the numbers alone, from an arm that skipped and cost nothing -- and the two
    say opposite things. The engine prints the cumulative line only when it skipped something, so its absence is
    a real zero.
    """
    # Two wordings of the same fact exist in this engine -- the serve line says "(N entries)" and the one-shot
    # summary says "(N routed entries)" -- so match both. Matching only one made the reference arm's "it must
    # have skipped nothing" check pass whenever the other wording was the one printed, which is a guard that
    # cannot fail rather than a guard that passed.
    hits = re.findall(r"route tail skip:?\s+(?:rank \d+, )?(\d+) missed experts skipped \((\d+) (?:routed )?entries\)",
                      log_text)
    return int(hits[-1][1]) if hits else 0


def prompt_tokens_read(log_text: str) -> tuple[int, int]:
    """The engine's own account of the last request: (prompt tokens sent, of those reused from the cache).

    This is the harness's only independent measure of what the engine read, and the thing every coverage claim
    rests on, so its absence raises rather than returning a zero. Guarding the comparison with "if we found a
    DONE line" instead meant the one check standing between this tool and its historical failure -- scoring 223
    of 2,441 positions and publishing the result -- would vanish without a word the moment that line's format
    drifted. A guard that disappears when its input does is not a guard.

    Only the fresh tokens go through the decode windows and get scored, so the reused count is returned
    separately rather than folded in: comparing against the total would read a reused prefix as missing
    coverage, and tolerating half the total (what this did) let a run score 51% of the prompt in silence.
    """
    done = re.findall(r"DONE produced=\d+ prompt=(\d+)(?:\s+reused=(\d+))?", log_text)
    if not done:
        raise ValueError("the server log has no 'DONE produced=... prompt=N' line, so there is no independent "
                         "count of what the engine read and this arm's coverage cannot be checked; a number "
                         "from it would be a sample of unknown size")
    return int(done[-1][0]), int(done[-1][1]) if done[-1][1] else 0


def run_arm(rank: int, prompt: str, logdir: Path, name: str) -> tuple[list[Row], str, int]:
    """One arm: a fresh server at this rank, one request, and the rows it scored."""
    # The scores go in a subdirectory of their own, because that subdirectory is what the engine is granted
    # write access to below -- and the server log, which is this harness's evidence for what the engine did,
    # must not be inside it. A contained process that can rewrite the proof of its own behaviour is not
    # contained for the one purpose this harness exists to serve.
    scores = logdir / "scores"
    scores.mkdir(parents=True, exist_ok=True)
    logpos = (scores / f"{name}.logpos").resolve()
    logpos.unlink(missing_ok=True)
    arm = Arm(
        name=name,
        env={"STRATA_LOGPOS": str(logpos)},
        # --short-read makes the engine read the prompt through the decode windows, which is the only path the
        # skip is on; the batched prefill path never consults it. It is set to the whole context rather than to
        # the prompt's expected length, because the engine falls back to the batched path for ANY prompt longer
        # than the bound and says nothing about it: a 2,048-token estimate against a 2,441-token prompt scored
        # zero positions and the only symptom was an empty output file. --adapt-every 0 holds the expert cache
        # still, so the two arms miss the same experts instead of the adaptive tier re-ranking residency
        # differently under each and mixing a second effect into the difference.
        engine_args=["--tail-skip", str(rank), "--short-read", "98304", "--adapt-every", "0"],
        expect=f"route tail skip: {'OFF' if rank == 0 else f'ON at rank {rank}'}",
    )
    # The engine is contained, so it cannot write where it likes: without this the fopen fails, logpos stays
    # nullptr and the engine scores nothing at all -- silently, because it has nowhere to say so.
    srv_extra = ["--sandbox-allow-write", str(logpos.parent)]
    log = logdir / f"{name}.log"
    srv = start(arm, log, server_extra=srv_extra)
    try:
        body = json.dumps({"prompt": prompt, "max_tokens": 1, "temperature": 0.0, "stream": False}).encode()
        req = urllib.request.Request(f"{BASE}/v1/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1200.0) as r:
            r.read()
    finally:
        srv.stop()
    log_text = log.read_text(errors="replace")
    if arm.expect not in log_text:
        raise RuntimeError(f"[{name}] the engine never printed {arm.expect!r}, so this arm did not run at rank "
                           f"{rank}. See {log}")
    if not logpos.exists() or logpos.stat().st_size == 0:
        raise RuntimeError(f"[{name}] the engine wrote nothing to {logpos}. Two causes, and the file is empty for "
                           f"both: the containment (the engine child may only write where --sandbox-allow-write "
                           f"permits, and a refused fopen leaves it scoring nothing with no way to say so), or "
                           f"the prompt never went through the decode windows, which is the only path that "
                           f"scores anything -- the engine reads anything longer than --short-read through the "
                           f"batched path instead. See {log}")
    rows = parse_logpos(logpos.read_text())
    # The prompt the engine actually read, from its own line, against the positions it scored. A prompt that went
    # half through windows and half batched would otherwise produce a quiet, smaller sample.
    read_n, reused_n = prompt_tokens_read(log_text)
    expected = read_n - reused_n
    if len(rows) < expected - 2:
        raise RuntimeError(f"[{name}] the engine read {read_n} prompt tokens ({reused_n} reused, so {expected} "
                           f"fresh) and scored only {len(rows)} positions: most of the prompt did not go through "
                           f"the decode windows and is not in this measurement. See {log}")
    return rows, log_text, skipped_entries(log_text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tokens", type=int, default=2048,
                    help="how many prompt tokens to score through the decode windows (default 2048)")
    ap.add_argument("--rank", type=int, default=7, help="the --tail-skip rank to measure against 0 (default 7)")
    ap.add_argument("--floor", action="store_true",
                    help="run the COMPLETE model twice instead, so the pair measures this instrument's own "
                         "floor. Every figure below has to be read against it: the engine is not reproducible "
                         "run to run, so a difference smaller than the floor is not a measurement of anything")
    ap.add_argument("--prompts", default=",".join(SOURCES),
                    help=f"which texts to score, comma separated (default all: {', '.join(SOURCES)})")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("--out", default="bench-quality.json")
    args = ap.parse_args()

    # Both ends, and before any server starts. An out-of-range rank used to pass here and then be refused by the
    # engine itself, which turned a typo into a four-minute wait on the readiness timeout before saying so.
    if not args.floor and not 1 <= args.rank <= 9:
        print("--rank must be 1-9: this compares a rank against 0, and the engine accepts a single digit. To "
              "compare 0 against 0 -- the engine's own run-to-run noise, which every other figure has to be "
              "read against -- pass --floor.", file=sys.stderr)
        return 2
    # The sample size is the measurement. --tokens 0 scored the instruction alone, about 35 positions, and
    # published a top-1 agreement and a perplexity ratio over it without remarking on the size.
    if args.tokens < 256:
        print(f"--tokens {args.tokens} is too few to measure anything: the figures this prints are an agreement "
              f"rate and a perplexity ratio, and over a few dozen positions they are noise. Use 256 or more.",
              file=sys.stderr)
        return 2
    wanted = [p for p in args.prompts.split(",") if p]
    unknown = [p for p in wanted if p not in SOURCES]
    if unknown:
        print(f"--prompts: no such text {', '.join(unknown)}; this file has {', '.join(SOURCES)}",
              file=sys.stderr)
        return 2
    missing = [rel for rel, _ in (SOURCES[p] for p in wanted) if not (PROJECT / rel).exists()]
    if missing:
        print(f"the text to score comes from this repository and these files are not here: {', '.join(missing)}",
              file=sys.stderr)
        return 2
    logdir = Path(args.logdir) if args.logdir else Path(tempfile.mkdtemp(prefix="aegis-quality-"))
    logdir.mkdir(parents=True, exist_ok=True)
    print(f"server logs and scores -> {logdir}", file=sys.stderr)
    if not gpu_is_idle():
        return 2
    for path in (SHARD1, SHARD2, MODELS / "pack-iq3-xxs", MODELS / "mtp/rt"):
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 2

    out: dict[str, dict] = {}
    for key in wanted:
        print(f"\n=== {key} ===", flush=True)
        text = prompt_text(key, args.tokens)
        # The hash of the exact bytes scored, printed and recorded. The text comes from a file in this
        # repository, so it moves when that file is edited, and a figure whose text cannot be identified cannot
        # be reproduced by the command printed next to it -- which is how a published number quietly becomes
        # unverifiable. With this, anyone can re-derive the text and check.
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        print(f"  text: {SOURCES[key][0]}, {len(text)} characters, sha256 {text_sha[:16]}", flush=True)
        arm_rank = 0 if args.floor else args.rank
        ref_rows, _, ref_skipped = run_arm(0, text, logdir, f"{key}-ref")
        arm_rows, _, arm_skipped = run_arm(arm_rank, text, logdir, f"{key}-skip")
        # The reference arm must have dropped nothing, and the second arm must have dropped something unless this
        # is the floor run, where both arms are the complete model on purpose. Either check failing means the arms
        # were not what they claim and the numbers below would describe something else.
        if ref_skipped != 0:
            raise RuntimeError(f"[{key}] the reference arm (--tail-skip 0) skipped {ref_skipped} routed entries; "
                               f"it is supposed to compute every one")
        if args.floor and arm_skipped != 0:
            raise RuntimeError(f"[{key}] the floor run's second arm skipped {arm_skipped} routed entries; both "
                               f"arms are supposed to be the complete model")
        if not args.floor and arm_skipped == 0:
            raise RuntimeError(f"[{key}] the skip arm (--tail-skip {args.rank}) skipped nothing, so this pair "
                               f"compares the complete model against itself. On a card that holds every expert "
                               f"there is nothing to skip and this measurement cannot be made")
        c = compare(ref_rows, arm_rows)
        total = c.n
        print(f"  positions scored          {c.n}")
        print(f"  routed entries dropped    {arm_skipped} (reference arm: {ref_skipped})")
        print(f"  top-1 agreement           {100 * c.top1_agreement:.2f}%  "
              f"({total - round(c.top1_agreement * total)} of {total} positions moved)")
        print(f"  mean -log p of the text   {c.nll_ref:.5f} complete, {c.nll_arm:.5f} "
              f"{'complete again' if args.floor else 'skipped'}")
        print(f"  perplexity ratio          x{c.perplexity_ratio:.4f}")
        print(f"  |delta log p|             mean {c.mean_abs_delta:.5f}, p99 {c.p99_abs_delta:.5f}, "
              f"max {c.max_abs_delta:.5f}")
        out[key] = {"rank": arm_rank, "floor": args.floor, "source": SOURCES[key][0],
                    "text_sha256": text_sha, "text_chars": len(text),
                    "skipped_entries": arm_skipped, **vars(c)}

    Path(args.out).resolve().write_text(json.dumps(out, indent=2))
    print(f"\nraw -> {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
