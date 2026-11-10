# aegis-strata

Run one large language model on your own machine, behind an OpenAI-compatible HTTP API on
localhost. Point your coding agent at it instead of a cloud provider.

It runs **Qwen3.8-Flash-Next** on a single NVIDIA GPU. The model is far larger than any consumer
card's memory, so most of it stays in system RAM and only the parts each token needs are streamed to
the GPU. On a 12 GB card it answers at about 98 tokens a second.

You compile it yourself. There is no installer, no download button, no web interface, no account.

## Why you might pick this one

This began as a fork of an existing engine, and most of the work since has been taking things out
and locking down what was left. The differences that matter to you:

**Nothing runs on your machine that you did not compile.** The projects this came from offer a
quicker path: download a prebuilt engine, or copy a ready-made weights pack from someone's file
host, and run it. That means executing a binary you cannot inspect, and one of those projects
installs system packages with `sudo` along the way. All of it is gone here. You build from source,
and if you ever see a file claiming to be an "aegis-strata release binary", it is not from this
project.

**The part that reads your prompts cannot reach the network.** The engine runs as its own process
inside an empty network namespace — not firewalled, but placed somewhere no network exists — behind
a kernel filter that refuses to create any socket except a local one. Its view of your filesystem is
cut down to the model files and the libraries it loads, so it cannot read `~/.ssh` or your shell
history even if something goes wrong inside it. If any of that cannot be set up, the server refuses
to start the engine rather than starting it unprotected. This does not stop your agent from
searching the web — see below, because it is the question everyone asks first.

**It does one thing.** The engine it came from handles six model families across five GPU backends,
with a Windows installer and a web UI. That is all removed. What is left is about a quarter of the
code: a quarter as much to go wrong, and a quarter as much to read if you want to check it yourself.

**The claims in these documents are tested.** `make test` runs the containment checks on any Linux
box, with no GPU and no model file. Part of that suite also checks that every file path and number
quoted in the documentation still matches the code, and fails when one does not.

## What you need

Linux on x86-64, an NVIDIA GPU with its driver, a CUDA toolkit, CMake 3.24 or newer, a C++20
compiler, `make` and `git`. The tests also need `python3` and `curl`.

A 12 GB card is enough. You also need plenty of system RAM — the model occupies about 43 GB while
running.

## Step 1: build

```
git clone https://github.com/Lutmak/aegis-strata && cd aegis-strata
git submodule update --init
make build CUDA_ARCHS=86     # 86 is RTX 30-series; use your card's compute capability
make test
```

Three files land in `build/bin/`:

| | |
|---|---|
| `aegis` | the server: serves the HTTP API and starts the engine as a child process |
| `strata` | the engine: does the inference on the GPU |
| `libstrata_kq.so` | extra matrix kernels the engine loads for some quantisation formats |

Two processes instead of one is deliberate. The server has to hold a listening socket, so it cannot
be cut off from the network. The engine has no such need, so it is the one that gets locked down.
Splitting them is what makes that possible.

`make server` builds `aegis` alone without CUDA, which is handy on a laptop. `make test` needs no GPU
and no model, so it runs anywhere.

**On compilers.** `nvcc` accepts only certain GCC versions, and mixing two of them breaks the link.
The Makefile tries `g++-15`, `g++-14` and so on until one works, and says which it chose. Override
with `HOSTCXX=/usr/bin/g++-14 make build`.

## Step 2: get the model

Only **Qwen3.8-Flash-Next** in the GSQ-RCO IQ3_XXS quantisation is supported.

Download the two GGUF shards on a machine with network access, check their SHA-256 sums, and copy
them across. Nothing here downloads a model for you.

Then prepare the two directories the engine needs:

```
uv venv .venv && uv pip install --python .venv/bin/python -r tools/requirements.txt

python3 tools/iq_pack.py  --gguf <shard1> --out <pack>
python3 tools/mtp_fetch.py fetch  --out <mtp>
python3 tools/mtp_fetch.py verify --out <mtp>      # must exit 0 — do not skip this
python3 tools/mtp_pack.py --src <mtp> --experts q2_0 --out <mtp>/mtp-q2_0.gguf
python3 tools/mtp_rt.py   --gguf <mtp>/mtp-q2_0.gguf --out <mtp>/rt
cp data/draft_vocab.bin <mtp>/rt/draft_vocab.bin
```

`tools/requirements.txt` pins every package by hash, so a tampered download fails the install rather
than running. `mtp_fetch.py verify` checks each tensor's hash and exits 3 listing any that fail.

This is one-off work you run by hand. It is not part of the build and not part of the server.

## Step 3: run

