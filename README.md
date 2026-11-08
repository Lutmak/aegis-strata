# aegis-strata

aegis-strata serves one large language model over an OpenAI-compatible HTTP API on localhost, for a
coding agent to use. You compile it yourself; there is nothing else to it — no installer, no model
downloader, no web UI, no desktop app.

It is a fork of [UnbiasedStrata](https://github.com/robertkarl/UnbiasedStrata), which forks
[PolyStrata](https://github.com/simplexai-labs/PolyStrata), which forks
[Strata](https://github.com/Niko1221/Strata). It keeps their CUDA engine, which is the only reason a
model far larger than the card's memory runs on a 12 GB card, and deletes almost everything around
it.

## What it refuses to do

This is the point of the project, so it comes before the build instructions.

1. **No prebuilt binary of this project, ever.** Nothing is downloaded and executed. Nothing is
   vendored as a binary. There is no `--prebuilt` flag and no code path that fetches an engine.
   Distribution is source only. Any file claiming to be an aegis-strata release binary is not from
   this project. The rule is about what this project ships and runs, and it has exactly one
   exception, named here rather than left implicit: the toolchain. A compiler, a CUDA toolkit and
   CMake are prebuilt binaries that your package manager or Nix installed, and every build of every
   project trusts them. `THREAT_MODEL.md` lists that among what is not covered.
2. **No network during the build.** Cloning the repository and its submodule needs network, of
   course. Compiling it does not, and that is measured rather than asserted: the whole build
   completes inside `bwrap --unshare-net`, in a network namespace holding only `lo`, with egress
   checked dead first (TCP to a public host refused, UDP to a public resolver unreachable).
3. **No network at run time.** No telemetry, no update check, no model fetch, no version ping. The
   server opens a listening socket and nothing else. For the engine this is *enforced*, not just
   intended: the front end runs it in a network namespace holding only `lo`, behind a seccomp filter
   that refuses every socket domain but `AF_UNIX`, and refuses to start it if that cannot be
   installed. `make test` proves it with no GPU. The process that serves HTTP is not yet filtered the
   same way — `THREAT_MODEL.md` says exactly how far the claim goes.
4. **No Python between a request and a token.** The server is C++. Python is used only for offline
   work you run yourself: preparing model packs, and the test suite's fake engine.
5. **No privilege escalation.** The build never calls `sudo` and never installs a system package.
   It writes the build directory, and one temporary file while it probes for a compiler nvcc
   accepts. If a dependency is missing it stops and names it.
6. **Every download is pinned and verified, or it does not happen.** The llama.cpp submodule is
   pinned by commit hash. A verification that cannot run is a failure, never a warning.
7. **Code this build can reach is deleted, not disabled.** An `OFF` build option is still code that
   ships, still code a stray flag can turn on, and still code a reviewer has to read, so the engines,
   backends and model families this fork does not serve are gone rather than switched off. Two trees
   are kept on purpose and this is the whole of the exception: `metal/`, which no option here can
   compile — it is a separate macOS-only project that nothing in this build references — and the ROCm
   path, which `-DSTRATA_ENABLE_HIP=ON` would compile against a toolkit that is not installed here.
   Neither has been built or run on the reference machine, so neither is supported. Both were audited
   against the rules above, and the two files that broke them are deleted: a Windows script that
   downloaded an unpinned prebuilt toolchain from a nightly index and then executed it, and a macOS
   installer that fetched models and opened a browser. What remains compiles nothing and fetches
   nothing.

Rules 1, 2 and 5 exist because the parent projects break them. `THREAT_MODEL.md` says where and how.

## Build

Needs: Linux x86-64, an NVIDIA GPU and its driver, a CUDA toolkit, CMake 3.24 or newer, a C++20
compiler, make and git. `make test` additionally needs `python3` and `curl`.

```
git clone https://github.com/Lutmak/aegis-strata && cd aegis-strata
git submodule update --init
make build CUDA_ARCHS=86          # 86 = RTX 30 series; use your card's compute capability
make test
```

`make build` produces three files in `build/bin/`:

- `strata` — the inference engine.
- `libstrata_kq.so` — K-quant matmul kernels, opened by the engine next to its own binary when a
  model needs them.
- `aegis` — the front end, which serves the HTTP API and runs the engine as a child process.

`make frontend` builds `aegis` alone, without CUDA, for a laptop or a CI box. `make test` runs the
front end against `frontend/tests/fake-engine.py`: no GPU and no model file needed, so the tests can
run anywhere.

**About compilers.** nvcc accepts only some GCC versions, and mixing two libstdc++ generations fails
at the link. The Makefile probes `g++-15`, `g++-14`, … against nvcc and hands CMake the newest one
nvcc accepts; `HOSTCXX=/usr/bin/g++-14 make build` overrides the probe. It also drops CUDA
architectures the installed nvcc rejects, with a note, instead of failing.

**What was built here.** nvcc 13.4.92, cmake 4.4.5, `CUDA_ARCHS=86`. The probe selected g++-15; the
box's default gcc 16.2 is rejected by nvcc and would fail. A clean build takes 2 minutes 44 seconds
and compiles 304 object files.

## Model files

The only supported model is **Qwen3.8-Flash-Next**, in the GSQ-RCO IQ3_XXS quantization. The engine
needs three things: the two GGUF shards, a *pack* directory built from shard 1, and an MTP draft
directory.

Download the shards on a machine that has network, check their SHA-256, and copy them to the
inference box. Neither the build nor the server fetches a model.

Then build the pack and the draft directory locally:

```
python3 tools/iq_pack.py --gguf <shard1> --out <pack>
python3 tools/mtp_fetch.py fetch  --out <mtp>
python3 tools/mtp_fetch.py verify --out <mtp>     # checks every tensor's hash; must exit 0
python3 tools/mtp_pack.py --src <mtp> --experts q2_0 --out <mtp>/mtp-q2_0.gguf
python3 tools/mtp_rt.py --gguf <mtp>/mtp-q2_0.gguf --out <mtp>/rt
cp data/draft_vocab.bin <mtp>/rt/draft_vocab.bin
```

These tools need `numpy`, `regex`, `pyyaml` and `tqdm`. `tools/requirements.txt` is a fully
hash-pinned lock, so install it into a virtualenv and let the hashes be checked:

```
uv venv .venv && uv pip install --python .venv/bin/python -r tools/requirements.txt
```

`mtp_fetch.py` reads tensors from Hugging Face at a pinned model revision, and `verify` hash-checks
every one and exits 3 listing any that fail; do not skip it. All of this is offline tooling you run once by hand — not part of the build, and not
part of the server.

Upstream offers a shortcut: copy a prebuilt pack from a published repository instead of building
one. Do not use it. That is exactly the untrusted-artifact path this project exists to remove, and
it was refused here.

## Run

One working invocation, on a 12 GB card:

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

`-m` names the GGUF the vocabulary and chat template are read from; the front end loads no tensors
from it. Everything before `--` is a llama-server option (`--host`, `--port`, `--api-key`, `--alias`,
`--temp`, `--reasoning-format`, `--sse-ping-interval`, …). Everything after `--` goes to the engine,
which is started as a child process with `--serve` appended.

Point your agent at `http://127.0.0.1:8080/v1`. Thirteen routes are served: `/v1/chat/completions`
(streaming and not), `/v1/completions`, `/v1/models`, `/v1/health`, the same four without the `/v1`
prefix, `/completion` (singular, llama.cpp's own), `/health`, `/props`, `/tokenize`,
`/detokenize` and `/apply-template`.

Four details in that invocation that upstream's README gets wrong or sizes for a 24 GB card:

- **`--mtp /models/mtp/rt`, not `/models/mtp`.** The engine looks for `dense.txt` directly inside the
  path you give. Upstream's README points at the parent directory, which fails hard at load.
- **`--ple-io direct`** (the default) rather than upstream's `--ple-io ram`. `ram` locks the whole PLE
  table into RAM, and the engine already peaks at 42.7 GiB of the reference machine's 62 GiB; `direct` reads the
  table from SSD instead.
- **`--kv-grow`** rather than a fixed `--kv-resident`, so the K/V cache claims VRAM as the context
  fills it instead of all of it at load. Peak VRAM below is 11,670 MiB of the card's 12,288, so
  there is nothing to spare.
- **`--gpu 0`** stated explicitly, because the reference machine has a second GPU driving the display and the
  engine should not touch it.

## Measured, on the reference machine

Every measurement in this repository was taken on one machine, and the documents call it **the
reference machine**:

| | |
|---|---|
| GPU | NVIDIA RTX 3080 Ti, 12,288 MiB, `sm_86`, driver 615.78.08 |
| CPU / RAM | AMD Ryzen 9 7900X (24 threads), 62 GiB |
| OS / kernel | Arch Linux, 7.2.14-arch1-1 |
| CUDA | nvcc 13.4 |
| LSM / sysctl | `capability,landlock,lockdown,yama,bpf`; `yama.ptrace_scope = 1` |

A number below is that machine's, not a promise about yours. Qwen3.8-Flash-Next GSQ-RCO
IQ3_XXS, the invocation above:

| | |
|---|---|
| Load to ready | 17–18 s |
| Time to first token, 13-word prompt | 0.71 s |
| Time to first token, ~7,100-token prompt | 2.84 s (about 2,500 tok/s prefill) |
| Decode, streamed | 89–92 tok/s; 103 tok/s with a fully cached prompt |
| 128-token request, end to end | 62–65 tok/s cold, 100–105 tok/s warm (this includes reading the prompt) |
| 4,061-token prompt, 64 tokens out | ~25 tok/s end to end |
| Peak VRAM | 11,670 MiB of 12,288 |
| Peak engine RSS | 42.7 GiB of 62 |
| Context | 32,768, with `--kv int8` |

These are from one box, one model file and one set of flags. They are not a benchmark against any
other engine.

## What is not supported

- **Intel Arc / SYCL.** Removed from the tree.
- **The DeepSeek-V4.1 engine.** Removed. DeepSeek-V4.1 needs roughly 225 GiB on fast storage, which
  the development box does not have, so it is deferred rather than maintained untested.
- **Every other model family** — Qwen3.6 included — and the multi-model engine that served them
  (`strata-poly`). Removed. Qwen3.8-Flash-Next is the only model this serves.
- **Windows.** Upstream's Windows branches are still in the tree. Nothing here builds or tests them.
  Treat Windows as unsupported.
- **macOS / Apple Silicon (Metal) and AMD / ROCm.** Upstream's code for both is kept, unmodified
  except for the two deletions rule 7 names. Neither has been built or run here, so neither is
  supported — it is just not deleted. If you get one working, say so.
- **No web UI, no installer, no model downloader, no update mechanism, no image generation.** None of
  these will be added.

## Known limits

- **`tool_choice=required` does not work**, and neither does anything else that needs constrained
  decoding (`json_schema`, grammars). The engine samples inside its own process and exposes no logits
  across the process boundary, so a grammar cannot constrain it. The request is still served and the
  output parser still runs; the server logs a warning instead (see the grammar check in
  `frontend/strata_context.cpp`). Agents that do prompt-based tool calling work. Agents that
  need constrained decoding do not.
- **One request at a time.** There is no parallel serving.
- **The engine is sandboxed, which means its filesystem is an allowlist.** The paths named in the
  invocation are allowed automatically; anything else the engine needs to read or write has to be
  given with `--sandbox-allow-read` or `--sandbox-allow-write`. A path you pass to one of those flags
  that cannot be resolved stops startup rather than being skipped; an engine argument that looks like
  a path and does not exist is only noted in the log, because the engine may mean to create it. `--no-sandbox` turns the whole thing off behind a loud banner.
  This is Linux-only; on macOS there is no sandbox and the server says so.
- **The model thinks a lot.** Qwen3.8-Flash-Next's chat template sets reasoning effort to "xhigh", so
  the model spends heavily before it answers. With a small `max_tokens` you can get
  `finish_reason: length`, an empty `content`, and the entire response in `reasoning_content`. Give
  it room.
- **Builds are not yet reproducible.** Pinning the toolchain so that two builds of the same commit
  produce the same hashes is planned work, not a property this project has today.

## Security

The CUDA kernels are the bulk of the engine, and they are **unaudited**. Compiling them ourselves
proves where they came from; it does not prove they are correct or safe. Nobody has reviewed them,
including us. There has been no external security audit of anything here.

`THREAT_MODEL.md` states what this project protects, what it does not, and which of its own
invariants are enforced versus merely intended. No parent project has one. `SECURITY.md` says how to
report a vulnerability.

## Credit and licence

Every project this is built from is MIT licensed. The work below is other people's:

- **Strata** — [Niko1221/Strata](https://github.com/Niko1221/Strata). The engine and its CUDA kernels
  originate here.
- **PolyStrata** — [simplexai-labs/PolyStrata](https://github.com/simplexai-labs/PolyStrata), tag
  `v0.1.45-poly.1`, commit `d0cd84dc`. The base tree, and the expert-streaming architecture that
  makes this run on a small card.
- **UnbiasedStrata** — [robertkarl/UnbiasedStrata](https://github.com/robertkarl/UnbiasedStrata),
  commit `ff19037a`. The C++ front end, the first round of deletions, and this build system. It also
  publishes no release binaries at all, which is the practice this fork keeps.
- **llama.cpp / ggml** — the ggml authors, via the `robertkarl/llama.cpp` fork pinned by commit in
  `third_party/llama.cpp`. The tokenizer, the Jinja chat templates, the chat-format parsers, the HTTP
  server code and the image encoder are theirs, compiled from the submodule unmodified.
- **Qwen3.8-Flash-Next** is by the Qwen team. The IQ3_XXS GSQ-RCO file is
  [ISTA-DASLab](https://huggingface.co/ISTA-DASLab)'s. Model files carry their own licences, which are
  not MIT.

The first commit in this repository is UnbiasedStrata `ff19037a` imported unmodified, and its tree
hash is identical to upstream's. So `git diff <that commit> HEAD` is the complete and permanent
record of what this fork changed.

This repository is MIT licensed; see [LICENSE](LICENSE).
