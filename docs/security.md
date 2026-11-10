# Security: what is protected, and what is not

This is the detailed version. `SECURITY.md` at the root is how to report a problem; the README's
"Why you might pick this one" is the short summary. Read this one if you are deciding whether to
trust the thing, or auditing whether its claims hold.

`docs/sandbox.md` is the companion: how the containment is actually built, syscall by syscall.

Two processes matter throughout. **`aegis`** is the server: it holds the listening socket, owns the
tokenizer and the chat template, and links no engine code. **`strata`** is the engine: it reads the
model, runs on the GPU, and is the process that sees your prompts. The engine is the one that gets
locked down, because it is the one with no reason to talk to anything.

## What is being protected

| Asset | Why it matters |
|---|---|
| Prompts and completions | The engine sees everything the coding agent sends it: source code, whatever secrets are in the files the agent read, and the conversation history |
| Your filesystem | The server runs as your user, so everything that user can read is in reach: SSH keys and known-hosts, shell and agent history, any network shares mounted in your home, and whatever your SSH config names |
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

**Status: enforced for the engine, and tested. Still incidental for the server.**

Measured with `objdump -T` on this build:

- The server (`aegis`) imports `socket`, `bind`, `listen` and `accept4`, which it needs because
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

**That is what the sandbox is for.** The server spawns the engine as a child, so the parent
installs containment between `fork` and `exec` (`server/strata_sandbox.cpp`, `docs/sandbox.md`):

- a network namespace holding only `lo`, so there is no route out of the child at all;
- a seccomp filter that permits `socket()` and `socketpair()` only for `AF_UNIX`, which is CUDA's
  local IPC, and returns `EAFNOSUPPORT` for every other domain, present or future. `connect()` is
  deliberately *not* the control point: its argument is a `sockaddr *`, and seccomp cannot
  dereference a pointer, whereas `domain` is a scalar it can compare soundly;
- `KILL_PROCESS` for the syscalls that would undo either of those — `setns`, `unshare`,
  `open_by_handle_at`, `io_uring_*`, `ptrace` and the rest, listed with a reason each in
  `docs/sandbox.md`.

If it cannot be installed, the engine does not start. `/health` and `/props` report `enforced`, `enforced-no-netns`,
`unavailable` or `off`, so a running server can be checked rather than trusted.

**It is tested, and the test has been seen to fail.** `server/tests/sandbox_test.cpp` runs in
`make test` with no GPU and no model: it forks, applies the sandbox and re-execs itself as a probe.
It asserts that `socket()` **creation** fails for `AF_INET`, `AF_INET6` and `AF_NETLINK` and
succeeds for `AF_UNIX` — creation, not reachability, because `socket()` succeeds on an airgapped
box and asserting on `connect()` is how this test would pass for the wrong reason. One case reruns
every probe with the sandbox off and every result inverts, so the suite cannot pass vacuously.
Setting the filter's deny action to ALLOW produces exactly three failures; zeroing Landlock's
handled access rights fails thirteen assertions.

**What is still incidental is the server.** `aegis` itself imports `connect` and `getaddrinfo`
and is not behind a filter of its own; a second filter installed in the parent after the listener
binds is designed in `docs/sandbox.md` §9 and not built. So invariant 1 holds structurally for the
process that runs the model, and by inspection only for the process that serves HTTP.

For the **server** this remains a statement about what the code happens not to call. If you need
that process constrained too, run it in a network namespace or block it at the firewall; a filter
for it is designed in `docs/sandbox.md` and not built.

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
  without crossing anything this document flagged. The web UI is replaced by `server/ui_stub`.
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

The exception is images. When a request contains one, the server encodes it in its own process
and writes the encoded rows to a temporary file created with `mkstemp` under `/tmp`, because that is
how they are handed to the engine child; the file is removed when the slot is released
(`server/strata_context.cpp`). So while a request with a picture is in flight, that picture exists
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
  build-info records the submodule's commit into the server.

Usefully, no `__DATE__`, `__TIME__` or `__TIMESTAMP__` appears anywhere in this project's sources or
in the llama.cpp files it compiles, so the most common source of build nondeterminism is already
absent.

**Measured, rather than assumed.** The same commit was built twice from scratch at the same path
with `PORTABLE=1`, and the results compared:

