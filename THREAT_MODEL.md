# Threat model

This document says what aegis-strata protects, who it protects it from, which of its own invariants
actually hold today, and what it does not cover. None of its parent projects has a threat model, and
writing this one is a large part of why the fork exists.

Every claim here is meant to be checkable. Where something is intended rather than enforced, it says
so. Where something was measured, it says on what.

## What is being protected

| Asset | Why it matters |
|---|---|
| Prompts and completions | The engine sees everything the coding agent sends it: source code, whatever secrets are in the files the agent read, and the conversation history |
| The developer's filesystem | The server runs as your user. That user's SSH keys, agent transcripts, cloud mounts and SSH config — which names internal hosts — are all readable by it |
| The build host | A compromised build is arbitrary code execution as your user, on the machine where you write code |

Note what is *not* on that list: the model weights. They are public files with published hashes.

## Who is being defended against

| Actor | What they can do |
|---|---|
| A compromised upstream maintainer account | Publish a release, or push to a repository this fork imports from |
| A malicious or backdoored release binary | Nothing here, because nothing here runs a binary it did not compile. This actor is the reason for that rule |
| A tampered dependency | The llama.cpp submodule, a Python package, the CUDA toolkit |
| A malicious model file | GGUF and pack files are parsed before anything else happens; a parser bug is remote code execution with a file as the vector |
| A prompt-injected agent | The coding agent calling this server may itself be driven by untrusted content it read |
| A network-local attacker | Anyone who can reach the listening port |

Explicitly *not* defended against: anyone who already runs code as your user, and anyone with
physical access.

## Invariants, and whether they hold

These six are the properties the project holds itself to. Two of them are enforced and measured
today, one holds in part, two are unverified, and one is not achieved at all. Saying which is which
is the point of this section, and the status lines below are the answer.

### 1. The engine process makes no outbound network connection

**Status: enforced for the engine, and tested. Still incidental for the front end.**

Measured with `objdump -T` on this build:

- The front end (`aegis`) imports `socket`, `bind`, `listen` and `accept4`, which it needs because
  it serves HTTP, and also `connect` and `getaddrinfo`. Neither libcurl nor OpenSSL is linked and
  nothing in the tree calls either symbol.
- The engine (`strata`) imports **no network symbol at all**. Its own code cannot open a socket.
- But the libraries loaded into the engine process do. `libcudart.so.13`, `libcublas.so.13` and
  `libcublasLt.so.13` each import `socket`, `socketpair`, `connect`, `bind`, `listen`, `sendmsg`
  and `recvmsg`. `libcuda.so.1` imports all of those except `socketpair`, and also `execvp` and
  `posix_spawn`.
- **And so does `libstrata_kq.so`, which this project builds** — the second of the three artifacts
  `make build` produces. It is opened at run time rather than linked, so it does not appear in
  `ldd build/bin/strata`, and the symbols arrive with the statically linked CUDA runtime rather than
  from anything in `src/`. It is named here because an enumeration that quietly omits one of our own
  artifacts is the wrong kind of enumeration for this document.

So the engine process has the whole socket API, and a way to spawn a process, available to it — most
of it through closed-source vendor code, some of it through a library we build but whose network
symbols we did not write. No `getaddrinfo` appears in any of them, so there is no DNS resolution: an
egress attempt would have to be to a literal address.

**That is what the sandbox is for.** The front end spawns the engine as a child, so the parent
installs containment between `fork` and `exec` (`frontend/strata_sandbox.cpp`, `docs/SANDBOX.md`):

- a network namespace holding only `lo`, so there is no route out of the child at all;
- a seccomp filter that permits `socket()` and `socketpair()` only for `AF_UNIX`, which is CUDA's
  local IPC, and returns `EAFNOSUPPORT` for every other domain, present or future. `connect()` is
  deliberately *not* the control point: its argument is a `sockaddr *`, and seccomp cannot
  dereference a pointer, whereas `domain` is a scalar it can compare soundly;
- `KILL_PROCESS` for the syscalls that would undo either of those — `setns`, `unshare`,
  `open_by_handle_at`, `io_uring_*`, `ptrace` and the rest, listed with a reason each in
  `docs/SANDBOX.md`.

If it cannot be installed, the engine does not start. `/health` and `/props` report `enforced`, `enforced-no-netns`,
`unavailable` or `off`, so a running server can be checked rather than trusted.

