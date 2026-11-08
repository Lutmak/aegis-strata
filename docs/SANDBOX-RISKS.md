# What this does not protect against

Stated plainly, because a containment feature that is oversold is worse than none: it moves the
reader's attention away from the gaps. `THREAT_MODEL.md` has a "Not covered" section for the gaps
that bear on the threat model; this document holds the ones specific to the containment code.

---

## 1. The GPU is a soft boundary, and the filter does not change that

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

## 2. Containment does not hide prompts

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

## 3. Invariant 2 is not proved by any of this

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

## 4. The denylist is unsound in principle

The seccomp filter is a **denylist** of 21 syscalls plus an argument allowlist on `socket`. That
choice is defended at length in `SANDBOX.md` §3 — a default-deny allowlist for a CUDA process cannot
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

## 5. Landlock denies; it does not hide

Landlock mediates `open`, `read`, `write`, `execute`, create, delete, rename and truncate. It does
**not** mediate `stat`, `access` or `readlink`. So the engine can still enumerate the shape of the
filesystem — which files exist, their sizes, their mtimes — it simply cannot read them. If the mere
*existence* of a path is sensitive, this design does not hide it; candidate (A), the mount
namespace, would have.

Related, smaller gaps in the filesystem half:
- `/tmp` is readable because the front end hands pictures over through a `mkstemp` file there
  (`frontend/strata_context.cpp#open_pictures_file`). So anything any process leaves world-readable in `/tmp` is
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

## 6. The engine can still be killed, and the front end still trusts it

Nothing here is availability protection. A compromised or buggy engine can exit, hang, or burn the
GPU; the front end's supervisor thread already handles the exit case by shutting down so a service
manager can restart both (`frontend/main.cpp#so the service manager can restart`).

More importantly, **the front end parses whatever the engine sends** over the line protocol. The
sandbox constrains what the engine can do to the *system*; it does nothing about what the engine can
do to the *front end* through that pipe. `Engine::classify` and the `DONE`/`PP`/`INFO` parsers in
`frontend/engine.hpp` are the trust boundary on that side, and they are not in Phase 6's scope.

## 7. The parent process is not contained

This design sandboxes the **child**. The front end keeps full network access, because it has to
bind and serve. `aegis` imports `connect` and `getaddrinfo`
from cpp-httplib's unused client half, and that is unchanged by any of this. Invariant 1
reads "the engine process", so it is satisfied; but a reader who assumes the whole server has no
outbound capability would be wrong.

`SANDBOX.md` §9 sketches the fix (a second, tiny filter in the parent after `listen()`, denying
`socket`, `connect` and the `send*` family outright) and explains why it is deliberately not part of
this change.

## 8. Things that were considered and left out, so they are not mistaken for oversights

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
  supervisor's `waitpid` contract (`frontend/engine.hpp#kill(-1, SIGKILL)`, with a comment recording that a
  `kill(-1, SIGKILL)` once took out two login sessions). Not worth that risk for what it buys;
  `/proc` is already narrowed, which covers most of the same ground.
- **Landlock's network rights as the egress control.** ABI 4 offers only
  `LANDLOCK_ACCESS_NET_BIND_TCP` and `LANDLOCK_ACCESS_NET_CONNECT_TCP`. **TCP only** — UDP, and
  therefore DNS and QUIC exfiltration, would walk past it untouched. They are handled anyway,
  because handling them with no rule costs two lines and denies all TCP, but they are a second lock
  and never the control.

---

## Not checked

- **Nothing was run against the real engine, a real model, or a GPU.** The validation is
  `sandbox_test` (14 cases, all passing, three deliberate breaks each producing exactly its named
  failure) plus static analysis of the engine's and CUDA's import sets. Whether CUDA initialises
  under this exact filter and allowlist on the RTX 3080 Ti is **unverified**, and it is the first
  thing to run on a machine with the card.
- **Whether CUDA ever calls `socket()` with a domain other than `AF_UNIX`** is unverified. No CUDA
  library imports `getaddrinfo`, which rules out DNS, and `libcuda`'s socket use is consistent with
  local IPC (`nvidia-persistenced`, MPS) — but "consistent with" is not "confirmed". The
  `SECCOMP_RET_LOG` derivation described in `SANDBOX.md` §4 settles it with no risk to the engine.
- **The claim that the kernel's seccomp bitmap cache absorbs this filter shape** is reasoned from
  kernel behaviour, not measured. If it is wrong, `--ple-io direct` pays per-`pread` BPF cost. The
  before/after decode-rate comparison against the Phase 1 baseline is the test.
- **The fail-closed branch for a kernel without Landlock was not exercised.** The reference machine has
  `CONFIG_SECURITY_LANDLOCK=y` and `landlock` in `/sys/kernel/security/lsm`, so the branch is
  unreachable here. Reviewed by reading, not tested.
- **Landlock ABI 10 is what this kernel reports; this design only uses features up to ABI 5** plus
  ABI 4's network rights. What ABIs 6-10 add was not researched, and no claim is made about them.
- **The `--mmproj` / `GENI` picture path was not tested under the sandbox at all** — not in
  `sandbox_test` (no projector) and not in `e2e.sh` (which only checks that a picture without
  `--mmproj` is refused). The `/tmp` read entry is reasoned from
  `frontend/strata_context.cpp#open_pictures_file`, not observed. If `--mmproj` is in use, test it.
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