| artifact | second build |
|---|---|
| `aegis` (the server, pure C++, no CUDA) | **byte-identical** |
| `strata` (the engine) | differs |
| `libstrata_kq.so` | differs |

So the server is already reproducible, and the nondeterminism is entirely in the CUDA
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

It is forced off in this configuration, the web UI is replaced by `server/ui_stub`, and the
no-network build is evidence that the path is unreachable here. That is also the reason to keep it
off: the guard is the configuration, so any change that re-enables the web UI re-introduces the
flaw.

## What this does not protect against

Stated plainly, because a protection that is oversold is worse than none: it moves your attention
away from the gap. The first group is general; the second is specific to how the containment is
built.

### In general

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
- **Chat templates and tool-call parsers for models this fork does not serve.** The server links
  llama.cpp's `common`, which carries template detection and output parsers for many model families.
  They are compiled in whether or not they can be reached: the DeepSeek-V4.1 parser, for instance,
  is still present in the `aegis` binary although the DeepSeek engine was removed, because the
  pinned submodule's tip is the commit that added it. These parsers run on model output, which is
  untrusted input. Pruning them is not currently possible without diverging from the submodule.

### Specific to the containment

#### 1. The GPU is a soft boundary, and the filter does not change that

`/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm` and `/dev/nvidia-uvm-tools` are in the
allowlist with read, write and `LANDLOCK_ACCESS_FS_IOCTL_DEV`, because without them CUDA cannot
initialise. **The NVIDIA kernel driver's ioctl interface is not namespaced.** A network namespace, a
Landlock ruleset and a seccomp filter all live above it; none of them can see inside an `ioctl`
request, and the request codes and payloads are undocumented, so argument filtering is not an
option even in principle (seccomp cannot dereference the pointer, and nobody outside NVIDIA knows
what the structure means).

Concretely: a bug reachable through those ioctls is a kernel bug, and this design gives an attacker
who reaches it exactly the same position they would have had without the sandbox. The same is true
of DMA: the GPU can be programmed to read and write host memory, and the IOMMU is what bounds that,
not us.

This is the one place where the filter buys **nothing** rather than something partial. Only a VFIO
passthrough VM closes it. On the reference machine the card sits alone in its IOMMU group with
`iommu=on iommu=pt` already on the kernel command line, so that option exists if the threat ever
justifies the cost; check your own grouping before assuming it.

A secondary, smaller point in the same area: `nvidia-container-toolkit` is deliberately not
installed and this design keeps it that way — devices are allowed by name, which is why the
allowlist can be a list of paths instead of a CDI hook with its own CVE history.

#### 2. Containment does not hide prompts

The engine's input *is* the prompt. Every token the coding agent sends, including the contents of
source files it read and any secret in them, crosses the pipe into the engine by design. No sandbox
can change that, and anyone who reads "the engine is contained" as "the engine does not see my
code" has misread it.

What the containment buys is that the prompt has **nowhere to go**: no socket to send it on, and no
writable path outside `/dev/shm` and what the invocation asked for. Egress control is the control
that matters here, and this design is an egress control plus a write control. It is not
confidentiality against the engine itself.

Corollary, and worth saying because it is the realistic attack: if the engine is compromised, the
*current* conversation is lost regardless. What is protected is everything the engine was not given
— the rest of `$HOME`: your ssh keys and known-hosts, your shell and agent history, any
network shares mounted there, and whatever your ssh config names.

#### 3. Invariant 2 is not proved by any of this

The canary test is sometimes described as proving invariants 1 and 2. It proves
invariant 1 and strengthens invariant 5. **Invariant 2 — "nothing executes that was not compiled
from source in this tree" — is not touched**, for a specific and unfixable reason:

Landlock governs paths. It does not govern pages. CUDA JITs PTX at runtime, so the engine must be
able to make anonymous memory executable; `mprotect(PROT_EXEC)` therefore cannot be denied, and
denying `memfd_create` alongside it would be theatre. Any code-reuse or injected-shellcode payload
inside the engine's address space runs with the engine's full (sandboxed) authority, and nothing in
this design sees it.