**It is tested, and the test has been seen to fail.** `frontend/tests/sandbox_test.cpp` runs in
`make test` with no GPU and no model: it forks, applies the sandbox and re-execs itself as a probe.
It asserts that `socket()` **creation** fails for `AF_INET`, `AF_INET6` and `AF_NETLINK` and
succeeds for `AF_UNIX` — creation, not reachability, because `socket()` succeeds on an airgapped
box and asserting on `connect()` is how this test would pass for the wrong reason. One case reruns
every probe with the sandbox off and every result inverts, so the suite cannot pass vacuously.
Setting the filter's deny action to ALLOW produces exactly three failures; zeroing Landlock's
handled access rights fails thirteen assertions.

**What is still incidental is the front end.** `aegis` itself imports `connect` and `getaddrinfo`
and is not behind a filter of its own; a second filter installed in the parent after the listener
binds is designed in `docs/SANDBOX.md` §9 and not built. So invariant 1 holds structurally for the
process that runs the model, and by inspection only for the process that serves HTTP.

For the **front end** this remains a statement about what the code happens not to call. If you need
that process constrained too, run it in a network namespace or block it at the firewall; a filter
for it is designed in `docs/SANDBOX.md` and not built.

The *build's* half of this rule is in better shape: the full build was run inside
`bwrap --unshare-net`, in a namespace holding only `lo`, with egress checked dead first (TCP to a
public host refused, UDP to a public resolver unreachable). It succeeded. That is a measurement.

### 2. Nothing executes that was not compiled from source in this tree

**Status: holds. Measured.**

- No ELF, PE32 or Mach-O file anywhere in the source tree (`file` run over every path). The build
  directory holds what you just compiled, and nothing else does.
- This repository's CMake contains no `ExternalProject`, no `file(DOWNLOAD)`, no `install()`, no
  `add_custom_command` or `add_custom_target`, and no `$ENV{}` reads. One `execute_process` remains,
  a `python3 -c "import numpy"` capability probe.
- The `Makefile` is under a hundred lines. No `sudo`, no network, and it writes only the build directory plus one
  probe file in `/tmp`.
- The `third_party/llama.cpp` submodule carries build-time fetch sites of its own (BoringSSL,
  LibreSSL, the web UI archive, LLVM OpenMP, kleidiai, CCCL, and several backends). Be precise about
  how they are off, because the two cases differ: `LLAMA_CURL`, `LLAMA_OPENSSL`,
  `LLAMA_USE_PREBUILT_UI`, `LLAMA_LLGUIDANCE`, `GGML_OPENMP` and the CCCL fetch are **forced** off
  with `CACHE … FORCE`, so a command-line `-D` cannot re-enable them; the hexagon, zendnn, virtgpu
  and vulkan backends are merely **off by default**, so enabling one would reintroduce its fetch
  without crossing anything this document flagged. The web UI is replaced by `frontend/ui_stub`.
  The sandboxed build is the proof that none is reachable *in this configuration*: with no network
  at all, the build still completes.

### 3. The server binds loopback unless told otherwise, and refuses a non-loopback bind without an API key

**Status: first half holds. Second half is not implemented.**

The default host is `127.0.0.1` (`third_party/llama.cpp/common/common.h`). Nothing has to be passed
to get a loopback-only server.

But `--host 0.0.0.0` with no `--api-key` is accepted and serves unauthenticated requests to anyone
who can route to the box. The `LLAMA_ARG_HOST` environment variable sets the host too, so a stray
export is enough. The guard that would refuse this combination does not exist. Treat a non-loopback
bind as something you must secure yourself.

### 4. A malformed model file causes a clean exit, never memory corruption

**Status: unverified.**

Model and pack parsing is the first untrusted input the engine touches, and it happens before
anything else. This is the highest-value parser in the tree and it has not been fuzzed here, by us.
Until it has, treat model files as trusted input: get them from a source you trust, check the
published SHA-256 before first use, and do not point the engine at a file someone sent you.

### 5. No prompt or completion is written to disk or to a log

**Status: holds for text. One exception, by design.**

Text prompts and completions go to the engine over a pipe and come back the same way. Nothing writes
them to a file.

The exception is images. When a request contains one, the front end encodes it in its own process
and writes the encoded rows to a temporary file created with `mkstemp` under `/tmp`, because that is
how they are handed to the engine child; the file is removed when the slot is released
(`frontend/strata_context.cpp`). So while a request with a picture is in flight, that picture exists
on disk in encoded form, in a file owned by the user running the server. The image path has not been
exercised in this fork.

