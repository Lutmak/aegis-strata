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


def server_argv() -> list[str]:
    return [
        str(PROJECT / "build/bin/aegis"),
        "--engine", str(PROJECT / "build/bin/strata"),
        "-m", str(SHARD1),
        "--host", "127.0.0.1", "--port", str(PORT),
        "--alias", "qwen3.8-flash-next-iq3_xxs",
        "--sse-ping-interval", "10",
        "--",
        "--pack", str(MODELS / "pack-iq3-xxs"),
        "--native", str(SHARD1),
        "--ple-gguf", str(SHARD2),
        "--expert-profile", str(PROJECT / "data/expert-profile.bin"),
        "--expert-cache", "auto", "--prefill", "auto",
        "--spec", "4", "--spec-min-p", "0.5",
        "--mtp", str(MODELS / "mtp/rt"),
        "--max-context", "32768", "--kv", "int8", "--ple-io", "direct", "--kv-grow",
        "--pool-workers", "15", "--gpu", "0",
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



def arms_for(drop: str | None) -> dict[str, dict[str, str]]:
    """The two environments to compare.

    "on" is deliberately EMPTY: the engine applies its own sm_86 table, so this measures the default path a user
    gets rather than a hand-built environment that happens to name the same variables. assert_default_applied()
    then checks the engine said so.

    With --drop KEY the comparison changes to "the full table minus KEY" against "the full table", which is what
    the per-switch column in docs/speed.md actually reports. Without this the only possible off arm was
    STRATA_SM86_PERF=0 ("everything off"), so a reader following the document could not reproduce that column at
    all -- they could only measure a subset against nothing.
    """
    if drop is None:
        return {"off": {"STRATA_SM86_PERF": "0"}, "on": {}}
    if drop not in SM86_SWITCHES:
        raise SystemExit(f"--drop: {drop} is not in the table; choose from {', '.join(sorted(SM86_SWITCHES))}")
    minus = {k: v for k, v in SM86_SWITCHES.items() if k != drop}
    return {"off": minus, "on": dict(SM86_SWITCHES)}


def assert_default_applied(srv: "Server", arm: str, drop: str | None) -> None:
    """On the default arm, fail loudly unless the engine printed the line that proves it applied its table.

    Without this the tool cannot tell "the defaults are on" from "the defaults silently did nothing", which is
    the exact failure it is supposed to detect.
    """
    if arm != "on" or drop is not None:
        return
    text = srv.log.read_text(errors="replace")
    if "speed switches on by default" not in text:
        raise RuntimeError(
            "the engine did not print its sm_86 startup line, so the default table was NOT applied: this arm "
            f"would measure the slow path and report it as the default. See {srv.log}"
        )


def start(env_extra: dict[str, str], log: Path, ready_timeout: float = 240.0) -> Server:
    env = os.environ | env_extra
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("w")
    proc = subprocess.Popen(
        server_argv(), stdout=handle, stderr=subprocess.STDOUT,
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


DECODE_PROMPT = (
    "Explain, in careful detail and without using lists, how a modern operating system "
    "schedules threads onto CPU cores, and why that design is the way it is."
)


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


def cmd_decode(args: argparse.Namespace) -> int:
    logdir = Path(args.logdir)
    arms = arms_for(getattr(args, "drop", None))
    results: dict[str, list[Sample]] = {"off": [], "on": []}
    order: list[str] = []
    for i in range(args.pairs):
        # Alternate which arm leads, so a warming or cooling trend cannot favour one.
        order += ["off", "on"] if i % 2 == 0 else ["on", "off"]

    peak = {"off": 0, "on": 0}
    for n, arm in enumerate(order, 1):
        print(f"[{n}/{len(order)}] arm={arm} starting server ...", flush=True)
        srv = start(arms[arm], logdir / f"{n:02d}-{arm}.log")
        try:
            assert_default_applied(srv, arm, getattr(args, "drop", None))
            # One discarded warm-up request: the first decode after load is not representative.
            completion(srv, "Say the word ready.", 8)
            s = completion(srv, DECODE_PROMPT, args.max_tokens)
            results[arm].append(s)
            peak[arm] = max(peak[arm], srv.peak_vram_mib)
            print(
                f"    decode {s.decode_tps:7.2f} tok/s   produced={s.produced} "
                f"finish={s.finish} drafts={s.drafts_accepted}/{s.drafts_offered} "
                f"vram={srv.peak_vram_mib} MiB sha={s.text_sha}",
                flush=True,
            )
        finally:
            srv.stop()

    print("\n=== decode, interleaved A/B ===")
    for arm in ("off", "on"):
        print(f"{arm:>4}: {fmt([s.decode_tps for s in results[arm]])}   peak VRAM {peak[arm]} MiB")
    off = [s.decode_tps for s in results["off"]]
    on = [s.decode_tps for s in results["on"]]
    if not off or not on:
        # Exit 2, not 0. `--pairs 0` used to start no server, measure nothing, print "-" for both arms and
        # return success -- and the one line a reader checks, "identical output across arms", simply vanished
        # rather than saying it was not determined.
        print(f"\nmeasured nothing: off={len(off)} samples, on={len(on)} samples. "
              f"--pairs must be at least 1.", file=sys.stderr)
        return 2
    if True:
        m_off, m_on = sum(off) / len(off), sum(on) / len(on)
        print(f"\nmean off {m_off:.2f}  mean on {m_on:.2f}  delta {m_on - m_off:+.2f} tok/s "
              f"({100 * (m_on - m_off) / m_off:+.2f}%)")
        shas = {arm: {s.text_sha for s in results[arm]} for arm in ("off", "on")}
        print(f"output sha off={shas['off']} on={shas['on']}")
        print("identical output across arms: " + ("YES" if shas["off"] == shas["on"] and len(shas["off"]) == 1 else "NO"))
    Path(args.out).write_text(json.dumps(
        {arm: [vars(s) | {"decode_tps": s.decode_tps} for s in results[arm]] for arm in results}, indent=2))
    print(f"raw samples -> {args.out}")
    return 0


def cmd_prefill(args: argparse.Namespace) -> int:
    logdir = Path(args.logdir)
    targets = [int(t) for t in args.tokens.split(",")]
    arms = arms_for(getattr(args, "drop", None))
    out: dict[str, dict[int, list[Sample]]] = {}
    for arm in args.arms.split(","):
        print(f"arm={arm} starting server ...", flush=True)
        srv = start(arms[arm], logdir / f"prefill-{arm}.log")
        out[arm] = {}
        try:
            completion(srv, "Say the word ready.", 8)
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
    d.add_argument("--out", default="bench-decode.json")
    d.set_defaults(func=cmd_decode)

    f = sub.add_parser("prefill", help="prefill rate at several prompt lengths")
    f.add_argument("--tokens", default="2048,8192")
    f.add_argument("--reps", type=int, default=3)
    # Both arms by default. It measured only "off" before, so the documented `bench_switches.py prefill`
    # printed a one-arm table while reading like a comparison.
    f.add_argument("--arms", default="off,on")
    f.add_argument("--out", default="bench-prefill.json")
    f.set_defaults(func=cmd_prefill)

    args = p.parse_args()
    if args.logdir is None:
        args.logdir = tempfile.mkdtemp(prefix="aegis-bench-")
        print(f"server logs -> {args.logdir}", file=sys.stderr)
    # Absolute, because the server runs with cwd=PROJECT: a bare --out used to drop the JSON into whatever
    # directory the tool was invoked from, which for a reader following docs/speed.md is the repository root.
    args.out = str(Path(args.out).resolve())
    if not gpu_is_idle():
        return 2
    for path in (SHARD1, SHARD2, MODELS / "pack-iq3-xxs", MODELS / "mtp/rt"):
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