The independent review demonstrated the consequence rather than leaving it implied: with execute
granted nowhere, `execv` of a blob on disk is denied by Landlock (`EACCES`) while
`memfd_create` + `execveat(AT_EMPTY_PATH)` **runs it**, because a memfd inode lives on a
kernel-internal superblock that Landlock allows unconditionally. So the execute right in this policy
constrains `execve` **by path only**. It is a startup convenience -- it is what lets a script engine
reach its interpreter -- and it is not a containment control. The engine cannot be stopped from
executing code it can synthesise.

Invariant 2 is a provenance property. It is proved by Phase 7's reproducible build and by hard rule
1, not by a sandbox.

#### 4. The denylist is unsound in principle

The seccomp filter is a **denylist** of 21 syscalls plus an argument allowlist on `socket`. That
choice is defended at length in `docs/sandbox.md` §3 — a default-deny allowlist for a CUDA process cannot
be maintained without GPU CI, and a filter that is widened whenever the engine breaks is a filter
that means nothing — but the cost is real and it is this:

**A syscall nobody thought of, that achieves the same effect, walks straight through.** `io_uring`
is not a hypothetical; it is the proof of the class. A ring can `IORING_OP_SOCKET` and
`IORING_OP_CONNECT`, and its operations are not syscalls, so **no seccomp filter can see them**. It
is on the kill list now, but the next such mechanism will not be, until someone notices.

The mitigation is the network namespace, which has no syscall-level bypass because there is nothing
on the other side to reach. **Where the netns does not install — a kernel with unprivileged user
namespaces disabled — the egress guarantee degrades from "structural" to "a denylist that is
believed complete".** That state is logged (`sandbox: unshare: ` then the kernel's error, then `; egress is denied by the syscall filter only`) and is visible in `sandbox_describe()`'s "no network namespace", precisely so it is
not mistaken for the strong case.

Two more holes in the syscall half, both closed by the netns and open without it:
- A socket fd **received over an AF_UNIX socket** via `SCM_RIGHTS` was never created by `socket()`,
  so the filter never saw it. `pidfd_getfd` is killed, which closes the direct theft, but a
  cooperating already-compromised process on the box can still hand one over. That attacker already
  has network access, so the practical value is low — but it is not zero and it is not closed.
- `clone3` passes its flags in a **pointer** struct, so `CLONE_NEWUSER` cannot be refused through
  it. `unshare`, `setns`, `mount` and `open_by_handle_at` are killed, so a nested namespace grants
  nothing, but the asymmetry is worth knowing about.

#### 5. Landlock denies; it does not hide

Landlock mediates `open`, `read`, `write`, `execute`, create, delete, rename and truncate. It does
**not** mediate `stat`, `access` or `readlink`. So the engine can still enumerate the shape of the
filesystem — which files exist, their sizes, their mtimes — it simply cannot read them. If the mere
*existence* of a path is sensitive, this design does not hide it; candidate (A), the mount
namespace, would have.

Related, smaller gaps in the filesystem half:
- `/tmp` is readable because the server hands pictures over through a `mkstemp` file there
  (`server/strata_context.cpp#open_pictures_file`). So anything any process leaves world-readable in `/tmp` is
  readable by the engine. Narrowing this to the single rows file is possible — pass the fd instead
  of the path — but it is a protocol change to `GENI` and belongs in its own piece of work.
- `/proc` is narrowed to `/proc/self` plus three global files, so other processes' `environ` and
  `cmdline` are denied. But `/proc/self` includes `/proc/self/mem` and `/proc/self/maps`, which is
  unavoidable and harmless (it is its own memory) — and it means the engine can rewrite its own
  code through `/proc/self/mem` regardless of `mprotect`. See §3.
- The allowlist is derived in part from "every engine argument that names an existing path". That
  heuristic grants a **directory hierarchy** when the argument is a directory. `--mtp
  ~/models/aegis/mtp/rt` grants that subtree, which is correct; a careless
  `--pack ~` would grant the whole home directory and the startup log would say so on one
  line. The log is the control; read it.

#### 6. The engine can still be killed, and the server still trusts it

Nothing here is availability protection. A compromised or buggy engine can exit, hang, or burn the
GPU; the server's supervisor thread already handles the exit case by shutting down so a service
manager can restart both (`server/main.cpp#so the service manager can restart`).

More importantly, **the server parses whatever the engine sends** over the line protocol. The
sandbox constrains what the engine can do to the *system*; it does nothing about what the engine can
do to the *server* through that pipe. `Engine::classify` and the `DONE`/`PP`/`INFO` parsers in
`server/engine.hpp` are the trust boundary on that side, and they are not in Phase 6's scope.