The engine's own logging has not been audited line by line for prompt content, so this invariant is
"believed, spot-checked" rather than "proven". The sandbox narrows it independently of that audit:
the engine child can only write where the Landlock ruleset allows, so a stray write of prompt text
to an arbitrary path fails even if some code path attempts it.

Unrelated to disclosure but relevant to verifying any of this: the engine's **decode is not
reproducible run to run**. Two runs of the same binary with the same flags at temperature 0 give
different answers, and still do with `STRATA_FREEZE_EXPERTS=1` and `STRATA_ROUTE_TAIL_SKIP=0`;
prefill-dominated requests are identical every time, so the variance is in decode, which is where
speculative decoding runs. Output equality therefore cannot be used to check that a change to this
engine was behaviour-preserving.

### 6. The build is reproducible

**Status: not achieved. Planned.**

No project in this family has reproducible builds. Achieving it would be the thing that makes this
fork demonstrably more trustworthy than its parents, rather than merely differently sourced, and it
is the reason published verification hashes are promised and not yet delivered.

The honest form of the claim, when it arrives, will be "two builds of the same **commit**, with the
same pinned toolchain and `PORTABLE=1`, produce the same hashes" — not "the same tree", and not
across machines at the default settings.

Three concrete obstacles, all measured here rather than assumed:

- **`-march=native` is the default.** `STRATA_PORTABLE` is `OFF` unless asked for, and ggml-cpu then
  compiles with `-march=native`, baking this host's instruction set into the binary. Two different
  CPUs cannot produce the same hash at that setting, by construction. `PORTABLE=1` drops ggml-cpu to
  an AVX2 baseline; Strata's own AVX-512 kernels stay in the binary as separately compiled objects
  dispatched at run time, so the cost is small.
- **Absolute paths get embedded.** Two trees built from identical source at different paths produce
  different binaries; that was observed here between this tree and a reference clone of the same
  commit, including for a library whose size was byte-identical.
- **A commit identifier is embedded**, though no longer by this project's own CMake: llama.cpp's
  build-info records the submodule's commit into the front end.

Usefully, no `__DATE__`, `__TIME__` or `__TIMESTAMP__` appears anywhere in this project's sources or
in the llama.cpp files it compiles, so the most common source of build nondeterminism is already
absent.

**Measured, rather than assumed.** The same commit was built twice from scratch at the same path
with `PORTABLE=1`, and the results compared:

| artifact | second build |
|---|---|
| `aegis` (the front end, pure C++, no CUDA) | **byte-identical** |
| `strata` (the engine) | differs |
| `libstrata_kq.so` | differs |

So the front end is already reproducible, and the nondeterminism is entirely in the CUDA
compilation. Both CUDA artifacts are the **same size** as their counterparts and differ only in
content, and the cause is visible in the binary: 61 `tmpxft_<hex>_00000000` symbol prefixes, which
are nvcc's temporary-file names with the compiler's process id in them, plus 32 embedded absolute
build paths. Neither is a property of this project's source.

That is the concrete shape of the remaining work for invariant 6: it is not a search for an unknown
cause, it is making nvcc stop naming its temporaries after its pid and stripping the build path.

The practical consequence today is that binary equality cannot be used to prove a deletion changed
nothing; compare the source diff instead.

## Weaknesses inherited from upstream

Measured on 2026-11-07 against the versions named. These are not accusations of malice — nothing
below looks malicious — and they are the specific reasons for this fork's hard rules.

**Strata (`Niko1221/Strata`, v0.2.12)**

- Trust on first use. The checksum and the signing public key ship inside the same release as the
  archive they authenticate, so a first install trusts only HTTPS and a GitHub account. Upstream's
  own `SECURITY.md` says so plainly, to its credit.
- Update signatures are verified by a hand-rolled pure-Python Ed25519 implementation, unchanged
  across twelve releases.
- No external security audit. The `SECURITY.md` cites an issue as the place audit findings are
  collected; that issue is closed. (PolyStrata's `SECURITY.md` is a copy of this one and still cites
  the same closed issue. That defect is a large part of why this project wrote its own.)
- No build provenance: no attestation, no sigstore, no SLSA, no reproducible builds. The release
  assets cannot be tied to the public source.
- The desktop application now ships a coding agent with a mode that grants full filesystem and
  command access without prompting. That is a far larger surface than an inference server, and it
  is attached to the same update channel.

**PolyStrata (`simplexai-labs/PolyStrata`, v0.1.45-poly.8)**

- No signed update feed of any kind.
- `setup.py` is 8,921 lines and contacts thirteen hosts.
- It pulls the prebuilt engine from Strata's releases: the same opaque binary, now without even
  Strata's signature layer.
