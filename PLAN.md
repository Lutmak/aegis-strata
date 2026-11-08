# UnbiasedStrata 2: plan

Fork of PolyStrata v0.1.45-poly.1 (VecSzn/PolyStrata, commit d0cd84dc) that keeps the engines and
replaces the Python around them with C++ from the llama.cpp tree the engines already pin.

Status: decisions taken 2026-10-17 (§12). Items marked **[D]** were open at review time; §12 is the answer.

Sources: inventories of `serve/`, `tools/`, the CMake tree, the previous fork
(`~/Code/UnbiasedStrata`) and llama.cpp `tools/server` at commit 3cf03257, taken 2026-10-17.
File:line cites refer to the upstream checkout at `PolyStrata-v0.1.45-poly.1/` unless noted.

## 1. What upstream is, in one page

Two engine binaries, both speaking the same line protocol over stdin/stdout:

- `strata` (`src/program/generate.cpp`, 16k lines): Qwen3.8-Flash-Next from packs (`--pack`, `--mtp`).
  Also runs `--native` GGUF for qwen4exp/qwen35moe and dispatches qwen35moe to `src/qwen35/`.
- `strata-poly` (`src/poly/`, 11k lines): GGUF read in place. glm5-next, qwen35moe (Qwen3.6-35B-A3B,
  Ornith, Tiel Coder, MiniCPM-V), gemma4, deepseek4, lfm2moe. No packing step.

The protocol (`docs/BACKENDS.md`, `serve/server.py:861-2080`, `generate.cpp:11621-14631`):

```
in:   GEN  <max_new> [key=value ...] <id,id,...>
      GENI <max_new> [key=value ...] <embeddings-file> <id,id,...>
      STOP | QUIT | SAVE <path> | RESTORE <path> | VRAM [mib] | BGEN/BSTOP/BYIELD (--batch)
out:  INFO k=v ... ; READY <max_context> stop
      T <id> | PP <done> <total> <ms> <tok/s> | RESUME <n> | REUSED <n>
      DONE <generated> <prompt> <prompt_ms> <decode_ms> <finish> <drafts_acc> <drafts_off> <reused> ...
      ERR <msg> | FATAL <msg>
```

The engine never tokenizes, never sees text, never sees a tool call. It samples on its own (GPU in
`strata`, CPU in `strata-poly`) and returns token ids. Images arrive as embedding rows in a file
(`SVE1` records) produced by `strata-vision`, a 302-line C++ child built on llama.cpp `mtmd`
(`tools/vision/`).

Everything else is Python: tokenizer (`tools/strata_tokenizer.py`, hand-written BPE), Jinja
templating (jinja2), five per-family tool-call parsers, SSE, three API dialects, MCP, metrics, web
UI, model switching, installers. `serve/` is 13.2k lines of non-test Python plus 16.9k of tests.
`setup.py` is 8,223 lines.

The engines use no `llama_*` API. They include `ggml.h`, `ggml-cpu.h`, and compile selected
`ggml-cuda` sources (MMQ instances, `quantize.cu`) directly into their own libraries. The build
takes `ggml-base` and `ggml-cpu` from `STRATA_GGML_DIR` and nothing else; `STRATA_GGML_DIR` empty
means `FetchContent` of llama.cpp at 3cf03257 (`CMakeLists.txt:1459-1468`). The engine build runs no
Python. The only build-time network access is that fetch.

## 2. Measured baselines already in hand

RTX 3090 24 GB, EPYC 7H12, CUDA 12.8, same pod, 2026-10-17. Recorded in this repo under
`benchmark-results/` (upstream) and `results/` (llama.cpp).

| Engine | Model | Decode tok/s | PP tok/s at 32K | Notes |
| --- | --- | ---: | ---: | --- |
| PolyStrata v0.1.45 `strata` | Flash-Next GSQ-RCO IQ3_XXS, MTP spec 4 | 112.5 | 2,404 | 15 expert workers, int8 KV, 64K ctx |
| llama.cpp 797de63 | Flash-Next GSQ-RCO IQ2_XS, MTP draft 2 | 41 to 48 | 515 to 558 | 14 GB expert cache, different quant |
| PolyStrata (upstream docs, RTX 5090) | Qwen3.6-35B-A3B UD-Q3_K_M | 323 / 268 / 179 | 4,239 to 5,789 | `docs/BACKENDS.md`; not our card |

No Qwen3.6 number exists for the 3090 yet, on either side. Step 8 produces both.

## 3. Inventory of `serve/`: keep, write, or drop

"llama.cpp has it" means the code exists in the pinned submodule and is used unmodified.