#### 7. The parent process is not contained

This design sandboxes the **child**. The server keeps full network access, because it has to
bind and serve. `aegis` imports `connect` and `getaddrinfo`
from cpp-httplib's unused client half, and that is unchanged by any of this. Invariant 1
reads "the engine process", so it is satisfied; but a reader who assumes the whole server has no
outbound capability would be wrong.

`docs/sandbox.md` §9 sketches the fix (a second, tiny filter in the parent after `listen()`, denying
`socket`, `connect` and the `send*` family outright) and explains why it is deliberately not part of
this change.

#### 8. Things that were considered and left out, so they are not mistaken for oversights

- **`PR_SET_DUMPABLE = 0`** in the child would stop a core dump carrying prompt text (invariant 5)
  and stop same-uid `ptrace` *into* the engine. It is left out because making the process
  non-dumpable re-owns `/proc/<pid>/*` to root, and the engine reads `/proc/self/io`
  (`src/platform/memory.cpp#"/proc/self/io"`), which is mode 0400 — whether a non-dumpable process can still
  read its own is uncertain and untested here. Cheap experiment, worth doing; not worth guessing.
  Note that `yama.ptrace_scope = 1` on the reference machine already limits inbound `ptrace` to ancestors.
- **`RLIMIT_*`** in the child. Not a containment control for anything in the threat model, and the
  engine legitimately wants 42.7 GiB of RSS and a large `RLIMIT_MEMLOCK`.
- **A pid namespace.** Would hide the user's other processes from the engine, which is worth
  something — but it needs an init process inside the namespace to reap, and reparenting changes the
  supervisor's `waitpid` contract (`server/engine.hpp#kill(-1, SIGKILL)`, with a comment recording that a
  `kill(-1, SIGKILL)` once took out two login sessions). Not worth that risk for what it buys;
  `/proc` is already narrowed, which covers most of the same ground.
- **Landlock's network rights as the egress control.** ABI 4 offers only
  `LANDLOCK_ACCESS_NET_BIND_TCP` and `LANDLOCK_ACCESS_NET_CONNECT_TCP`. **TCP only** — UDP, and
  therefore DNS and QUIC exfiltration, would walk past it untouched. They are handled anyway,
  because handling them with no rule costs two lines and denies all TCP, but they are a second lock
  and never the control.

---

### Found by adversarial review, not yet fixed

An adversarial pass over `server/` and the engine's file readers on 2026-11-08 produced these. Two
findings from the same pass — the pathname-socket escape and control-token literals in JSON object
keys — were fixed and are covered by tests; the rest are recorded here rather than fixed, and none
of them is reachable without either an operator-supplied file or a client that is already trusted to
send requests.

A third, the missing engine timeout, was fixed afterwards and is covered by a test; it is kept below
with that marked, because the reasoning is what justifies the shape of the fix.

- ~~**There is no engine timeout, so a wedged engine blocks the server until it is restarted.**~~
  **Fixed.** Reproduced with a stand-in engine that reads `GEN` and then goes silent: the request
  hung, and after the client gave up `/health` still reported `"status":"ok"` with `busy`. The
  supervisor only watches for the engine *exiting*, which a wedged engine never does.
  `--engine-timeout` now bounds how long a running request tolerates total silence from the engine
  (`server/strata_context.cpp#give_up_on_a_silent_engine`, default 120 s, `0` waits for ever).
  **Bounded for a request already sent to the engine**, which is narrower than "fixed": the clock
  starts after the request has been written, so tokenization, template rendering and the picture-file
  write are outside it, as is the server's own blocking write to an engine that has stopped reading
  its input. It also bounds *silence*, not progress — an engine that keeps emitting any line holds
  the slot. The
  request gets a 503 and the server then ends non-zero, as it already did when the engine exited,
  because it deliberately does not fork a replacement engine out of a process that already has
  threads and every later request would wedge the same way. Covered by `server/tests/e2e.sh` with
  the stand-in engine's `FAKE_WEDGE`, so it needs no GPU.