```
build/bin/aegis --engine build/bin/strata \
  -m /models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf \
  --host 127.0.0.1 --port 8080 --alias qwen3.8-flash-next-iq3_xxs --sse-ping-interval 10 \
  -- --pack /models/pack-iq3-xxs \
     --native /models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf \
     --ple-gguf /models/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf \
     --expert-profile data/expert-profile.bin \
     --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.5 \
     --mtp /models/mtp/rt \
     --max-context 32768 --kv int8 --ple-io direct --kv-grow \
     --pool-workers 15 --gpu 0
```

Options before `--` belong to the server; options after `--` go to the engine. `-m` is only where the
vocabulary and chat template are read from — the server loads no model weights.

Point your agent at `http://127.0.0.1:8080/v1`. Thirteen routes are served:
`/v1/chat/completions` (streaming or not), `/v1/completions`, `/v1/models`, `/v1/health`, the same
four without the `/v1` prefix, and `/completion`, `/health`, `/props`, `/tokenize`, `/detokenize`
and `/apply-template`.

Four choices in that command line matter on a 12 GB card, and differ from what you may have seen
elsewhere:

- **`--mtp .../rt`**, not the directory above it. The engine looks for `dense.txt` directly inside
  the path you give, and fails at load otherwise.
- **`--ple-io direct`** reads a large lookup table from SSD as it goes. The alternative locks that
  table in RAM, and the engine is already using about 43 GB of 62 GB.
- **`--kv-grow`** lets the context cache claim GPU memory as the conversation fills it, rather than
  reserving all of it at load. A 256-token answer peaks at 11,610 MiB of 12,288, and a long conversation
  that fills the context has reached 11,670 — close enough to the limit that there is nothing spare.
- **`--gpu 0`** stated explicitly, in case a second GPU is driving your display.

## The engine is offline. Your agent is not.

This is the part people get wrong, so it is worth being plain about.

**Web search still works.** When the model decides to look something up, it does not open a
connection. It writes out a tool call as text. That text travels back to your agent, **your agent**
runs the search with its own network access, and the result arrives in the next request as more
text. Deciding to use a tool and running it are two different jobs, and running it was always the
client's. Nothing here changes that, so searching, fetching a page and reading your repository
behave exactly as they would against a cloud provider.

**What the engine cannot do is reach the network itself.** It has no reason to: it takes a prompt
and returns tokens. The binary links no network code at all — `nm -D --undefined-only` on the
engine finds no `connect`, `socket` or `getaddrinfo`, and the same command on the server finds all
three (fourteen symbols across the whole socket API). The containment is there for the case where something goes wrong inside the engine: a
malformed GGUF, a bug in a CUDA kernel, a flaw in the pack parser. Code that gets control there
still cannot open a socket to send your prompts somewhere, and still cannot read `~/.ssh`. That
turns "it has no network code today" into "it cannot acquire any", which is a different and more
durable claim.

So the boundary is between the two processes, not between the model and the world:

| | network | contained |
|---|---|---|
| your agent | yes | no — it is your program, not ours |
| `aegis`, the server | yes, it holds the listening socket | not yet (see `docs/security.md`) |
| `strata`, the engine | no | yes |

**If you want the model cut off from the web**, that is a setting in your agent — stop giving it a
search tool. This server cannot do it for you. By the time a tool call reaches your agent, the
server is no longer in the conversation and never sees whether the tool ran.

The containment is on by default and these are the only switches:

| | |
|---|---|
| *(default)* | the engine is contained, and the server refuses to start it if that cannot be set up |
| `--no-sandbox` | start the engine uncontained; prints a warning saying exactly that |
| `--sandbox-allow-read PATH` | also let the engine read this path, repeatable |
| `--sandbox-allow-write PATH` | also let it write there, repeatable |

`GET /props` reports which of those you actually got, as `sandbox`.

### When the engine stops answering

The engine is a separate process, so it can fail in three ways you can tell apart from outside.
If it **exits**, the request gets a 503 and the server exits too, with a non-zero status. If it
**stops answering without exiting** — a hung kernel, a deadlock — then `--engine-timeout` bounds the
wait: after that many seconds with no output at all from the engine during a request, the request
gets a 503 and the server ends, again non-zero. The default is 120 seconds, and `0` waits for ever.
Do not set it below about 15: reading a long prompt is silent, and a maximal 32,768-token prompt is
about 13 seconds of silence, so a smaller value would cut off healthy requests. It bounds silence,
not total time, so a slow engine that keeps producing tokens is never cut off. Third, it can
**fail one request and then exit** — a CUDA kernel that will not launch, say: that request gets a
500 naming the error, and the server ends non-zero as before. The engine does not try to carry on
after a kernel failure, because a CUDA error of the sticky kind poisons the context and every later
launch would fail too.