| Feature in `serve/` | Disposition | Source |
| --- | --- | --- |
| `POST /v1/chat/completions`, stream and not; messages with system/user/assistant/tool, `reasoning_content` in and out | llama.cpp has it | `tools/server/server-common.cpp:1151-1425` parse, `server-task.cpp` results, `format_oai_sse` |
| Chat template from the GGUF, `chat_template_kwargs`, `enable_thinking`, `reasoning_effort`, `preserve_thinking` | llama.cpp has it | `common/chat.cpp` `common_chat_templates_init/apply` on a vocab-only model |
| Tokenize and detokenize | llama.cpp has it | `llama_vocab` from the GGUF, `vocab_only=true` |
| Reasoning and tool-call parsing, incremental, per family (Qwen, GLM, Gemma 4, DeepSeek, LFM2) | llama.cpp has it | `common_chat_parse` PEG autoparser plus 15 hand-written parsers, `common_chat_msg_diff` |
| Streaming deltas for content, reasoning and partial tool-call arguments | llama.cpp has it | `task_result_state::update_chat_msg`, `server-task.cpp:170-177` |
| SSE framing, `[DONE]`, keepalive pings, client-disconnect cancel | llama.cpp has it | `server-http.cpp:544-583`, `sse_ping_interval`, `is_connection_closed` |
| `max_tokens` clamp, context overflow 400 | llama.cpp has it; we add the `strata` engine's 8-token reserve | old fork `main.cpp:345-362` (`kEngineReserve = 8`, REGINA-112) |
| Sampling: temperature, top_p, top_k, min_p, seed, penalties, `logit_bias`, `stop` | we write: map request fields to `GEN` keys (~80 lines); stop strings via server's `process_token` logic | engine keys at `generate.cpp:11626-11682`, `src/poly/main.cpp:561-572` |
| Model-card default sampling per family when the client sends none | we write: CLI defaults like `llama-server` (`--temp` etc.), ~20 lines. pi always sends them | `serve/server.py:7380` |
| `tool_choice=required` or a named tool | best effort: the template emits the opener grammar, the engine cannot enforce it. Same as upstream, which writes the opener into the prompt. We log a parse failure with raw output | see §5 |
| `response_format` json_object / json_schema | **[D]** validate-only like upstream (~40 lines), or drop. Recommendation: drop in v1, revisit if a client needs it | `serve/structured.py` |
| Images: `image_url` base64, multiple per message | llama.cpp has it: `mtmd` in-process, CPU encode by default, GPU by flag. We write the splice of pad ids and the `SVE1` file (~150 lines, old fork `vision.hpp`) | `tools/mtmd`, `generate.cpp:10558-10562`, `src/poly/main.cpp:211-222` |
| Images from `http://` URLs | drop (rule 1: network at request time) | `serve/server.py:2451-2466` |
| Prompt cache, conversation checkpoints | engine-side; we send `turn_at=` for poly and nothing else | `serve/server.py:1272` |
| `GET /v1/models`, `/health` | llama.cpp has it; `/health` adds the engine's child state | |
| `GET /metrics` Prometheus | llama.cpp has it (`server-task` metrics) if it falls out of design C; otherwise drop | `serve/prometheus.py` |
| `GET /props`, `/slots`, `/v1/status` | drop `/slots` and `/v1/status`; `/props` only if free | |
| `POST /v1/messages` (Anthropic), `/v1/responses` | **[D]** llama.cpp has the conversions (`server-chat.cpp`). Keep only if they compile with zero extra code, marked untested. pi uses chat completions | |
| `/v1/messages/count_tokens` | drop | |
| MCP hub, `strata_mcp` server-side tool execution | drop (rule 1: outbound HTTP at start; pi runs its own tools) | `serve/mcp.py` |
| Web UI, API monitor, `/settings`, `/config`, telemetry sampler | drop (rule 4 and not needed; llama.cpp's UI downloads assets at build unless `LLAMA_USE_PREBUILT_UI=OFF`) | `serve/web/`, `serve/telemetry.py` |
| Image generation via `sd-cli` | drop (rule 1: downloads stable-diffusion.cpp) | `serve/images.py`, `tools/image/` |
| Replicas, CPU affinity, `--batch` slot scheduling (`BGEN`) | drop in v1. One request at a time, queued. Engine `--batch` stays in the tree, "present, unmeasured" | `serve/replicas.py`, `server.py:1572-1900` |
| Model switching, load/unload, `VRAM` resize | drop in v1: one model per process, one systemd unit per model | `server.py` `Service.switch_model` |
| `SAVE`/`RESTORE` sessions | drop in v1; engine keeps the feature | |
| API key, Host and Origin checks | llama.cpp has `--api-key`; bind `127.0.0.1` by default | `server-http.cpp` middleware |
| Reasoning budget, wrap-up injection, loop recovery, `tool_call_recovery`, `drop_oldest`, `env_to_user`, `warm_start`, `strata_prefix`, `strata_tune`, speed projection cvec | drop (heuristics and extensions; need logits or are upstream-only) | `server.py:4584-4932` |
| `keepawake`, `winjob`, `cache_drop`, `session_save_retry` | drop (Windows or niceties) | |
| `n`, `logprobs`, `stream_options` | not in upstream either; drop | |

New C++ we write regardless of design (§5): the engine adapter (spawn, `READY`/`INFO`, `GEN`/`GENI`
write, `T`/`PP`/`RESUME`/`DONE`/`ERR`/`FATAL` read, `STOP` plus drain, watchdog-aware supervision),
about 400 lines, with the old fork's `engine.hpp` and its three fake-engine tests as the reference.

## 4. Inventory of `tools/` and installers

The serving path touches nothing in `tools/` at request time except, on AMD, the 15 hipBLASLt and
rocBLAS tuning tables in `tools/hip/*.txt` which the engine reads through env vars. Those stay where
they are.

Offline tools that stay (rule: Python that runs once to produce model files is allowed):

| Group | Files | Network |
| --- | --- | --- |
| Flash-Next pack chain | `iq_pack.py`, `strata_tokenizer.py`, `gguf_reader.py`, `strata_pack.py`, `canonical_xcheck.py`, `pack_layer.py`, `pack_index.py`, `_paths.py`, `gguf_writer.py` | none |
| MTP draft head | `mtp_fetch.py` (HF range reads), `mtp_pack.py`, `mtp_rt.py`, `draft_vocab.py`, `glm_draft_fetch.py` (HF) | the two fetchers, by design; they run once |
| Quantization, NVFP4, inspection | `nvfp4_*.py`, `embd_bf16_pack.py`, `ple_fp8_pack.py`, `quant_*.py`, `quantscope.py`, `strata_inspect.py`, `iq_fixture.py` | HF API only when given a repo name |
| Profiling and calibration | `make_profile*.py`, `collect_profile_traces.py`, `bench_eviction.py`, `replay_cache_policy.py`, `check_isa_guard.py` | none |
| Benchmarks and engine gates | `strata_bench.py`, `ab_engine.py`, `agent_turns_bench.py`, `*_test.py` engine probes, `qwen35/` parity gates | localhost |
| `calibrate.py` | imports `setup` and `serve.server`; stays in the tree but cannot run after the cut. Documented as such | |
| Vision encoder source | `tools/vision/` (C++, mtmd). Stays unbuilt; the front end encodes in-process | |

The pack chain, as `setup.py:7851-7897` runs it, becomes a documented manual procedure in `README.md`:

```
python tools/iq_pack.py --gguf <shard1> --out <pack>            # add --compat-bf16 for Unsloth
STRATA_SOURCE=huggingface python tools/mtp_fetch.py fetch --out <mtp>
python tools/mtp_pack.py --src <mtp> --experts q2_0 --out <mtp>/mtp-q2_0.gguf
python tools/mtp_rt.py --gguf <mtp>/mtp-q2_0.gguf --out <mtp>/rt
cp data/draft_vocab.bin <mtp>/rt/draft_vocab.bin
```

`mtp_pack.py` and `mtp_rt.py` need llama.cpp's `gguf-py`; `tools/_paths.py:16` already looks in
`third_party/llama.cpp/gguf-py`, so the submodule satisfies it. Python deps for the tools: numpy and
regex. `requirements.txt` shrinks to those two and moves to `tools/requirements.txt`.

`setup.py` jobs that are not downloads and must become documented flags (`setup.py` lines):
GPU and compute-capability detection (1305-1437), CPU ISA floor and `--pool-workers` (1099-1304),
RAM mode and KV streaming (4455-4544, 7949-7995), context and KV type and rope scaling (7589-7650,
5882-5917), the cmake flag set (4397-4403), engine argument assembly (7910-8060), run-config JSON
(8077-8160), draft vocab copy (5759), HIP table pick (3395). The fork's `README.md` gets a table of
these as explicit engine flags per model, with the values setup would have chosen for a 24 GB card.

## 5. Front end: three designs, one recommendation

The engine returns sampled tokens, not logits. That fact rules every design: grammar-constrained
decoding (`json_schema`, `tool_choice=required`, reasoning budget) cannot be enforced without an
engine change. Upstream has the same limit (`v1_status` advertises `constrained_decoding: false`).
We accept it, as upstream does, and log raw model output whenever a tool call fails to parse.

**A. llama.cpp `tools/server` with the Strata engine behind its slot loop.** `server-context.cpp`
is 5,558 lines; about 2,500 of them assume one `llama_batch` across slots, per-slot logit indices,
`common_sampler` reading logits through `llama_context`, and `llama_memory_seq_*` for cache and
checkpoints (`server-context.cpp:111-237, 2857-4024`). Substituting an engine that batches,
caches, samples and drafts internally is a rewrite of that file, not a patch. Every pin bump
re-ports it. Rejected.

**B. New main on `common` + `mtmd` + `httplib`.** What the old fork did: 1,269 lines, proven under
pi for a week, zero llama.cpp patches beyond the parser fix. Cost: hand-written SSE, disconnect,
keepalive, OpenAI JSON shapes, request parsing, delta diffs. Seven of its fourteen production bugs
were in exactly that hand-written layer (streaming held back content, silent wire during tool
calls, no `max_tokens` clamp, pipe drain, control-token tokenization).

**C. Hybrid: compile the engine-agnostic server files unmodified, replace `server-context.cpp`.**
From the submodule, as-is: `server-http` (838), `server-queue` (621), `server-task` (1,901),
`server-common` (1,986), `server-chat` (692), `server-schema` (662), `server-stream` (668). New:
`frontend/strata-context.cpp` implementing the queue callbacks and the routes we keep, the engine
adapter, a vision module, `main.cpp`, and a 20-line stub for `llama_ui_*` so `server-http` links
without the UI target. Estimated 2,000 to 3,000 new lines. Patches to llama.cpp: 0 to 30 lines
(five `server-common` signatures take `llama_context*` only to reach the vocab; we either avoid
those five or add vocab overloads). Compile-time seam when the pin moves: about six types.

**Recommendation: C**, because it is the ethos of this fork applied to the front end. SSE,
disconnect handling, pings, request schema, result formatting, incremental tool-call deltas and
Prometheus metrics come from llama.cpp unchanged, and the bugs the old fork spent a week on are in
code we did not write. The risk is the undocumented internal API of `tools/server`; the pin is
fixed, so that risk is paid once. Gate: step 4 must have `/v1/models` and a non-streaming chat
completion working against a fake engine, with the seven server files compiling unmodified, within
the first front-end commit. If that needs more than 30 patch lines to llama.cpp, fall back to B
with the old fork's `main.cpp` as the starting point. **[D]**

Facts that shape the implementation either way:

- The vocab-only `llama_model` has no hparams (`src/llama-model.cpp:1250`), so `n_embd`, rope type
  and context length come from the GGUF through `gguf_*` or Strata's own reader. The old fork did
  this for the projector width check (`vision.hpp:72-87`).
- `mtmd` on a vocab-only model misclassifies M-RoPE position type. We only take embeddings
  (`mtmd_encode_chunk`, `mtmd_get_output_embd`); the engine computes positions itself, so this does
  not matter.
- `setup.py` builds `strata-vision` from llama.cpp 69f201a2, not the engine pin, for MiniCPM-V
  (`setup.py:186-189`). We use one pin. MiniCPM-V images are "present, unmeasured, possibly
  unsupported at 3cf03257" until checked.
- The parser patch `qwen3-coder any argument order` from the old fork (`patches/`) is still needed
  at this pin: Qwen3.6 and Qwen3.8 route to that parser and the upstream grammar silently drops a
  call whose optional argument comes first. Applied once, at configure time, idempotently (the old
  fork had to apply it in two places; we apply it in one). (2026-10-19: the patch and its DeepSeek/GLM counterpart are commits on robertkarl/llama.cpp, branch `UnbiasedStrata`, which the submodule pins; nothing is applied at configure any more.)
- Prompt caching stays in the engine. The front end sends the full id list every request.
- Encode images on CPU by default to leave VRAM for the expert cache; `--vision-gpu` to move it.

## 6. Engines and models

Keep both engines in the build. **[D]**

| Engine | Model | Status at the end of this plan |
| --- | --- | --- |
| `strata` | Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS, MTP spec 4, 24 GB card | measured against upstream, same pod |
| `strata-poly` | Qwen3.6-35B-A3B Unsloth UD-Q3_K_M (17.1 GB), draft block in the file | measured against upstream, same pod |
| `strata-poly` | GLM 5.3 Flash, DeepSeek V4 Flash, LFM2.5, Ornith, Gemma 4, Tiel Coder, MiniCPM-V | present, unmeasured |
| `strata` | other Flash-Next quants, Coder, Unsloth NVFP4 | present, unmeasured |

Images: Qwen3.6 verified; Gemma 4 and MiniCPM-V present, unmeasured.

**One binary.** Both engines and the front end in one executable, dispatched on a hidden `--engine
strata|poly` first argument, forked by the front end before any thread exists (the old fork's
pattern, `main.cpp:742-765`). Two engine `main()`s are renamed without touching engine source, by a
wrapper translation unit that defines `main` away and includes the engine's file (old fork
`engine_entry.cpp`). Costs that are build-only: `strata_mmq` and `strata_poly_mmq` both compile
`quantize.cu` and `ggml_cuda_host.cu` and must merge into one MMQ library with the union of the
per-type instance files (`CMakeLists.txt:1577, 2035`); `strata_kq` is a shared object dlopened from
the exe's directory (`src/prefill/mmq_kq_loader.cpp:43-60`). Upstream made it a `.so` on purpose
(`CMakeLists.txt:1666-1669`): it is an optional plugin carrying its own copy of `ggml_cuda_host.cu`
and the static CUDA runtime, and without the file K-quant experts fall back to the FP16 prompt
path. Qwen3.6 UD-Q3_K_M is K-quant, so we need it. Linking it in means removing that duplicate
copy and the dlopen seam (`mmq_kq_lib.cu`, `mmq_kq_loader.cpp`): an engine build change with a
measurable gate (PP on a K-quant model equal before and after). Risk: non-`main` symbol clashes
between `generate.cpp` and `src/poly/main.cpp`. Fallbacks, in order: one binary plus
`libstrata_kq.so` beside it; three executables in one `build/bin/`, copied as a directory. **[D]**