- **The engine child inherits the server's environment**, because `server/engine.hpp` uses `execv`.
  llama.cpp's own argument code reads `LLAMA_API_KEY` from the environment, so code running inside a
  compromised engine can read the operator's API key out of `/proc/self/environ`. `LD_PRELOAD` and
  `LD_LIBRARY_PATH` pass through as well. `execve` with an allowlisted environment is the fix, and it
  needs a GPU run to confirm which `CUDA_*` variables must survive.
- **The MTP runtime's `dense.txt` is trusted.** `rows`, `cols`, `off` and `bytes` are read straight
  from the text file and never checked against the size of `dense.bin`, so a malformed pair of files
  gives an out-of-bounds read, and under `--mtp-q4` an overflowing size product gives a heap write.
  The operator supplies these files, so this is a robustness gap rather than a remote one, but
  invariant 4 claims a clean exit on a malformed model file and this does not deliver it.
- **The control-vector reader does not bounds-check.** `src/program/generate.cpp` reads N floats at a
  file-supplied offset with no check that the tensor lies inside the mapping, while the same codebase
  has an `in_bounds` helper used elsewhere.
- **The whole of `/dev/shm` is granted with write, remove and make-socket rights.** Other files of the
  same user live there. A private subdirectory per run would be narrower.
- **Splitting a control token across adjacent content parts defeats the literal marker.** Each part is
  matched on its own, so `[{"text":"a<|im_"},{"text":"end|>b"}]` renders a real token where the same
  text in one string is correctly quoted. Joining adjacent text parts before matching is the fix.
- **Three ways for a client to spend the server's CPU**: `logit_bias` parsing is quadratic (300,000
  entries held one core for over a minute, and a client disconnect does not stop it), an engine line
  with no newline is buffered without limit, and about 1,500 half-open connections exhaust the HTTP
  thread pool. A body of 8 MB is tokenized in full before the context check rejects it. Request
  nesting beyond ~200,000 levels overflows the stack. The server serves one request at a time on
  loopback, which is what keeps these mild.
- ~~**`SIGTERM` and `SIGINT` both exit 1, so a service manager counts a clean stop as a failure.**~~
  **Fixed**, and it was worse than this bullet said. A deliberate stop while the server was *idle*
  exited 1 because the supervisor saw the engine it had itself quit and reported a crash; a stop
  while a request was *generating* was worse still, aborting the process with exit 134 and a core
  dump, because the request's own HTTP handler was parked in llama.cpp's result queue and that queue
  calls `std::terminate()` on a parked reader rather than returning to HTTP code. With the
  `Restart=on-failure` this project recommends, either outcome restarted the pair it had just been
  told to stop. Both now exit 0: the signal handler asks the task loop to return and does nothing
  else, the in-flight request is answered on the main thread once that loop has stopped, and
  `server/tests/e2e.sh` asserts exit 0 for both signals both idle and mid-generation.
- **Every run leaves its `mkdtemp` directory behind** (121 were found on the reference machine).
- **An engine-side exception message reaches the HTTP client verbatim.** The engine prints
  `ERR <text>` on its protocol and the server forwards it as a 500 body, so a `std::filesystem`
  error or a GGUF/pack parse failure can put **absolute host paths** in the response. The client is
  same-uid on loopback and can read those paths directly, so this is informational rather than a
  boundary crossing, but it is a wider class than it was: before, only the server's own errors could
  do this, and now any exception anywhere in the engine can. Nothing echoes request content, because
  the only things the server writes to the engine are numeric sampling values and token ids.
- **Malformed JSON generally returns 500 rather than 400**, and `/detokenize` leaks
  `vector::_M_range_check` text on an out-of-range id.

### Not checked

- ~~Nothing was run against the real engine, a real model, or a GPU.~~ **Now run.** On 2026-11-08 the
  whole path was exercised on the reference machine with the real weights: the engine loads under the
  filter and the allowlist, `/health` reports `loaded` with `sandbox = enforced`, and non-streaming
  completions, SSE streaming and a tool call all answered correctly. That was repeated after the
  ABI 6/9 hardening above, so CUDA is confirmed to initialise with pathname sockets denied and
  signals scoped. The GPU-free validation is still `sandbox_test`, now 59 assertions.
- **Whether CUDA ever calls `socket()` with a domain other than `AF_UNIX`** is unverified. No CUDA
  library imports `getaddrinfo`, which rules out DNS, and `libcuda`'s socket use is consistent with
  local IPC (`nvidia-persistenced`, MPS) — but "consistent with" is not "confirmed". The
  `SECCOMP_RET_LOG` derivation described in `docs/sandbox.md` §4 settles it with no risk to the engine.
