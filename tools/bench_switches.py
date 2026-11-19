"""Interleaved A/B benchmark of the aegis server, and the tool the sm_86 default table came from.

Decode speed on this engine is reproducible to about 1% when the prompt, the token count and the warm-up are
held fixed, but it is NOT reproducible across differently-shaped runs -- so a single run of each arm proves
nothing. This runs the arms alternately in one sitting, alternating which one leads, and compares paired
differences, which cancels any slow drift in the machine.

The numbers come from the server's own DONE log line, which carries the engine's own
`produced / prompt_n / prompt_ms / decode_ms`. Nothing is timed from the client side, so HTTP and SSE overhead
stay out of the result. It also hashes the generated text, so you can see whether an arm changes the answer.

Use it to check the shipped defaults on YOUR card, which is the point of docs/speed.md:

    export AEGIS_MODELS=/models            # holds IQ3_XXS/, pack-iq3-xxs/ and mtp/rt, as in the README
    python3 tools/bench_switches.py decode --pairs 4      # the whole table on vs off
    python3 tools/bench_switches.py prefill               # prefill rate at several prompt lengths

`decode` compares the table in SM86_SWITCHES below against an empty environment. To test one switch, or a set
this file does not ship, edit that dict: it is read as the "on" arm and nothing else depends on it.

Needs a built server and engine, the model, and an idle GPU; it refuses to start if another process is using
the card, because a second tenant makes every number meaningless.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
MODELS = Path(os.environ.get("AEGIS_MODELS", "/models"))
SHARD1 = MODELS / "IQ3_XXS" / "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
SHARD2 = MODELS / "IQ3_XXS" / "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf"
PORT = int(os.environ.get("AEGIS_BENCH_PORT", "18080"))
BASE = f"http://127.0.0.1:{PORT}"

# The table this tree ships as the sm_86 defaults (src/core/nv_defaults.cpp).
#
# This list is NOT how the "on" arm is produced -- that arm runs the engine with an empty environment and lets
# the engine apply its own table, which is the only way to measure what a user actually gets. Setting these here
# is what hid a real bug: the engine skips any switch the environment already names ("the user's setting wins"),
# so exporting all seven meant apply_nv_defaults() set nothing, printed no startup line, and the benchmark
# measured a configuration the default path cannot reach. One of the seven -- STRATA_B6_MMVQ_ROWS -- is read by
# an initializer that runs before main, so the engine's own setenv could never reach it, and the published
# +9.5% was obtained with a switch the shipped engine never applied.
#
# The list survives for --drop, which needs to name the keys in order to leave one out.
# STRATA_CPU_IQ_ALL_EXACT1 and STRATA_GU_SWIGLU are upstream's and deliberately absent; docs/speed.md says why.
SM86_SWITCHES: dict[str, str] = {
    "STRATA_ADAPT_NOWAIT": "1",
    "STRATA_ADAPT_LAG": "2",
    "STRATA_SIDE_MIXER": "2",
    "STRATA_ATTN_DAILY": "1",
    "STRATA_GR_UP75": "1",
    "STRATA_QSA_ROWS": "1",
    "STRATA_B6_MMVQ_ROWS": "1",
}


def server_argv(engine_extra: list[str] | None = None, server_extra: list[str] | None = None) -> list[str]:
    """The command line the README ships, plus any engine arguments an arm adds.

    It used to read `--prefill auto --max-context 32768` and no `--vram-reserve-mib`, which is not what anybody
    runs: the README ships `--prefill 4096 --max-context 98304 --vram-reserve-mib 1152`, and those three are not
    cosmetic -- the chunk size costs about 17% of prompt speed and is what keeps the VRAM floor off the paging
    band, and the expert cache is sized against the reserve at load. So every figure this tool produced described
    a configuration no operator uses, and a decode figure measured at a 4 GiB prompt borrow is not the decode
    figure of a line that borrows 2.49 GiB. Measuring what ships is the whole point of the harness.
    """
    return [
        str(PROJECT / "build/bin/aegis"),
        "--engine", str(PROJECT / "build/bin/strata"),
        "-m", str(SHARD1),
        "--host", "127.0.0.1", "--port", str(PORT),
        "--alias", "qwen3.8-flash-next-iq3_xxs",
        "--sse-ping-interval", "10",
        *(server_extra or []),
        "--",
        "--pack", str(MODELS / "pack-iq3-xxs"),
        "--native", str(SHARD1),
        "--ple-gguf", str(SHARD2),
        "--expert-profile", str(PROJECT / "data/expert-profile.bin"),
        "--expert-cache", "auto", "--prefill", "4096",
        "--spec", "4", "--spec-min-p", "0.5",
        "--mtp", str(MODELS / "mtp/rt"),
        "--max-context", "98304", "--kv", "int8", "--ple-io", "direct", "--kv-grow",
        "--vram-reserve-mib", "1152", "--pool-workers", "15", "--gpu", "0",
        *(engine_extra or []),
    ]


DONE_RE = re.compile(
    r"DONE produced=(\d+) prompt=(\d+) prompt_ms=([\d.]+) decode_ms=([\d.]+) "
    r"finish=(\w+) drafts=(\d+)/(\d+) reused=(\d+)"
)


@dataclass
class Sample:
    produced: int
    prompt_n: int
    prompt_ms: float
    decode_ms: float
    finish: str
    drafts_accepted: int
    drafts_offered: int
    text_sha: str

    @property
    def decode_tps(self) -> float:
        return self.produced / (self.decode_ms / 1000.0)

    @property
    def prefill_tps(self) -> float:
        return self.prompt_n / (self.prompt_ms / 1000.0)


@dataclass
class Server:
    proc: subprocess.Popen
    log: Path
    _read_to: int = 0
    peak_vram_mib: int = 0

    def new_done_lines(self) -> list[Sample]:
        text = self.log.read_text(errors="replace")
        fresh = text[self._read_to:]
        self._read_to = len(text)
        return [
            Sample(int(m[1]), int(m[2]), float(m[3]), float(m[4]), m[5], int(m[6]), int(m[7]), "")
            for m in DONE_RE.finditer(fresh)
        ]

    def sample_vram(self) -> None:
        """Peak VRAM, or -1 if nvidia-smi could not be read.

        -1 and not 0: a failed read used to leave the field at 0, and the report printed `peak VRAM 0 MiB` as a
        finished measurement -- and docs/speed.md's VRAM column is this field.
        """
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False,
        )
        out = r.stdout.strip()
        if r.returncode != 0 or not out.isdigit():
            self.peak_vram_mib = -1
            return
        if self.peak_vram_mib >= 0:
            self.peak_vram_mib = max(self.peak_vram_mib, int(out))

    def stop(self) -> None:
        # By pid, never by name: a name kill in this project has killed the wrong process before.
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def gpu_is_idle() -> bool:
    """True only when nvidia-smi was READ and reported no compute apps.

    It used to inspect stdout alone with check=False, so an empty stdout returned True -- and stdout is empty
    whenever nvidia-smi is missing, is denied, or is a container stub that writes to stderr. "I could not look"
    then read as "the card is free", which is the opposite of what the docstring promises and makes every
    number this tool prints meaningless without saying so.
    """
    r = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        print(f"could not read nvidia-smi (exit {r.returncode}), so I cannot tell whether the GPU is free; "
              f"refusing to measure.\n{r.stderr.strip()}", file=sys.stderr)
        return False
    out = r.stdout.strip()
    if out:
        print(f"GPU is not idle, compute apps present:\n{out}", file=sys.stderr)
        return False
    return True



def parse_with(pairs: list[str] | None) -> dict[str, str]:
    """`--with KEY=VALUE` (repeatable): the variables of the extra "with" arm, checked here rather than at use.

    This exists because the only comparisons the tool could make were inside the sm_86 table. A switch that is not
    in the table -- an opt-in kernel like STRATA_IQ4_W4A8, or an upstream default being evaluated here for the
    first time -- had no arm, so measuring one meant editing this file.
    """
    out: dict[str, str] = {}
    for item in pairs or []:
        key, sep, value = item.partition("=")
        # `STRATA_` alone is not a switch name. It mattered more than it looks: the "did this arm take effect"
        # proof falls back to matching the name without its prefix, and an empty suffix made that fallback
        # match any log at all, so `--with STRATA_=1` passed as a switch the engine had honoured.
        if not sep or not key.startswith("STRATA_") or key == "STRATA_":
            raise SystemExit(f"--with: expected STRATA_KEY=VALUE with a name after the prefix, got {item!r}")
        # An empty value is how a shell turns one of this engine's switches OFF (nv_defaults.cpp documents
        # STRATA_SM86_PERF= as exactly that), so `--with STRATA_X=` would measure the default path twice and report
        # the agreement as a measurement of STRATA_X.
        #
        # "0" is the OTHER way these switches are turned off, and refusing the empty value while accepting it was
        # half a guard. Worse than half: the "with" arm is built as `on`'s table | with_vars, so with_vars wins,
        # and `--drop X --with STRATA_Y=0` produced an arm that is the default table MINUS a switch while the
        # summary labelled it "the default path plus one more setting". The two arms then differ by the opposite
        # of what the run claims to measure.
        if value == "" or value == "0":
            raise SystemExit(f"--with: {key}={value!r} turns a switch off, and this arm is 'the default path plus "
                             f"one setting': it would be the default path MINUS one, labelled as plus one. To "
                             f"measure what a switch is worth, drop it from the default arm with --drop {key}.")
        if key in out:
            raise SystemExit(f"--with: {key} given twice")
        out[key] = value
    return out


@dataclass
class Arm:
    """One side of a comparison: the environment, the engine arguments, and how to prove it took effect.

    Engine arguments are here because not every setting is an environment variable any more. The route tail skip
    is `--tail-skip N` on the engine's own command line, and an arm that could only set variables had no way to
    drive it -- which is how this tool ended up unable to measure the one setting in the engine that changes how
    much of the model runs.
    """
    name: str
    env: dict[str, str] = field(default_factory=dict)
    engine_args: list[str] = field(default_factory=list)
    expect: str | None = None   # a string the engine's log must contain, or this arm measured something else


def parse_with_args(items: list[str] | None) -> list[str]:
    """`--with-arg "--tail-skip 7"` (repeatable): extra arguments for the ENGINE in the "with" arm.

    One quoted string per option rather than one token per option, for a dull but unavoidable reason: argparse
    reads a value that begins with `-` as another option, so `--with-arg --tail-skip` fails before this function
    is reached. A value with a space in it is passed through, which is why the flag and its value travel together.
    Split with shlex, so a quoted path with a space in it survives.
    """
    out: list[str] = []
    for item in items or []:
        parts = shlex.split(item)
        if not parts:
            raise SystemExit(f"--with-arg: expected an engine argument, got {item!r}")
        if not parts[0].startswith("-"):
            raise SystemExit(f"--with-arg: {parts[0]!r} is not an option; write the flag and its value together, "
                             f'as --with-arg "--tail-skip 7"')
        out += parts
    return out


def arms_for(drop: str | None, with_vars: dict[str, str] | None = None,
             with_args: list[str] | None = None, expect: str | None = None) -> dict[str, Arm]:
    """The arms to compare.

    "on" is deliberately EMPTY of environment: the engine applies its own sm_86 table, so this measures the
    default path a user gets rather than a hand-built environment that happens to name the same variables.
    assert_default_applied() then checks the engine said so.

    With --drop KEY the comparison changes to "the full table minus KEY" against "the full table", which is what
    the per-switch column in docs/speed.md actually reports. Without this the only possible off arm was
    STRATA_SM86_PERF=0 ("everything off"), so a reader following the document could not reproduce that column at
    all -- they could only measure a subset against nothing.
    """
    if drop is None:
        arms = {"off": Arm("off", env={"STRATA_SM86_PERF": "0"}), "on": Arm("on")}
    elif drop not in SM86_SWITCHES:
        raise SystemExit(f"--drop: {drop} is not in the table; choose from {', '.join(sorted(SM86_SWITCHES))}")
    else:
        minus = {k: v for k, v in SM86_SWITCHES.items() if k != drop}
        arms = {"off": Arm("off", env=minus), "on": Arm("on", env=dict(SM86_SWITCHES))}
    # "with" is the "on" arm plus whatever --with and --with-arg named: the default path a user gets, and one more
    # setting on top of it, so the pair isolates that setting and nothing else.  Named only when one of them was
    # given, so `--arms with` without them fails as an unknown arm instead of silently measuring "on" twice.
    if with_vars or with_args:
        arms["with"] = Arm("with", env=arms["on"].env | (with_vars or {}),
                           engine_args=list(with_args or []), expect=expect)
    return arms


def assert_default_applied(srv: "Server", arm: str, drop: str | None) -> None:
    """On the default arm, fail loudly unless the engine printed the line that proves it applied its table.

    Without this the tool cannot tell "the defaults are on" from "the defaults silently did nothing", which is
    the exact failure it is supposed to detect.
    """
    # "with" is the default path too (it adds a switch the table does not name), so it gets the same check: a
    # "with" arm that silently lost the table would otherwise be compared against an "on" arm that kept it.
    if arm not in ("on", "with") or drop is not None:
        return
    text = srv.log.read_text(errors="replace")
    if "speed switches on by default" not in text:
        raise RuntimeError(
            "the engine did not print its sm_86 startup line, so the default table was NOT applied: this arm "
            f"would measure the slow path and report it as the default. See {srv.log}"
        )


def assert_with_took_effect(srv: "Server", arm: Arm) -> None:
    """On a "with" arm, fail unless the engine said something about each switch the arm added.

    The arm's whole claim is "the default path, plus this switch". Nothing checked that the switch did anything, and
    the ways it can quietly do nothing are ordinary: a typo in the name, a value the engine parses as off, a kernel
    that declines every product on this card, a build without the device code. All of them produce a "with" arm that
    measures the default path a second time, two columns that agree, and the conclusion "the switch is worth nothing"
    -- which is indistinguishable from "the switch did nothing". That is lesson 24 one level up: a harness for a
    switch must drive the switch and then prove it was driven.

    The proof used is the engine's own habit of naming a switch it has honoured (docs/speed.md rests on the same
    habit for the sm_86 table). A switch that announces nothing cannot be measured by this tool without being taught
    here, and failing is the right answer then: a silent arm is not evidence.
    """
    if arm.name != "with":
        return
    text = srv.log.read_text(errors="replace")
    # `KEY=`, not `KEY`. Bare substring containment was the wrong test in three ways at once, and a gauntlet run
    # demonstrated all three: `STRATA_SM86_PER=1` (the real name with one letter dropped -- the exact typo this
    # docstring claims to catch) passed, because it is a prefix of a switch the engine does mention; so did
    # `STRATA_S=1`; and so did `STRATA_=1`, because `"" in text` is true of every log ever written. Each of them
    # sets a variable the engine never reads, which is an arm that measured the default path a second time and
    # then reported "the switch is worth nothing".
    # The second clause exists because the engine sometimes names a switch without its prefix (the sm_86 line
    # lists `ADAPT_NOWAIT ADAPT_LAG ...`). It has to require a non-empty suffix: for the key `STRATA_` the
    # suffix is "", so the test degenerated to `"=" in text`, which is true of every log that names anything.
    silent = [
        k for k in arm.env
        if f"{k}=" not in text
        and not (k.removeprefix("STRATA_") and f"{k.removeprefix('STRATA_')}=" in text)
    ]
    if silent:
        raise RuntimeError(
            f"the 'with' arm set {', '.join(silent)} and the engine never mentioned {'it' if len(silent) == 1 else 'them'}: "
            f"this arm measured the default path again, so comparing it against 'on' would show no difference whatever "
            f"the switch is worth. Check the spelling and the value, and that this card and build have the code behind "
            f"it. See {srv.log}"
        )
    # An engine argument announces itself in whatever words the engine chose, so the caller states the words with
    # --expect and this checks for them. It is required alongside --with-arg rather than optional, because the
    # failure it catches is the one that looks like a result: an arm whose setting never arrived measures the
    # other arm a second time, the two agree, and "no difference" reads as "the setting is worth nothing".
    if arm.engine_args and arm.expect and arm.expect not in text:
        raise RuntimeError(
            f"the 'with' arm passed {' '.join(arm.engine_args)} to the engine and its log never contains "
            f"{arm.expect!r}: this arm did not run the configuration it claims to, so comparing it against 'on' "
            f"would describe something else. See {srv.log}"
        )


def assert_expect_discriminates(expect: str, baseline_log: Path) -> None:
    """Fail unless `--expect` is absent from the arm that did NOT get the engine argument.

    `--expect TEXT` proves an argument arrived only if TEXT is something the engine says when it arrived and
    does not say otherwise. A string that appears in both arms' logs proves nothing, and the natural choice
    often is one: this engine announces the route tail skip in every outcome now, and three of the four lines --
    including the two refusals and the OFF default -- quote `--tail-skip 7` in their explanatory prose. So
    `--with-arg "--tail-skip 7" --expect "--tail-skip 7"`, which is what an operator types first because it
    echoes the flag, passed on an arm where the skip was off. Both arms then ran the complete model, agreed, and
    the summary read "the setting is worth nothing".

    Checked here rather than against a word list, because the property is general: whether the string separates
    the two arms is a question about this run's own logs, and this run has both.
    """
    if expect and expect in baseline_log.read_text(errors="replace"):
        raise RuntimeError(
            f"--expect {expect!r} also appears in the log of the arm that did NOT get the engine argument "
            f"({baseline_log}), so it does not prove the argument arrived and this pair cannot be trusted. Pick "
            f"a string the engine prints only when the setting is active -- for the route tail skip that is "
            f"\"route tail skip: ON at rank N\", not the flag's own name, which its OFF line also quotes."
        )


def start(arm: Arm, log: Path, ready_timeout: float = 240.0,
          server_extra: list[str] | None = None) -> Server:
    env = os.environ | arm.env
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("w")
    proc = subprocess.Popen(
        server_argv(arm.engine_args, server_extra), stdout=handle, stderr=subprocess.STDOUT,
        cwd=PROJECT, env=env, start_new_session=True,
    )
    srv = Server(proc=proc, log=log)
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with {proc.returncode}; see {log}")
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                if r.status == 200:
                    return srv
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1.0)
    srv.stop()
    raise RuntimeError(f"server not ready within {ready_timeout:.0f}s; see {log}")


def post(path: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def completion(srv: Server, prompt: str, max_tokens: int, timeout: float = 600.0) -> Sample:
    before = len(srv.log.read_text(errors="replace"))
    srv._read_to = before
    reply = post(
        "/v1/completions",
        {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.0, "stream": False},
        timeout,
    )
    srv.sample_vram()
    text = reply["choices"][0]["text"]
    # The DONE line is written as the task finishes; give the logger a moment to flush.
    for _ in range(50):
        samples = srv.new_done_lines()
        if samples:
            s = samples[-1]
            s.text_sha = hashlib.sha256(text.encode()).hexdigest()[:16]
            return s
        time.sleep(0.1)
    raise RuntimeError("no DONE line appeared in the server log for this request")


# Decode prompts, by the kind of work they ask for. One prompt is not enough for a decode figure on this engine
# and that is not a matter of noise: within a sitting every arm here repeats its answer exactly, but across
# sittings the same configuration settled on a DIFFERENT continuation, and the two differed in how draftable they
# were -- 151 of 225 drafts accepted against 133 of 254 -- which moved the arm's rate by 13%. A gap measured on
# one prompt is a gap for that continuation. Upstream reports its own tail-skip figures per prompt kind for the
# same reason (a story, a code answer, and an answer after a long document), so these three mirror theirs and the
# numbers can be read side by side.
DECODE_PROMPTS: dict[str, str] = {
    "story": (
        "Write a short story about a lighthouse keeper who discovers that the light has been answering "
        "someone. Keep it to a few hundred words and do not use lists."
    ),
    "prose": (
        "Explain, in careful detail and without using lists, how a modern operating system "
        "schedules threads onto CPU cores, and why that design is the way it is."
    ),
    "code": (
        "Write a C++ function that takes a sorted vector of integers and a target, and returns the range of "
        "indices whose values equal the target, as a pair of iterators. Explain the invariant your loop "
        "maintains, then say what it does when the target is absent and why that is the right answer."
    ),
}
DECODE_PROMPT = DECODE_PROMPTS["prose"]   # the one the published figures were taken at before `--prompt` existed


def filler(approx_tokens: int, salt: int = 0) -> str:
    """A prompt of roughly `approx_tokens` tokens, built from prose, not a repeated token.

    A repeated single token would let prefix caching or any dedup path short-circuit the
    read and flatter the prefill number.

    `salt` changes the prompt from the FIRST token on, so two calls with different salts share no prefix. The
    same prompt sent twice to one server is served from the prompt cache, which is the correct behaviour of the
    server and the wrong thing to time when the number being published is a prefill rate.
    """
    sentence = (
        "The quarterly report notes that throughput rose while latency held steady, "
        "which the authors attribute to a change in how work is batched before it is "
        "handed to the accelerator rather than to any change in the kernels themselves. "
    )
    # ~45 tokens per sentence; build long, then let the caller confirm with /tokenize.
    reps = max(1, approx_tokens // 46)
    return "".join(f"[{salt}.{i}] {sentence}" for i in range(reps))


def tokenize_len(prompt: str) -> int:
    return len(post("/tokenize", {"content": prompt}, 60.0)["tokens"])


def fmt(xs: list[float]) -> str:
    if not xs:
        return "-"
    mean = sum(xs) / len(xs)
    return ", ".join(f"{x:.1f}" for x in xs) + f"  -> mean {mean:.2f}"


def arm_facts(srv: Server) -> str:
    """What the engine said about the things that decide a decode rate, so a row carries its own context.

    Decode speed here is mostly a question of how often a routed expert is not in the GPU cache and what the
    engine does about it, and all three of those come out of the engine's own log. Without them a pair of numbers
    is uninterpretable six months later: the same two arms on the same card give different gaps at different
    cache hit rates, and the hit rate depends on the profile, the context and the reserve.
    """
    text = srv.log.read_text(errors="replace")
    bits = []
    m = re.search(r"expert cache (\d+) slots", text)
    if m:
        bits.append(f"slots={m[1]}")
    hits = re.findall(r"decode expert cache hit rate: ([\d.]+)%", text)
    if hits:
        bits.append(f"hit={hits[-1]}%")
    skips = re.findall(r"route tail skip: (\d+) missed experts skipped \((\d+) entries\)", text)
    bits.append(f"skipped={skips[-1][1]} entries" if skips else "skipped=0 entries")
    m = re.search(r"route tail skip: (OFF|ON at rank \d+)", text)
    if m:
        bits.append(f"tail_skip={m[1]}")
    return "  ".join(bits)


def cmd_decode(args: argparse.Namespace) -> int:
    logdir = Path(args.logdir)
    arms = arms_for(getattr(args, "drop", None), parse_with(getattr(args, "with_", None)),
                    parse_with_args(getattr(args, "with_arg", None)), getattr(args, "expect", None))
    # Exactly two arms, because what this subcommand does is a PAIRED comparison: it alternates which arm leads so
    # that a machine warming or cooling cannot favour one, and compares differences within a pair. Three arms have
    # no pairs, and averaging them separately would throw away the only thing that makes the small deltas here
    # trustworthy -- docs/speed.md records a 1.6% drift between sittings against a 0.3-0.4% spread inside one.
    wanted = [a for a in args.arms.split(",") if a]
    if len(wanted) != 2 or wanted[0] == wanted[1]:
        print(f"--arms: name exactly two different arms, got {args.arms!r}; this run has "
              f"{', '.join(sorted(arms))}", file=sys.stderr)
        return 2
    a_name, b_name = wanted
    results: dict[str, list[Sample]] = {a_name: [], b_name: []}
    order: list[str] = []
    for i in range(args.pairs):
        # Alternate which arm leads, so a warming or cooling trend cannot favour one.
        order += [a_name, b_name] if i % 2 == 0 else [b_name, a_name]

    # -1, not 0: a failed nvidia-smi read leaves Server.peak_vram_mib at -1 and says so in its docstring, and
    # max(0, -1) turned that into "peak VRAM 0 MiB" printed as a finished measurement -- in the summary line,
    # which is the one that gets copied into docs/speed.md's VRAM column.
    peak = {a_name: -1, b_name: -1}
    facts: dict[str, str] = {}
    arm_logs: dict[str, Path] = {}
    for n, name in enumerate(order, 1):
        arm = arms[name]
        print(f"[{n}/{len(order)}] arm={name} starting server ...", flush=True)
        srv = start(arm, logdir / f"{n:02d}-{name}.log")
        arm_logs[name] = srv.log
        try:
            assert_default_applied(srv, name, getattr(args, "drop", None))
            # One discarded warm-up request: the first decode after a model load is not representative.
            completion(srv, "Say the word ready.", 8)
            s = completion(srv, DECODE_PROMPTS[args.prompt], args.max_tokens)
            results[name].append(s)
            peak[name] = srv.peak_vram_mib if peak[name] < 0 else max(peak[name], srv.peak_vram_mib)
            # After the requests, not at start-up: the skip's counter and the cache hit rate only exist once
            # something has been decoded, and the tail-skip line is the engine's proof the arm ran what it claims.
            assert_with_took_effect(srv, arm)
            baseline = next((o for o in (a_name, b_name) if not arms[o].engine_args), None)
            if arm.engine_args and arm.expect and baseline and baseline in arm_logs:
                assert_expect_discriminates(arm.expect, arm_logs[baseline])
            facts[name] = arm_facts(srv)
            print(
                f"    decode {s.decode_tps:7.2f} tok/s   produced={s.produced} "
                f"finish={s.finish} drafts={s.drafts_accepted}/{s.drafts_offered} "
                f"vram={srv.peak_vram_mib} MiB sha={s.text_sha}",
                flush=True,
            )
            print(f"    {facts[name]}", flush=True)
        finally:
            srv.stop()

    print(f"\n=== decode, interleaved A/B, prompt={args.prompt} ===")
    for name in (a_name, b_name):
        vram = f"peak VRAM {peak[name]} MiB" if peak[name] >= 0 else "peak VRAM not measured (nvidia-smi failed)"
        print(f"{name:>5}: {fmt([s.decode_tps for s in results[name]])}   {vram}")
        print(f"       {facts.get(name, '(nothing recorded)')}")
    off = [s.decode_tps for s in results[a_name]]
    on = [s.decode_tps for s in results[b_name]]
    if not off or not on:
        # Exit 2, not 0. `--pairs 0` used to start no server, measure nothing, print "-" for both arms and
        # return success -- and the one line a reader checks, "identical output across arms", simply vanished
        # rather than saying it was not determined.
        print(f"\nmeasured nothing: {a_name}={len(off)} samples, {b_name}={len(on)} samples. "
              f"--pairs must be at least 1.", file=sys.stderr)
        return 2
    m_off, m_on = sum(off) / len(off), sum(on) / len(on)
    print(f"\nmean {a_name} {m_off:.2f}  mean {b_name} {m_on:.2f}  delta {m_on - m_off:+.2f} tok/s "
          f"({100 * (m_on - m_off) / m_off:+.2f}%)")
    # How many pairs went the same way, which is the only thing that makes a difference this small readable. A
    # mean on its own hides "three pairs one way and one the other".
    pairs = list(zip(off, on))
    favour = sum(1 for x, y in pairs if y > x)
    print(f"pairs in favour of {b_name}: {favour} of {len(pairs)}")
    shas = {name: {s.text_sha for s in results[name]} for name in (a_name, b_name)}
    print(f"output sha {a_name}={shas[a_name]} {b_name}={shas[b_name]}")
    print("identical output across arms: " +
          ("YES" if shas[a_name] == shas[b_name] and len(shas[a_name]) == 1 else "NO"))
    Path(args.out).write_text(json.dumps(
        {name: [vars(s) | {"decode_tps": s.decode_tps} for s in results[name]] for name in results}, indent=2))
    print(f"raw samples -> {args.out}")
    return 0


def cmd_prefill(args: argparse.Namespace) -> int:
    logdir = Path(args.logdir)
    targets = [int(t) for t in args.tokens.split(",")]
    arms = arms_for(getattr(args, "drop", None), parse_with(getattr(args, "with_", None)),
                    parse_with_args(getattr(args, "with_arg", None)), getattr(args, "expect", None))
    out: dict[str, dict[int, list[Sample]]] = {}
    wanted = args.arms.split(",")
    unknown = [a for a in wanted if a not in arms]
    if unknown:
        hint = "" if "with" in arms else " (name one with --with to get a 'with' arm)"
        raise SystemExit(f"--arms: no such arm {', '.join(unknown)}; this run has {', '.join(sorted(arms))}{hint}")
    for arm in wanted:
        print(f"arm={arm} starting server ...", flush=True)
        srv = start(arms[arm], logdir / f"prefill-{arm}.log")
        out[arm] = {}
        try:
            completion(srv, "Say the word ready.", 8)
            # The check cmd_decode has always done, and the reason this function was extended for the "with" arm:
            # without it an arm that silently lost the engine's own table is compared against one that kept it.
            assert_default_applied(srv, arm, getattr(args, "drop", None))
            for t in targets:
                prompt = filler(t)
                real = tokenize_len(prompt)
                reps = []
                for r in range(args.reps):
                    # 16 generated tokens: enough for a DONE line, small enough that
                    # decode time does not dominate the request.
                    #
                    # A DIFFERENT prompt each rep, of the same length. Sending the identical prompt to the same
                    # server meant every rep after the first hit prefix reuse, so prompt_ms collapsed towards
                    # zero and prefill_tps was inflated (or divided by exactly 0.0) -- while docs/speed.md
                    # claims the table was measured with reused=0 on every run.
                    s = completion(srv, filler(t, salt=r), 16)
                    reps.append(s)
                    print(f"    {arm} target={t} actual={s.prompt_n} "
                          f"prefill {s.prefill_tps:8.2f} tok/s ({s.prompt_ms:.0f} ms)", flush=True)
                out[arm][real] = reps
            # After the requests, not beside assert_default_applied: a prompt-path switch is announced on its first
            # use, so at startup the log is silent about it even when everything is right.
            assert_with_took_effect(srv, arms[arm])
        finally:
            srv.stop()

    print("\n=== prefill ===")
    print(f"{'prompt tokens':>14} | {'arm':>4} | prefill tok/s")
    for arm, by_len in out.items():
        for n, reps in sorted(by_len.items()):
            print(f"{n:>14} | {arm:>4} | {fmt([s.prefill_tps for s in reps])}")
    Path(args.out).write_text(json.dumps(
        {arm: {str(n): [vars(s) | {"prefill_tps": s.prefill_tps} for s in reps]
               for n, reps in by_len.items()} for arm, by_len in out.items()}, indent=2))
    print(f"raw samples -> {args.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    # default=None, resolved after parsing. tempfile.mkdtemp() as a default is evaluated when the parser is
    # BUILT, so every invocation created and abandoned a /tmp/aegis-bench-* directory -- including `--help`,
    # and including runs that pass --logdir and never use it. Nine were found on the reference machine.
    p.add_argument("--logdir", default=None,
                   help="where to keep the server logs (default: a fresh temporary directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode", help="interleaved A/B of the sm_86 switch stack")
    d.add_argument("--drop", metavar="STRATA_X", default=None,
                   help="measure the full table MINUS this one switch against the full table, which is what the "
                        "per-switch column of docs/speed.md reports. Without it the comparison is the engine's "
                        "own defaults against STRATA_SM86_PERF=0")
    d.add_argument("--pairs", type=int, default=4)
    d.add_argument("--max-tokens", type=int, default=256)
    # The two arms to pair. Decode is where the route tail skip lives, and it could not be measured here at all
    # before: --with and --arms existed on `prefill` only, and --drop refuses any key outside the sm_86 table.
    d.add_argument("--arms", default="off,on",
                   help="the two arms to pair, e.g. 'on,with' for the default path against the default path plus "
                        "whatever --with / --with-arg added")
    d.add_argument("--with", dest="with_", metavar="STRATA_KEY=VALUE", action="append",
                   help="add an environment variable to the 'with' arm (repeatable); name it in --arms to measure it")
    d.add_argument("--with-arg", metavar="ARG", action="append",
                   help='add arguments to the ENGINE\'s command line in the \'with\' arm (repeatable), as one '
                        'quoted string per option, e.g. --with-arg "--tail-skip 7"; needs --expect')
    d.add_argument("--expect", metavar="TEXT",
                   help="text the 'with' arm's engine log must contain, proving --with-arg took effect")
    d.add_argument("--prompt", default="prose", choices=sorted(DECODE_PROMPTS),
                   help="which kind of answer to time (default prose). A gap measured on one prompt is a gap for "
                        "that continuation; run all three before quoting one number")
    d.add_argument("--out", default="bench-decode.json")
    d.set_defaults(func=cmd_decode)

    f = sub.add_parser("prefill", help="prefill rate at several prompt lengths")
    f.add_argument("--tokens", default="2048,8192")
    f.add_argument("--reps", type=int, default=3)
    # Both arms by default. It measured only "off" before, so the documented `bench_switches.py prefill`
    # printed a one-arm table while reading like a comparison.
    f.add_argument("--arms", default="off,on")
    # The prompt path is where an opt-in expert kernel shows up, so the extra arm lives on this subcommand only:
    # `--arms on,with --with STRATA_IQ4_W4A8=2` is the default path against the default path plus that kernel.
    f.add_argument("--with", dest="with_", metavar="STRATA_KEY=VALUE", action="append",
                   help="add a variable to the 'with' arm (repeatable); name it in --arms to measure it")
    f.add_argument("--with-arg", metavar="ARG", action="append",
                   help='add arguments to the ENGINE\'s command line in the \'with\' arm (repeatable), as one '
                        'quoted string per option; needs --expect')
    f.add_argument("--expect", metavar="TEXT",
                   help="text the 'with' arm's engine log must contain, proving --with-arg took effect")
    f.add_argument("--out", default="bench-prefill.json")
    f.set_defaults(func=cmd_prefill)

    args = p.parse_args()
    if args.logdir is None:
        args.logdir = tempfile.mkdtemp(prefix="aegis-bench-")
        print(f"server logs -> {args.logdir}", file=sys.stderr)
    # Absolute, because the server runs with cwd=PROJECT: a bare --out used to drop the JSON into whatever
    # directory the tool was invoked from, which for a reader following docs/speed.md is the repository root.
    args.out = str(Path(args.out).resolve())
    # The argument checks come before the machine checks: a typo in --with or --arms used to be reported only on a box
    # that already had an idle GPU and the model on disk, so the feedback an operator got for a misspelled switch
    # depended on machine state.  parse_with raises; arms_for rejects an unknown --drop.
    # --expect is required with --with-arg, not optional. An engine argument that never arrived -- a typo, a flag
    # this build does not have, a value the engine refuses -- produces an arm that is a second copy of the other
    # one, and two arms agreeing reads as "the setting is worth nothing" rather than "the setting never ran".
    if getattr(args, "with_arg", None) and not getattr(args, "expect", None):
        print("--with-arg needs --expect TEXT: the string the engine's log must contain to prove the argument "
              "took effect. Without it an argument that never arrived measures the other arm again and the two "
              "agreeing looks like a result.", file=sys.stderr)
        return 2
    if getattr(args, "expect", None) and not getattr(args, "with_arg", None):
        print("--expect without --with-arg: nothing would be checked against it.", file=sys.stderr)
        return 2
    arms_now = arms_for(getattr(args, "drop", None), parse_with(getattr(args, "with_", None)),
                        parse_with_args(getattr(args, "with_arg", None)), getattr(args, "expect", None))
    unknown = [a for a in getattr(args, "arms", "").split(",") if a and a not in arms_now]
    if unknown:
        hint = "" if "with" in arms_now else " (name one with --with or --with-arg to get a 'with' arm)"
        print(f"--arms: no such arm {', '.join(unknown)}; this run has {', '.join(sorted(arms_now))}{hint}",
              file=sys.stderr)
        return 2
    # `decode` compares exactly two arms, and this belongs here with the other argument checks rather than
    # inside cmd_decode, which runs after the machine checks below: `decode --arms off` on a box with a busy GPU
    # reported "the GPU is not idle" and said nothing about the arms, so the feedback for a mistyped argument
    # depended on machine state -- the very thing the comment at the top of this block says it fixed.
    if args.cmd == "decode":
        wanted = [a for a in args.arms.split(",") if a]
        if len(wanted) != 2 or wanted[0] == wanted[1]:
            print(f"--arms: decode compares exactly two different arms, got {args.arms!r}; this run has "
                  f"{', '.join(sorted(arms_now))}", file=sys.stderr)
            return 2
    if not gpu_is_idle():
        return 2
    for path in (SHARD1, SHARD2, MODELS / "pack-iq3-xxs", MODELS / "mtp/rt"):
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