- Its download helper performs **no hash verification**. Model GGUFs are separately SHA-256 pinned;
  nothing else is.
- On Debian and Ubuntu it downloads a `.deb` to a fixed path in `/tmp` with no hash check and
  installs it with `sudo dpkg -i`. A `.deb`'s maintainer scripts run as root. It is gated on
  `apt-get` existing, so it never fires on Arch — but it is an unverified download executed as root.
- `pip install` runs without `--require-hashes`; `requirements.txt` pins versions only.

**UnbiasedStrata (`robertkarl/UnbiasedStrata`)**

- No `SECURITY.md` and no threat model.
- Its llama.cpp submodule points at the maintainer's personal fork rather than `ggml-org`. This fork
  keeps that pin for now, by commit hash; re-pointing at `ggml-org` is planned.
- It publishes **no release binaries at all**. That is the one practice worth copying wholesale, and
  this fork copies it.

**The llama.cpp submodule's web-UI fetcher**

`scripts/ui-assets.cmake` downloads `dist.tar.gz` and validates it against `dist.tar.gz.sha256`
fetched from the same base URL. A server that can serve a tampered archive can serve a matching
checksum, so the check constrains transport errors, not an attacker — structurally the same
trust-on-first-use flaw this document faults Strata for.

It is forced off in this configuration, the web UI is replaced by `frontend/ui_stub`, and the
no-network build is evidence that the path is unreachable here. That is also the reason to keep it
off: the guard is the configuration, so any change that re-enables the web UI re-introduces the
flaw.

## Not covered

- **The CUDA kernels are unaudited.** They are the bulk of the engine. Compiling them from source
  proves provenance, not correctness. Nobody has verified them, including us.
- **Kernel numerical correctness cannot be checked without GPU runs against real weights**, compared
  against a reference. That work has not been reproduced in this fork.
- **A container is a soft boundary for the GPU.** The NVIDIA kernel driver's ioctl interface is not
  namespaced, so a process with `/dev/nvidia*` has a wide, un-namespaced path into the driver.
  Containment buys isolation of the filesystem and the network, not of the GPU. Only VFIO
  passthrough to a VM closes that gap.
- **Containment does not hide prompts.** Prompts are the engine's input by design; no sandbox can
  keep them from it. Egress control is the control that matters — the question is not whether the
  engine can read your prompt, it is whether anything can leave the box.
- **Anything running as your user.** The server, the model files and the listening port are all
  accessible to any process with your uid. This project reduces what the server itself does; it does
  not sandbox you from yourself.
- **The toolchain below the source.** The CUDA toolkit, the NVIDIA driver, the compiler and the
  kernel are trusted as given. There is no attempt to verify them.
- **The model's behaviour.** Jailbreaks, bad or dangerous output, and prompt injection of the agent
  that calls this server are the model's and the agent's problem. This server runs the weights it is
  pointed at.
- **Windows, macOS and AMD paths.** Code for all three is in the tree and none of it is built or run
  here, so no claim in this document has been checked against it.
- **Chat templates and tool-call parsers for models this fork does not serve.** The front end links
  llama.cpp's `common`, which carries template detection and output parsers for many model families.
  They are compiled in whether or not they can be reached: the DeepSeek-V4.1 parser, for instance,
  is still present in the `aegis` binary although the DeepSeek engine was removed, because the
  pinned submodule's tip is the commit that added it. These parsers run on model output, which is
  untrusted input. Pruning them is not currently possible without diverging from the submodule.

## What would strengthen this

In the order it is worth doing, and none of it done yet:

1. ~~A seccomp filter installed before the engine `exec`, plus a test that fails when the filter is
   removed.~~ **Done** — see invariant 1. The engine is contained and the test has been seen to fail.
2. ~~Restricting the engine child's filesystem view.~~ **Done** — a Landlock allowlist, derived from
   the invocation and logged line by line. Not reduced to "the model directory only": CUDA needs a
   set of system paths, and the real list is in `docs/SANDBOX.md`.
3. The same filter for the front-end process itself, after the listener binds (`docs/SANDBOX.md` §9).
   That is what would close the remaining half of invariant 1.
4. A refusal to bind a non-loopback address without `--api-key`, closing invariant 3.
5. Fuzzing the GGUF and pack parsers, closing invariant 4.
6. A pinned toolchain and published hashes, closing invariant 6. `flake.nix` is a first step and has
   never been evaluated.
