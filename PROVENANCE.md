# Provenance

What this tree is made of, pinned by commit, and how to check it yourself. This file is the audit
trail; keeping it accurate on every import is part of the work.

## Repository shape

This is a standalone repository, not a GitHub fork. The first commit is UnbiasedStrata imported
unmodified, and its tree hash is identical to upstream's:

| | |
|---|---|
| Import commit | `39cab36` — "Import UnbiasedStrata ff19037a (unmodified)" |
| Its tree hash | `5107565f1dd1cf695efd9bf178215838cc6e94f3`, the same as upstream's |

So **`git diff 39cab36 HEAD` is the complete and permanent record of what this fork changed**, and
`git diff 39cab36 HEAD -- src include` is every change to the engine. That property is the whole
reason for the repository's shape, and it is why upstream's file and directory names are kept
exactly as they are: renaming them would wreck every future merge, and cheap merges are the only
thing that makes this fork maintainable.

The upstreams are plain git remotes, so their history is fetchable without this repository being a
fork of any of them:

```
git remote add ubs    https://github.com/robertkarl/UnbiasedStrata.git
git remote add poly   https://github.com/simplexai-labs/PolyStrata.git
git remote add strata https://github.com/Niko1221/Strata.git
```

## Pins

| What | Source | Pin |
|---|---|---|
| Front end, the first deletions, this build system | `robertkarl/UnbiasedStrata` | `ff19037a` (`main` at 2026-11-06) |
| Base tree beneath it: engine, tools, docs | PolyStrata | tag `v0.1.45-poly.1`, commit `d0cd84dc6bd009d1d9e2191e4e28cc76b80f1ee3` |
| ggml, llama, common, mtmd, cpp-httplib | `robertkarl/llama.cpp`, branch `UnbiasedStrata` | submodule `third_party/llama.cpp` at `44072a19fb94d0bed63d381d42f6237373f91d54` |
| Upstream changes taken since the import | PolyStrata | tag `v0.1.45-poly.8`, commit `4de79331b49e553406860470fa3e96b5256e2e50` |
| Model weights | `ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF` | repo revision `ed59f92082b1e93c0e96d60a8b11aab089b52f09` |
| MTP draft tensors | `Qwen/Qwen3.8-Flash-Next` | 31 tensors read as byte ranges; the pin that is actually checkable is `mtp-manifest.json`, which carries a sha256 per tensor and which `tools/mtp_fetch.py verify` re-checks (exit 3 names any that fail) |

**On PolyStrata's URL.** Older documents in this family cite `VecSzn/PolyStrata` and newer ones
`simplexai-labs/PolyStrata`. Both resolve, and both return the same commit
`d0cd84dc6bd009d1d9e2191e4e28cc76b80f1ee3` for tag `v0.1.45-poly.1`, so one is a rename of the
other. The commit hash is what identifies the tree; the URL is a convenience.

All three parent projects are MIT licensed. The model files carry their own licences, which are not
MIT.

## Model files

Verified on the reference machine before first use. Check these before you point the engine at a file.

| File | sha256 |
|---|---|
| `Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf` | `219ea929900dfa9ef091f3aa473fdba6874b65fcb36526d7d851ac9e95856d15` |
| `Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf` | `316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113` |

The pack directory and the MTP runtime directory are **built locally** from those files by
`tools/iq_pack.py`, `tools/mtp_pack.py` and `tools/mtp_rt.py`. Upstream offers a prebuilt pack for
download; it was deliberately refused here, because an opaque prebuilt artifact is exactly what this
project exists to avoid.

## Why there is a llama.cpp fork at all

The submodule is not `ggml-org/llama.cpp`. It is `robertkarl/llama.cpp`, which carries six commits on
top of ggml-org `3cf03257f219afbe7334045ff7c6a06ac68c627d`, and all six are tool-call parser fixes.

They exist because **this engine samples unconstrained.** llama.cpp's parsers were written against a
grammar-constrained sampler, so output this engine legitimately produces was being rejected or
silently dropped. The same architectural fact is why `tool_choice=required` does not work here (see
the README's known limits).

| Commit | What it fixes |
|---|---|
| `9547d8d` | Qwen3-Coder tool calls with arguments in any order. Qwen3.8-Flash-Next often writes optional arguments first, and the call was dropped silently: `finish_reason=stop` with an empty answer |
| `a6de9af` | The same for every auto-generated PEG grammar. Well-formed calls were reported as "output does not match the expected peg-native format" |
| `1421694` | A partial re-parse that finds fewer tool calls no longer throws; the request used to die with a 500 mid-stream. A non-partial parse failure now returns the raw text as content instead of failing the request |
| `638a7e2` | Skip a stuttered `</parameter>` close tag, seen in 7 of 673 requests of one run |
| `4174b21` | Skip blank lines between arguments and before `</function>` / `</tool_call>` |
| `385445f` | Generalised: any close tag that is not `</function>` or `</tool_call>` is skipped between arguments |

`git -C third_party/llama.cpp diff 3cf03257 HEAD` is exactly these plus the commit below.

Until 2026-10-19 these were `patches/*.patch` that CMake applied to the submodule's working tree at
configure time. That left the submodule dirty after every build and meant the pinned commit did not
name the sources actually compiled. They are commits on the fork now, so **the pin names the sources
and `git status` is clean after a build** — a prerequisite for reproducible builds.

**One pinned commit is an orphan, knowingly.** The submodule's tip, `44072a1`, is
"common: DeepSeek V4.1 (deepseek41) chat template detection and DSML tool-call parsing", taken from
`ggml-org/llama.cpp` PR 30817 for the DeepSeek-V4.1 engine. That engine was removed, so the commit
serves no model this fork can run, and its parser is still compiled into the `aegis` binary by way
of `llama-common`. Re-pointing the submodule at `385445f` would remove it. That has not been done
because it changes the front end's parser library and needs its own build and test cycle; it is
recorded here rather than left to be discovered.

## Build provenance

- **ggml comes from the submodule, never a fetch.** `STRATA_GGML_DIR` defaults to
  `third_party/llama.cpp`, and configure stops and tells you to run `git submodule update --init` if
  `ggml/CMakeLists.txt` is missing. Upstream's `FetchContent` of llama.cpp — the only network access
  in its build — is gone.
- The submodule's own build-time fetch sites are off in this configuration -- the ones that matter
  forced off rather than merely defaulted off, as `THREAT_MODEL.md` sets out -- and
  the web UI is replaced by `frontend/ui_stub`. The evidence that none is reachable is that the whole
  build completes inside a network namespace holding only `lo`.
- This repository's CMake has one `execute_process` (a `python3 -c "import numpy"` capability probe)
  and no `ExternalProject`, `file(DOWNLOAD)`, `install()`, `add_custom_command` or `$ENV{}` read.

## What this fork removed

Each is one commit, each with a clean build and the test suite behind it.

| Removed | Lines |
|---|---|
| Intel Arc / SYCL backend | −154,045 |
| DeepSeek-V4.1 engine (`strata-dsv41`) | −33,719 |
| `strata-poly` and the eleven model families it served | −16,228 |
| Qwen3.6 engine path (`src/qwen35`) and its tooling | −7,026 |
| Documentation and data cruft | −23,005 |

What is kept: the `strata` engine for Qwen3.8-Flash-Next, the C++ front end, the Metal and AMD/ROCm
backends (unmodified and unbuilt here), the test suite, and the benchmark harness without its result
data.