- **The claim that the kernel's seccomp bitmap cache absorbs this filter shape** is reasoned from
  kernel behaviour, not measured. If it is wrong, `--ple-io direct` pays per-`pread` BPF cost. The
  before/after decode-rate comparison against the Phase 1 baseline is the test.
- **The fail-closed branch for a kernel without Landlock was not exercised.** The reference machine has
  `CONFIG_SECURITY_LANDLOCK=y` and `landlock` in `/sys/kernel/security/lsm`, so the branch is
  unreachable here. Reviewed by reading, not tested.
- **Landlock ABIs 6-10 have now been researched, and two of them were load-bearing.** The earlier
  version of this document said they were not, and that gap hid a complete escape. Up to ABI 5 the
  kernel does not govern `connect()` to a *pathname* `AF_UNIX` socket at all, and the syscall filter
  permits `connect()` because CUDA needs it. A contained engine therefore reached
  `/run/user/<uid>/bus`, `/run/user/<uid>/systemd/private` and `/run/dbus/system_bus_socket`, and all
  three answered `OK <guid>` to `AUTH EXTERNAL` — and an authenticated caller on the systemd socket can
  ask for a transient unit, which runs outside the sandbox with the host network. ABI 9's
  `LANDLOCK_ACCESS_FS_RESOLVE_UNIX` closes it, and ABI 6's `LANDLOCK_SCOPE_SIGNAL` and
  `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET` close signalling and abstract sockets. All three are now
  requested, no path is granted `RESOLVE_UNIX`, and `server/tests/sandbox_test.cpp` asserts
  `unixpath=denied signal=denied` under enforcement and the inverse with the sandbox off. CUDA was
  re-verified on the reference machine afterwards: the engine loads and serves with
  `sandbox = enforced`. If a future CUDA feature needs a socket (MPS, nvidia-persistenced), grant
  `RESOLVE_UNIX` on that exact path rather than widening the handled set.
- **ABIs 7, 8 and 10 are still not used.** They were read but nothing in them was needed here; no
  claim is made about what they would add.
- **The `--mmproj` / `GENI` picture path was not tested under the sandbox at all** — not in
  `sandbox_test` (no projector) and not in `e2e.sh` (which only checks that a picture without
  `--mmproj` is refused). The `/tmp` read entry is reasoned from
  `server/strata_context.cpp#open_pictures_file`, not observed. If `--mmproj` is in use, test it.
- **macOS was not built.** The non-Linux branch compiles by inspection only; there is no Mac here.
- **Reviewed, not formally verified.** The design and the code were reviewed independently of
  whoever wrote them, which found and fixed four high-severity defects in this file's subject —
  among them a relative `-m` resolving to `$HOME` and granting the contained engine the whole home
  directory. Independent review is not proof: nothing here has been formally verified, and the
  kernel mechanisms are trusted as documented.
- The engine's own crash handler (`ggml.c:197-221`, `fork` + `execlp("gdb")`) was identified but its
  behaviour under the sandbox was not tested. Expect it to fail: `/usr/bin` is not executable under
  the allowlist, so a crashing engine will fall back to `backtrace_symbols_fd`. That is an
  improvement, not a regression, but it is untested.

## What would strengthen this

In the order it is worth doing, and none of it done yet:

1. ~~A seccomp filter installed before the engine `exec`, plus a test that fails when the filter is
   removed.~~ **Done** — see invariant 1. The engine is contained and the test has been seen to fail.
2. ~~Restricting the engine child's filesystem view.~~ **Done** — a Landlock allowlist, derived from
   the invocation and logged line by line. Not reduced to "the model directory only": CUDA needs a
   set of system paths, and the real list is in `docs/sandbox.md`.
3. The same filter for the server process itself, after the listener binds (`docs/sandbox.md` §9).
   That is what would close the remaining half of invariant 1.
4. A refusal to bind a non-loopback address without `--api-key`, closing invariant 3.
5. Fuzzing the GGUF and pack parsers, closing invariant 4.
6. A pinned toolchain and published hashes, closing invariant 6. `flake.nix` is a first step and has
   never been evaluated.