All three end the process rather than carrying on, because the engine takes one request at a time
and the server does not start a replacement one, so the next request would fail the same way. Run it
under a unit with `Restart=on-failure` and the pair comes back. Being **asked** to stop is different:
`SIGTERM` and `SIGINT` exit 0, including while a request is generating, so `systemctl stop` does not
look like a failure and does not trip that restart.

## What it will not do

- Download or run a binary it did not compile. The single exception is your toolchain: the compiler
  and CUDA are prebuilt binaries, as they are for every build of every project.
- Touch the network during a build, or at any point while serving. No telemetry, no update check, no
  model fetch.
- Run Python between a request arriving and a token coming back. The server is C++; Python is only
  for the offline model preparation above.
- Ask for `sudo`, or write anything outside the build directory.
- Ship code it cannot run. Unused engines, backends and model families were deleted rather than
  switched off, because a disabled code path still ships and still has to be read by anyone
  reviewing it.

## What is not supported

- **Any model but Qwen3.8-Flash-Next.** The other families, and the multi-model engine behind them,
  were removed.
- **Windows**, **Intel Arc / SYCL**, and multi-GPU setups.
- **macOS / Metal and AMD / ROCm.** The source for both is still here and the normal build cannot
  compile either. Neither is built or tested on any machine the maintainer has, so treat both as
  unverified: the Metal tree is kept consistent with the shared headers by inspection only, and the
  last time a header gained a parameter the Metal stub was missed. If you get one working, say so.
- No web interface, installer, model downloader, auto-update or image generation. None are planned.

## Speed and memory

Measured on one machine — RTX 3080 Ti (12 GB), Ryzen 9 7900X, 62 GB RAM, Arch Linux — using the
command above. These are that machine's numbers, not a promise about yours, and not a comparison
against any other engine.

| | |
|---|---|
| Load until ready | 15–18 s |
| Reading your prompt | 1,281 tokens/s at 2K, 2,547 at 8K |
| Writing the answer | ~98 tokens/s |
| GPU memory at peak | 11,610 MiB of 12,288 for a short answer; 11,670 with the context full |
| System RAM at peak | 43 GB |
| Context | 32,768 tokens with `--kv int8` |

Prompt reading is quoted at two lengths on purpose: the rate climbs with the prompt, because a fixed
per-request cost is spread over more tokens. A single figure would be misleading, and an earlier
version of this table quoted ~85 tokens/s, which was measured at a 67-token prompt and understated
a realistic one by more than ten times.

Turning the engine's containment off made no measurable difference — 88.5 against 89.0 tokens a
second across several runs — so the protection costs you nothing.

**Seven speed switches are on by default on an RTX 30-series card**, worth about +9.7% of that
writing speed for 2 MiB of GPU memory. They also change the model's exact wording, so
`docs/speed.md` is the disclosure: what they are, what each is worth, how to turn them off with
`STRATA_SM86_PERF=0`, and how to re-measure them on a card that is not this one with
`tools/bench_switches.py`. If your GPU is not an RTX 30-series, none of them are on and that
document is where to start looking for the same win.

## Security

`SECURITY.md` is how to report a problem. `docs/security.md` is the long version: what an attacker
can and cannot do, how the containment is built, and the list of things it does *not* protect you
from. The GPU driver interface, for one, is a wide path that no sandbox of this kind closes.

The engine's CUDA kernels are inherited and unaudited. They are most of the code here, and building
them from source proves where they came from, not that they are correct.

## If you are a coding agent

- Build with `make build CUDA_ARCHS=<arch>`, test with `make test` (no GPU needed).
  `make test-engine` builds and runs the engine's own suite, which does need a GPU.
- `server/` is the HTTP server. `src/` and `include/` are the inference engine. `tools/` is offline
  model preparation. The server links no engine code, and that boundary is the point — do not cross
  it.
- Before adding a sentence to any document here, ask whether a change to the code alone could make
  it false. If it could, cite text you can `grep` for rather than a line number, and run
  `python3 tools/check_docs.py` — it fails on a broken reference.
- Do not add anything that downloads and runs a binary, reaches the network while serving, or puts
  Python in the serving path.

## Where this came from

A fork of [UnbiasedStrata](https://github.com/robertkarl/UnbiasedStrata), which forked
[PolyStrata](https://github.com/simplexai-labs/PolyStrata), which forked
[Strata](https://github.com/Niko1221/Strata). The CUDA engine is theirs and is the reason any of
this works. The first commit is their code exactly as it was, and every commit after it is this
fork's own work, so `git diff 39cab36 HEAD` is the entire difference with nothing hidden in
between.

llama.cpp is vendored as a pinned submodule and supplies the HTTP layer, the tokenizer and the chat
template. Model weights carry their own licences, which are not this one.

## Licence

MIT. See [LICENSE](LICENSE).