## 7. Build layout

```
Makefile                  make build | make test | make bench ; CUDA_ARCHS=, PORTABLE=, JOBS=
CMakeLists.txt            upstream's, plus: submodule default, front end, merged MMQ lib
.gitmodules               third_party/llama.cpp -> robertkarl/llama.cpp branch UnbiasedStrata (3cf03257 + parser commits), shallow
                          (patches/ applied at configure until 2026-10-19; now commits on that branch, see UPSTREAM.md)
frontend/                 strata-context.cpp, engine.hpp, vision.hpp, ui_stub.cpp, main.cpp, tests
src/ include/             upstream engine, unchanged except what UPSTREAM.md lists
tools/                    upstream's offline Python, minus installers (§8)
data/                     upstream's profiles and draft vocabs (runtime data, kept)
docs/                     upstream's engineering docs (kept), minus marketing (§8)
bench/                    bench_vs_llama.py + corpus, kernel probes, correctness/ harness (2026-10-19: per-run records and
                          upstream's results/ removed from the tree, in history at 7551f138)
sycl/ metal/ tests/       kept, unbuilt
README.md UPSTREAM.md PLAN.md LICENSE
```

CMake changes to upstream, each one hunk, recorded in `UPSTREAM.md`:

1. `STRATA_GGML_DIR` default becomes `third_party/llama.cpp`; `FATAL_ERROR` with the submodule
   command if `ggml/CMakeLists.txt` is missing; the `FetchContent` block is deleted
   (`CMakeLists.txt:1416, 1456-1469`). `FETCHCONTENT_FULLY_DISCONNECTED ON` as a belt.
2. `.gitignore:31` stops ignoring `/third_party/llama.cpp*`.
3. A configure-time `execute_process(git apply --check ...)` step for `patches/`, idempotent.
4. A second `add_subdirectory(third_party/llama.cpp)` with `LLAMA_BUILD_COMMON=ON`,
   `LLAMA_BUILD_MTMD=ON`, `LLAMA_BUILD_SERVER=OFF`, `LLAMA_BUILD_TOOLS/TESTS/EXAMPLES=OFF`,
   `LLAMA_USE_PREBUILT_UI=OFF`, `LLAMA_OPENSSL=OFF`, `LLAMA_LLGUIDANCE=OFF`. `LLAMA_CURL` is already a
   no-op at this pin. The `tools/server/*.cpp` files we reuse are listed as sources of our own
   static library pointing into the submodule.
5. The one-binary merge (§6).

`make build` runs one configure and builds only the targets we ship: not `all`, which includes
about 70 parity and bench executables that are not test-gated (`CMakeLists.txt:850-1256`).

Defaults: `-DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_TESTS=OFF -DSTRATA_MMQ_KQUANTS=ON
-DSTRATA_Q6K_EXPERTS=ON`, `CMAKE_CUDA_ARCHITECTURES` from `CUDA_ARCHS`, default `61;75;86;89`, with
`STRATA_EXPERIMENTAL_SM60=ON` added whenever an arch below 75 is listed (upstream refuses sm_61
without it, `CMakeLists.txt:190-196`). Upstream's default is `120` alone, which needs CUDA 13.
`GGML_NATIVE=ON` means `-march=native` for the CPU expert kernels: build where you run, or
`PORTABLE=1` for an AVX2 baseline. **[D]** on the arch list: each arch adds minutes of nvcc time.

Requirements on the build box, same as llama.cpp plus: CMake 3.24 or newer (Ubuntu 22.04 ships
3.22), a C++20 compiler, CUDA 12.x toolkit, driver. No Python, no git access after
`git submodule update --init`.

## 8. Cut list

Each entry names the rule from the brief: (1) network at setup, build or start; (2) Python between a
request and a token; (3) breaks or complicates the one-command build; (4) marketing.

| Path | Size | Rule |
| --- | --- | --- |
| `setup.py`, `setup.sh`, `SETUP.bat`, `START-HERE.bat`, `UPDATE.bat`, `update.sh` | 530 KB | 1 |
| `serve/` (92 files; server, three API dialects, MCP, UI, telemetry, tests) | 2.0 MB, 33.9k lines | 2 |
| `polystrata.py` (imports `serve/` and `setup`) | 12 KB | 2 |
| `Dockerfile`, `docker-entrypoint.sh`, `flake.nix`, `flake.lock`, `.dockerignore` | | 1 |
| `requirements.txt` (jinja2, pillow, psutil, requests, cmake, ninja...) | | 1; replaced by `tools/requirements.txt` with numpy and regex |
| `tools/strata_mcp.py` (installer driven over MCP) and its test | 1,885 lines | 1 |
| `tools/image/` (stable-diffusion.cpp, Vulkan SDK, HF downloads) | | 1 |
| `tools/hip/package_windows.py` (AMD nightly wheels) | | 1 |
| `tools/test_setup_*.py` and the two golden JSONs (test `setup.py`) | 44 files | 1, follows `setup.py` |
| `tools/test_strata_mcp.py`, `test_qimage.py`, `test_docker_entrypoint.py`, `test_poly_prebuilt.py`, `test_chat.py` | | follow their subjects |
| `README.{de,es,fr,ja,pt-BR,zh-CN}.md` | 383 KB | 4 |
| `docs/media/` (9.6 MB webp demo, 1.8 MB gif, figures) | 12 MB | 4 |
| `.github/` (issue and PR templates), `SECURITY.md`, `AGENTS.md` | | 4 |
| `docs/INSTALL.md`, `AI_SETUP.md`, `MCP_SERVER.md`, `IMAGES.md`, `LLAMA_SWAP.md`, `COMMUNITY_BENCHMARKS.md`, `FAQ.md` | | **[D]** documents only cut components; recommend cut, rest of `docs/` stays |
| `README.md` | 57 KB | rewritten (§10) |

Kept although large and unbuilt, per the brief: `sycl/` (7.8 MB), `metal/` (1.2 MB), `tests/`,
`bench/` (23 MB of upstream results and the profile corpus; the results went 2026-10-19, see UPSTREAM.md), `docs/` engineering pages (`DETAILS.md`,
`BACKENDS.md`, `QUANTS.md`, `MULTI_GPU.md`, `AMD_HIP.md`, `INTEL.md`, ...), `data/`, `ref/`,
`third_party/ggml/` (upstream's `ggml-common.h` copy), `tools/vision/`, `chat.py` (stdlib client,
depends on nothing), `tools/make_poly_release.py`, `tools/calibrate.py` (inert after the cut).

Expected diff shape: removals dominated by `setup.py`, `serve/`, translations and media; `src/` and
`include/` untouched at import plus whatever `UPSTREAM.md` lists.

## 9. Steps, each a commit, each gated

| # | Step | Gate (what is verified before the commit) |
| --- | --- | --- |
| 0 | Decisions in this file signed off | you |
| 1 | Import `PolyStrata-v0.1.45-poly.1/` as the root commit, byte for byte | `diff -r` against the checkout is empty; `git log` shows one commit |
| 2 | Submodule at `third_party/llama.cpp` @ 3cf03257 shallow; CMake hunks 1 to 4; `Makefile`; `patches/` with the parser patch; `UPSTREAM.md` opened | `make build` on a Linux CUDA box with networking disabled (`unshare -n` or firewall) produces `strata`, `strata-poly`; `strace -f -e trace=network` shows no connects |
| 3 | Cuts (§8), one commit per rule group, message cites the rule | `make build` still passes offline |
| 4 | Front end skeleton (design C): server files from the submodule, `strata-context.cpp`, engine adapter, fake engine | `make test`: fake-engine tests (drain on error, cancel, backpressure, child exit diagnosis, ported from the old fork); `/v1/models`; non-streaming chat against the fake engine. Decision gate for C vs B |
| 5 | One binary (§6) | the single executable runs `--engine strata --version` and `--engine poly --version`; still offline |
| 6 | First end-to-end chat, both engines | Qwen3.6 UD-Q3_K_M from GGUF; Flash-Next IQ3_XXS from the pod's existing pack and MTP dir. Streaming and non-streaming, reasoning on and off, greedy output equal to upstream PolyStrata on the same ids (teacher-forced id comparison, not text) |
| 7 | Tool calls under pi | pi completes a multi-step coding task against each model. Test the request shapes pi sends: no `max_tokens`, `max_tokens` 32768, optional arguments first, `reasoning_content` on assistant messages, parallel calls, a client that disconnects mid-stream, a stream silent during a long prompt. Raw output logged on parse failure |
| 8 | Images | a screenshot through pi to Qwen3.6; projector width checked against the model | Done 2026-10-18 on the A6000 VM (`bench/unbiased/2026-10-18-a6000-qwen36-iq4xs-images-step8/`): six facts read off a 1920x1080 screenshot, by the API and through pi; width check at start. Encoder on the CPU only (15 s a picture at the 300-row default on a Ryzen 7 1700); giving it the card is a build change, open. |
| 9 | Measurements (§11) | tables under `bench/unbiased/` with commands, raw outputs, checksums of the model files | Rows 1-2 done 2026-10-18 (`bench/unbiased/2026-10-17-3090pod-step9/`); row 3 done (within noise per token); one cell to explain (Qwen3.6 16K prefill 2.3 % slower). |
| 10 | `README.md`, `UPSTREAM.md` complete | every `src/`/`include/` hunk since the import appears in `UPSTREAM.md`; README has only numbers that have rows | Done 2026-10-18: README rewritten (build, run lines, pack chain, measured table from step 9 and 6 records only, limits, cut rule); UPSTREAM.md gains the front-end behaviour list; `src/`/`include/` diff since import is empty. |
| 11 | **Correctness plan** (added 2026-10-17). An agent writes `docs/CORRECTNESS-PLAN.md`: how to check PolyStrata's engines against llama.cpp at the pinned commit for the kernels actually in use on our target (the expert MMQ paths for IQ3_XXS and the K-quants, the MTP draft layer, the Q8/int8 KV paths, the prompt path, `strata-poly`'s qwen35moe path), which of upstream's ~70 parity and bench programs cover them and what each compares against, the teacher-forced greedy id comparison against llama.cpp for Flash-Next and Qwen3.6 (same ids in, how many tokens, what divergence is tolerated and why), what a failure would mean for the fork, and the cost in machine time. Planning only: this step runs nothing on a GPU and changes no code | the document, with each proposed check naming the program or script, the reference it compares against and the pass criterion; your sign-off on which checks become a later step |
| 12 | **Correctness checks** (added 2026-10-17). Run the checks step 11 proposed and you signed off: upstream's parity programs that cover the kernels in use, and the teacher-forced greedy id comparison of each engine against llama.cpp at the pinned commit for Flash-Next IQ3_XXS and Qwen3.6 UD-Q3_K_M. Results, commands and raw outputs under `bench/unbiased/correctness/`; a divergence is a bug to find, in the engine or in llama.cpp, before any README claim beyond "same output as upstream PolyStrata" | every signed-off check has a result on disk with its pass criterion met, or an open finding with the first diverging token, the inputs that reproduce it and which side is wrong; `docs/MODEL-FILES.md` says per file whether it passed | Done 2026-10-18 on the A6000 VM (`bench/unbiased/correctness/`): 38 parity programs pass, `poly_expert_parity` fails its own bound on one UD-Q3_K_M expert (USTRAT-1); teacher-forced distributions of both engines on 16 sequences per model are within llama.cpp's own CPU-vs-GPU spread on every set and arm (T2); the greedy token agrees at every 0.5-nat-margin position but a handful of near ties per arm (USTRAT-3, USTRAT-5); drafting is not bitwise inert above 4-token windows (USTRAT-2); `strata`'s bits depend on the free VRAM at start unless the expert cache is pinned (USTRAT-4); `check-batch 4` passes. `docs/MODEL-FILES.md` has the per-file column. Not covered on 48 GB: the 24 GB tier pattern, a full-GPU Flash-Next reference. **Extended 2026-10-19 to DeepSeek V4 Flash UD-IQ2_XXS and GLM 5.3 Flash 3.0bit** on a 2x RTX PRO 6000 pod (the 2x B200 run was lost with the pod): T2 passes every set and arm (DeepSeek 0.69-1.06x, GLM 0.89-1.13x the floor); T1 flips at 3 (DeepSeek) and 5 (GLM) margin positions where both llama.cpp runs agree, T2 holding (USTRAT-10); the GLM draft arm is not bitwise the base arm because the draft block's VRAM moves 52 experts to the CPU (USTRAT-9); DeepSeek's is, on 15 of 16. GLM's reference is llama.cpp master 99db719b (the pinned tree lacks `glm5-next`); a master-vs-pinned CPU probe on DeepSeek measures the reference's own drift (`bench/unbiased/correctness/README.md` section 8). `--kv q8_0` is qwen35moe-only, not run. |
| 13 | **Model acceptance suite** (added 2026-10-17; scope cut 2026-10-19 after step 12: the model math is proven within llama.cpp's own spread, so this step checks what teacher-forced greedy cannot: the sampled path end to end and a coding agent doing real work against each model family). (a) For every model family the fork serves, on a box that holds it: Qwen3.8-Flash-Next IQ3_XXS (RTX 3090 pod), Qwen3.6-35B-A3B UD-Q3_K_M and UD-IQ4_XS (3090 pod, A6000 VM), DeepSeek V4 Flash UD-IQ2_XXS and GLM 5.3 Flash 3.0bit (the 2x B200 pod: first bring-up of both on this fork, `strata-poly`, `src/poly/deepseek4.cpp`, upstream's docs/DEEPSEEK.md and docs/GLM.md run lines). (b) The task: pi, with its tools, in a fresh sandbox directory on the pod, against the fork's server, told to write a minesweeper clone in html/js/css and make it run; sampled, not greedy (the model card's defaults, fixed seed; a second run at temperature 1.0). The page it leaves is rendered headless (lookingglass) and must load without a console error and expose a clickable grid; pi's transcript, the server log, token counts and wall time are the record. (c) The API-only variant of the same prompt for the row's decode and prompt tok/s against the step 6/9 numbers for that card, a 15 % band. One script, `frontend/tests/acceptance-real-engine.sh`, the same for every family; the pod setup downloads every file fresh from Hugging Face onto the container disk (the RunPod network volume is too slow to serve from), `tools/pod/bringup.sh`. Dropped from the 2026-10-17 stub: models whose files we do not have beyond those four families, judging the page beyond render and a click, and the first-run-sets-the-range machinery (step 14 owns performance) | per family: loaded, pi produced a page that renders and clicks, decode and PP inside the band; DeepSeek V4 Flash and GLM 5.3 Flash rows added to `docs/MODEL-FILES.md` with hashes and the box **Done 2026-10-19** (fork commits a8469d55, 3a37ac63 and the closing commit; record `bench/unbiased/2026-10-19-step13-acceptance/`, harness `tools/acceptance/`, API script `frontend/tests/acceptance-real-engine.sh`). Per model family, pi 0.85.1 in Docker with tools, sampled as the model card asks, four work items (md-toc CLI, HTTP KV store, three planted bugs, Minesweeper; Minesweeper again at temperature 1.0): Flash-Next IQ3_XXS and Qwen3.6 UD-Q3_K_M on the RTX 3090 pod 5 of 5 each; Qwen3.6 UD-IQ4_XS on the A6000 VM 5 of 6 (the temperature-1.0 page throws a ReferenceError of its own); DeepSeek V4 Flash UD-IQ2_XXS and GLM 5.3 Flash 3.0bit on the 2x B200 pod 5 of 5 each after two fork fixes found by this step: GLM's architecture name refused by the vocabulary load (USTRAT-6, `frontend/strata_context.cpp`) and tool-call arguments out of schema order rejected by llama.cpp's grammars because the engine samples unconstrained (USTRAT-7, `patches/llama.cpp-any-argument-order.patch`); GLM also needs `reasoning_effort` low or high (its template treats anything else as max). API-only checks: thinking split, seeded repeat, tool call, decode and prompt speed within 15 % of step 9 on the 3090 (Flash-Next only with `--pool-workers 15`; 24 is 14 % slower), first figures on the B200 (DeepSeek 72 tok/s, GLM 74 tok/s decode on one card), A6000 IQ4_XS prose decode 175 tok/s recorded as its own baseline. Both pods downloaded every file fresh from HF (`tools/pod/bringup.sh`). |
| 14 | **Performance parity protocol** (added 2026-10-18; the ship bar is "within 5 % of upstream on decode and prompt processing", every model, every target card). Builds on step 9. (a) Same engine binary on both sides first: the fork front end driving upstream's engine binaries (the `forkfe-upengine-*` arms of 2026-10-18) separates the front end from the engine build; then both engines built with the same compiler, nvcc, flags and `-march` recorded, so the two binaries differ in nothing but the tree they were built in. (b) Workloads, each 5 runs, median and range, greedy: short chat decode; sustained 512-token decode; prefill 4K / 16K / 32K and the model's maximum context; agent turns with growing history, with the reused-token counts recorded (a front end that renders the template differently breaks the prompt cache, and that shows as `cache_n`, not as tok/s); time to first token; inter-token latency p50 and p99 from the client's timestamps; client-side tok/s next to engine-side tok/s (the SSE and JSON cost of each front end); cold start to READY and warm start. (c) Every file in `docs/MODEL-FILES.md`, on each target card (Pascal 1080 Ti, RTX 3090, A6000), upstream and fork back to back. (d) A 1-hour soak of mixed request shapes with RSS and VRAM sampled every minute. Pass: fork median within 5 % of upstream on every (model, card, workload) cell, or the ranges overlap; `cache_n` equal per turn; RSS and VRAM flat. Any cell outside is a finding with the arm directories that reproduce it | tables under `bench/unbiased/parity/<date>-<card>/` with provenance (engine sha256, compiler, flags) per arm; every cell filled or marked blocked with the reason |
| 15 | **Accuracy protocol against llama.cpp** (added 2026-10-18; extends step 12 with pass criteria). The reference is llama.cpp at the pinned commit, CUDA build, on the same GGUF. (a) Noise floor first: llama.cpp CUDA versus llama.cpp CPU on the same model and ids, per-position logits: max abs difference, KL(ref||other) per position, top-1 agreement. That floor is the tolerance, as the user put it: "as close as llama.cpp's GPU and CPU kernels are to each other". (b) Kernel level: upstream's parity programs that cover the kernels in use (step 11's list) at the pinned commit on each target arch, all green; the per-kernel error against the ggml reference recorded, not only pass/fail; target 1e-5 relative where the kernel is exact-arithmetic (norms, rope, residual), and the quant kernels within the floor from (a). (c) Logit level, teacher-forced: the engine's per-position logits along fixed ids (upstream's `--teacher-force` style diagnostics, `tools/canonical_xcheck.py`) against llama.cpp CUDA for Flash-Next IQ3_XXS and Qwen3.6 UD-Q3_K_M and UD-IQ4_XS: per-position KL median and p99, top-1 agreement rate, first position where the greedy argmax differs; 20 prompts of 512 to 4096 tokens including code and a tool-call transcript. Pass: median KL within 2x the floor from (a), top-1 agreement not below the floor's. (d) Perplexity on a fixed public text (a wikitext-2 slice, 64K tokens) engine versus llama.cpp CUDA: within 1 %. (e) Speculation is lossless: greedy ids with `--spec 0` equal to `--spec 4` and to the drafter off, for 20 prompts x 512 tokens; sampled with a fixed seed likewise. (f) Each speed knob upstream ships on by default that changes answers (`--tail-skip`, `STRATA_ROUTE_TAIL_SKIP`, int8 KV, `--spec-min-p`) measured off versus on with the (c) metrics, so the README states the accuracy cost of each default. (g) Long context: a needle test (`tools/needle_bench.py`) at 32K and the model's maximum, engine versus llama.cpp. Pass criteria written down before the runs | `bench/unbiased/correctness/<date>/` with the floor, every metric per model, the commands and the raw logits' checksums; `docs/MODEL-FILES.md` says per file: KL median and p99 against llama.cpp, top-1 agreement, PPL ratio |
| 16 | **Front-end equivalence to upstream's Python server** (added 2026-10-18; the part of the port that is not the engine). (a) Prompt rendering: a corpus of chat requests (system prompt, multi-turn, tools with optional arguments, tool results, `reasoning_content` on assistant turns, `enable_thinking` on and off, `preserve_thinking`, an image once step 8 lands) rendered by upstream's jinja2 path and by llama.cpp's template engine in the fork: the token id sequences must be identical, per model template. A difference changes what the model sees and breaks checkpoint reuse. (b) Output parsing: a corpus of raw model outputs (tool calls in both argument orders, parallel calls, reasoning blocks, truncated streams) parsed by upstream's per-family parsers and by llama.cpp's: same `tool_calls`, same `content`, same `reasoning_content`. (c) Streaming equals non-streaming: for the same seed, the concatenated deltas equal the non-streamed message, usage counts equal the engine's `DONE` counts. (d) API conformance: the HTTP-level tests of upstream's `serve/` (import commit, 58 files) that apply to the kept surface, ported to run against both servers; cancel and disconnect semantics (the engine is told to stop; the next request is clean) on both. (e) Task level: pi on 10 fixed coding tasks x 3 runs per server, pass rate and tokens per wall second; within noise | corpora and results under `bench/unbiased/frontend-equivalence/`; a rendering or parsing difference is a bug in the fork unless upstream is shown wrong, with the request that reproduces it |
| 17 | **Release gate** (added 2026-10-18). Steps 12 to 16 green or every open finding written in `README.md` with its size; the acceptance suite (13) run on every file on at least one card; `UPSTREAM.md` current; a tagged commit with the `bench/unbiased/` directories it was judged on | the tag; the README's measured table cites only those directories |
| 18 | **Multi-GPU** (added 2026-10-19, user priority after the correctness work). Upstream runs one model across several cards as a layer split (`docs/MULTI_GPU.md`): `strata` (Flash-Next) with `--layer-split`, `--split-device`, `--split-skip-if-fits`, the chunk pipeline, `--pipeline-windows` and the `STRATA_SPLIT_*` variables; `strata-poly` with `--layer-split auto|N[,N...]` across the cards `CUDA_VISIBLE_DEVICES` shows it (two RTX 3080 measured upstream for Qwen3.6, Gemma 4, GLM REAP50; two cards hold about twice the experts, decode and prefill gain follow from that). The engines' code is unchanged in this fork, so the capability is intact; what went with `setup.py` and `serve/` (UPSTREAM.md, Removed) is the glue: card detection and the `--gpus 0,1` choice, the run-config `"gpu"`/`"layer_split"` fields, putting the faster card last, `--calibrate` setting `--prefill-pipe`, and `tools/batch_test.py`'s config-driven `--layer-split`. Nothing in the fork has exercised a split yet; the first run is the 2x RTX PRO 6000 pod (DeepSeek V4 Flash UD-Q4_K_XL / UD-Q8_K_XL, `tools/pod/serve.sh deepseek-q4|deepseek-q8`, `--layer-split auto`). Work: (a) README Run section documents `CUDA_VISIBLE_DEVICES` plus `--layer-split` for both engines as the replacement for `--gpus`, including the faster-card-last rule; (b) the front end passes the environment through untouched and `/props` or the log says which cards and layers each stage took; (c) measured: one card vs two, same model and workloads as step 14, on the 2-card pod for strata-poly (DeepSeek, GLM) and, when a 2-card 24 GB-class box exists, Flash-Next with `strata`; (d) correctness: the step-12 arms (T1/T2 against llama.cpp, T0 base vs draft) on the split, since upstream notes the rounding changes with which card or the CPU computes an expert; (e) upstream's layer-split parity and gate programs that still build, run and recorded | `bench/unbiased/multi-gpu/<date>-<box>/` tables with the split chosen per cell and the engine log lines naming each card's layers; README section with the flags and the measured rows; any divergence a finding with the inputs that reproduce it |

Steps 14 to 17 (2026-10-18) are the ship bar for consumers of PolyStrata: as fast and as accurate as upstream within
5 % on decode and prompt processing, kernels within 1e-5 where exact and otherwise within the spread between
llama.cpp's own GPU and CPU kernels, logits close in KL, and the front end rendering and parsing exactly what the
Python server did. Step 8 (images) precedes them in order; step 5 stays on its fallback by decision.

Step 13 is the minimum "does it work" bar per model file: load, a sampled program that runs, speed inside a known
range. It is a sanity check, not a correctness proof: a kernel that is slightly wrong still writes a program that runs,
and only step 12's id comparison against llama.cpp shows the engine computes what it should. Both stay.
Step 11 is a plan, not a run, and step 12 runs what it proposes; they exist because the plan above inherits the engines' correctness from upstream without
checking it (TRELLO.md: "Vet polystrata fork on correctness"), and the step-6 gate as run compared text and token counts,
not ids. Steps 2, 6, 7, 8, 9 need a Linux box with an NVIDIA card. You hand me the machines; the RunPod pod
from the 2026-10-17 benchmark has the IQ3_XXS pack, the MTP dir and the upstream build. Nothing
runs on the homelab.

## 10. Documents

`README.md`: what it is (one paragraph), the two engines and the model table with measured versus
present-unmeasured, the exact build lines, the exact run line per measured model with every flag
`setup.py` would have chosen, the pack chain, the measured table, the cut list with rules, known
limits (no constrained decoding, one request at a time, CPU image encode time). No superlatives.

`UPSTREAM.md`: the upstream commit; the llama.cpp pin; each engine or build hunk with file, reason,
and the measurement that justified it; each commit on the llama.cpp fork's branch; the list of features dropped
relative to upstream with the rule. Updated in the same commit as any engine or build change.

## 11. Measurements

Same pod, same card, same model file (SHA-256 recorded), same context, same sampling, same client
script, upstream PolyStrata and this fork back to back, three runs each, median and range.

| Row | Model | Workloads |
| --- | --- | --- |
| 1 | Flash-Next IQ3_XXS, MTP spec 4, 64K ctx, int8 KV | short chat decode, 512-token sustained decode, PP at 4K/16K/32K, agent turns with growing history (the existing `bench/community.py` suite) |
| 2 | Qwen3.6-35B-A3B UD-Q3_K_M, draft on, 32K ctx | same |
| 3 | Row 1 with pi: wall time and tokens for a fixed coding task | one task, three runs |

Pass: fork within noise of upstream (the run-to-run range) on decode and PP. A fork slower than
upstream on any row is a bug to find before the README claims anything. The llama.cpp rows already
recorded stay in the table as context, labelled with their different quant.

Rows 1 and 2 measured 2026-10-18 UTC on the RTX 3090 pod: the step-9 record (in history at 7551f138, `bench/unbiased/2026-10-17-3090pod-step9/`).
Row 1 (Flash-Next): fork faster on every workload (chat decode 103.1 vs 87.1 tok/s, sustained 119.1 vs 105.1,
prefill 4K/16K/32K 2065/2531/2616 vs 1924/2342/2393). Row 2 (Qwen3.6 UD-Q3_K_M): decode within the
run-to-run range (chat 213.5 vs 220.8, sustained 200.0 vs 207.6, ranges overlap); prefill 4K 4.7 % faster,
prefill 16K 2.3 % slower with non-overlapping ranges (3486 vs 3570), which the pass rule counts as a finding
to explain. The engine binaries were built separately in the two trees (sha256 in each arm's provenance),
so an engine-build difference was suspected; the attribution arms of 07:04 UTC (fork front end driving
upstream's engine binaries) confirm it: with one engine binary, prefill is equal within 1 % and decode within the
run-to-run range on both models, so the front end passes and the differences are the engine builds (step 14a makes
them identical in flags). Two client-compatibility gaps found and fixed on the way
(`/health` fields, usage in streams; commits 453d3015, 96f4ef1f). Row 3 (pi, Flash-Next): all six runs complete the task; wall time per generated token within noise (fork 76.6 vs
upstream 72.1 tok/s median); raw wall medians 44.7 vs 36.3 s because the sampled trajectories differed in length.

## 12. Decisions, taken 2026-10-17

1. **Repository.** The fork continues the `UnbiasedStrata` repo (`~/Code/UnbiasedStrata`, remote
   `robertkarl.net:UnbiasedStrata`) in place. The present head is tagged first. Commit 1 removes every
   tracked file, `.gitignore`, `.gitmodules` and both submodules (an empty tree). Commit 2 imports
   PolyStrata byte for byte. The diff baseline is commit 2. `unbiasedstrata2/` stays as the notes and
   bench workspace.
2. **Design C** with the step-4 gate and B as fallback (§5).
3. **Three executables** in `build/bin/`: `strata`, `strata-poly`, the front end, plus
   `libstrata_kq.so` beside them. The directory is what gets copied. No MMQ merge, no symbol dedup.
   §6's one-binary paragraph is superseded.
4. **Drops:** everything in §3 marked drop goes, including structured output, the Anthropic and
   Responses dialects and Prometheus.
5. **Gray-zone cuts:** the seven installer docs, `chat.py` and `tools/calibrate.py` are cut.
6. **CUDA archs** default `61;75;86;89`.
7. **Measured models:** Flash-Next GSQ-RCO IQ3_XXS and Qwen3.6-35B-A3B UD-Q3_K_M (unchanged).
8. **Machines:** a homelab GPU box for the offline build proof only, without touching running
   services. Measurements wait for a pod you hand me; steps 6 to 9 are blocked until then.

No new third-party tool or dependency is introduced by this plan. Everything comes from the
upstream tree, the pinned llama.cpp submodule, CMake, a C++20 compiler and the CUDA toolkit.
