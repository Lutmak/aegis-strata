# Containment of the engine child, enforced in the server before `exec`

This is implemented: `server/strata_sandbox.h`, `server/strata_sandbox.cpp` and
`server/tests/sandbox_test.cpp`. It records the design and the reasoning behind it — what was
considered, what was rejected and why each syscall on the list is on it — because none of that is
recoverable from a BPF program. Every number was measured on the reference machine (see the README) on
2026-11-08, and the command is given with each so it can be re-derived rather than believed.

---

## 1. What the child must keep working

Derived mechanically, not by guessing. Three sources, each re-runnable:

**(a) The engine's own path use** — `grep -rn '"/proc\|"/sys\|/dev/' src/ include/`:

| Path | Where | Why |
|---|---|---|
| `/proc/self/{exe,stat,status,statm,io,task,numa_maps}` | `src/prefill/mmq_kq_loader.cpp`, `src/program/generate.cpp`, `src/platform/memory.cpp`, `include/strata/platform/aux_cpus.hpp` | finds `libstrata_kq.so` beside the exe; RSS/IO/thread accounting |
| `/proc/meminfo`, `/proc/buddyinfo`, `/proc/sys/vm/nr_hugepages` | `src/core/expert_cache.cpp:49`, `src/core/pinned.cu:62,254`, `src/core/expert_source.cpp:453,2809` | expert-cache sizing, hugepage planning |
| `/sys/devices/system/cpu/*/topology/*`, `/sys/devices/system/node/*` | `include/strata/platform/aux_cpus.hpp`, `src/kernels/cpu/pool.cpp`, `src/core/expert_source.cpp` | CPU/NUMA placement of the expert worker pool |
| `/sys/fs/cgroup/*`, `/sys/dev/block/<maj>:<min>`, `/sys/kernel/mm/transparent_hugepage/enabled` | `include/strata/core/conversation_memory.hpp`, `src/program/generate.cpp`, `src/core/pinned.cu` | memory limits, rotational-disk detection for `--ple-io` |
| `/dev/shm` | `src/core/pinned.cu:89`, `--shared-expert-arena` (`src/program/generate.cpp#--shared-expert-arena`) | MAP_SHARED arena |
| the exe's own directory | `mmq_kq_loader.cpp` | `dlopen("libstrata_kq.so")` beside the binary |

**(b) The server's own handover path** — `grep -n GENI server/strata_context.cpp`: pictures are
encoded in the parent and written to `/tmp/aegis-<random>/pictures-XXXXXX`
(`server/strata_context.cpp#open_pictures_file`), whose path goes to the engine on the `GENI` line. **The
child must be able to read `/tmp`** or `--mmproj` breaks. This is the single easiest thing to miss.

**(c) Paths the engine takes as options** — `grep -oE '"--[a-z0-9-]+"' src/program/generate.cpp`
gives 18 path-taking options. Read: `--model`, `--pack`, `--mtp`, `--expert-profile`, `--ple-gguf`,
`--embd-gguf`, `--native-dense-gguf`, `--native-head-gguf`, `--control-vector`, `--cvec-dir`,
`--tokens-file`, `--vision`. Write: `--expert-profile-save`, `--prompt-cache-root`,
`--shared-expert-arena`, the `--dump-*` family. They are opaque to the server, which is why the
allowlist is derived from the invocation (§5) rather than hardcoded.

**(d) CUDA init's syscall needs.** Not guessed either, and not fully knowable without a GPU run.
What is knowable statically is the *library* import set above: CUDA needs `ioctl` (the whole driver
interface), `mmap`/`mremap`/`mprotect`/`msync`/`madvise`/`mlock`, `dlopen`/`dlmopen`, `clone` for
threads, `eventfd`, `timerfd_create`, `poll`, `shm_open`/`shmget`, SysV and POSIX semaphores,
`socket`/`socketpair`/`sendmsg`/`recvmsg` (local IPC: `nvidia-persistenced`, MPS), `getrlimit`,
`setrlimit`, `sysinfo`, `get_nprocs`, `uname`, `gethostname`, `realpath`, `nftw` over `/proc`.
**None of that is denied by this design.** The design denies exactly one thing CUDA touches —
`socket()` with a domain other than `AF_UNIX` — and §7 gives the experiment that confirms CUDA never
asks for one.

There is also a *measured* end-to-end data point that settles the biggest worry. The reference
machine's baseline was produced with the serving run inside `bwrap --unshare-net --die-with-parent`: a network
namespace with only `lo`. It loaded 47 GB + 28.8 GB of GGUF, initialised CUDA on the RTX 3080 Ti,
and decoded at 89-92 tok/s. **A network namespace demonstrably does not break CUDA or this engine on
the reference machine.** That is the strongest single fact available before any new code is written, and it
means the egress half of Phase 6 is not a gamble.

---

## 2. Three candidate designs

Different in kind, judged against the §6 actor list and the §6 invariants.

### (A) Namespaces only — in-process `bwrap`

In the child between `fork` and `exec`: `unshare(CLONE_NEWUSER|CLONE_NEWNET|CLONE_NEWNS)`, write
`uid_map`/`gid_map`, then build a minimal root from bind mounts (`/usr`, `/opt/cuda`, `/etc/ld.so.cache`,
`/proc`, `/sys`, `/dev/nvidia*`, `/dev/shm`, `/dev/null`, `/tmp`, the model dir, the exe dir) and
`pivot_root` into it. No seccomp, no Landlock.

- **Enforces:** egress completely (no route exists, not even a denied one). Filesystem completely,
  and it *hides* rather than denies — a canary outside the tree does not exist, so even `stat` fails.
- **Against the actor list:** stops a malicious model file and a prompt-injected agent from reaching
  the network or the user's `~/.ssh`; does nothing against ptrace of a sibling process (the parent
  server is in the same pid namespace unless we add `CLONE_NEWPID`, which then needs an init
  process and reparenting logic).
- **Costs:** needs unprivileged user namespaces, which are disabled on hardened and some enterprise
  kernels — so the feature would have to be optional, and an optional containment control enforces
  nothing by default. The mount list is an enumeration of *mounts*, and every one we forget is a
  hard engine failure with a confusing error; CUDA looking somewhere new in a driver update breaks
  the engine, not just the sandbox. The code is several hundred lines of `mount(2)` plumbing in the
  one context in the program where almost nothing is safe to call. It also reimplements, badly, a
  tool already on the box (`bwrap 0.13.1`), so the deletion test — would inlining this into its
  callers remove complexity or just move it? — goes against the wrapper.
- **Testable:** very. The canary genuinely is not there.

### (B) seccomp-bpf only — argument filter on `socket`

In the child: `prctl(PR_SET_NO_NEW_PRIVS)`, then one hand-written BPF program that allows
`socket`/`socketpair` only for `AF_UNIX` and kills a short list of escape syscalls. No namespaces, no
filesystem restriction.

- **Enforces:** egress at the syscall level. Nothing else.
- **Against the actor list:** stops the malicious model file and the injected agent from *opening*
  an outbound socket. Does nothing about `~/.ssh`, `~/.claude`, or writing `~/.bashrc` for code
  execution on the user's next login — so it leaves the second path to exfiltration (write a secret
  somewhere the user's own tooling will ship) wide open, and leaves the first path (write a payload,
  get it run later) open too.
- **Costs:** ~60 lines, zero dependencies, no privilege, works on every Linux kernel since 3.5.
- **Testable:** trivially (probe `socket()` and read the errno). But it does nothing for the
  filesystem half, so half of the required test has nothing to assert against. Incomplete by
  construction.

### (C) Landlock for the filesystem + seccomp for the syscall surface, with a network namespace when one can be had — **recommended**

In the child, in this order: `PR_SET_NO_NEW_PRIVS` → `unshare(CLONE_NEWUSER|CLONE_NEWNET)` and the
identity uid/gid maps → a Landlock ruleset (default-deny filesystem, explicit allowlist, plus
`handled_access_net` with no net rules when the ABI offers it) → a hand-written seccomp filter →
`close_range(3, ~0)` → `execv`.

- **Enforces:** egress twice over (no network namespace to reach it, *and* no `socket()` for an
  internet family). Filesystem as a kernel-enforced path allowlist that cannot be relaxed
  afterwards. Lateral movement closed for the specific routes that would otherwise undo both halves
  (`ptrace`, `process_vm_writev`, `pidfd_getfd`, `io_uring_*`, `open_by_handle_at`, `setns`).
- **Against the actor list:** a malicious model file (parser RCE) and a prompt-injected agent get a
  process with no network, no readable `$HOME` outside the model directory, no writable path except
  `/dev/shm` and what was asked for. It can still *receive* a descriptor over an AF_UNIX socket
  (`SCM_RIGHTS`) from another process of the same uid, which is the residual egress `security.md`
  §4 describes. `socket(AF_UNIX)` itself cannot be denied because CUDA needs local IPC — but from
  Landlock ABI 9 the engine is given no path on which it may *connect* one, which closes the escape
  route that mattered. See "AF_UNIX: the socket stays, the destinations go" below.
  A tampered CUDA toolkit or driver userspace gets the same, *except* through the GPU ioctl
  interface, which is not namespaced (see `security.md`).
- **Costs:** one new file, measured at 710 lines / 245 statements as written (the rest is comment, the UAPI fallbacks and the non-Linux stub), one `#if defined(__linux__)`, no new dependency, no privilege
  required for the mandatory half, and the netns half degrades to "seccomp only" where userns is
  disabled. Landlock *denies* rather than *hides*, so `stat` still works — path existence remains
  probeable, which is a weaker guarantee than (A) but not one the threat model needs.
- **Testable:** the probe child reports a distinct outcome per mechanism, so each half can be broken
  independently and seen to fail independently — the seven deliberate breaks that prove it are
  listed in `server/tests/sandbox_test.cpp`'s header.

### Recommendation

**(C).** The single strongest reason: it is the only candidate that enforces both halves **with no
privilege and no mount plumbing**, which is what lets it be **on by default for every user**. A
containment control that is off by default, or that requires a sysctl the distro may have disabled,
does not make an invariant structural — it makes it a documented option, and an option is not a
guarantee. (A) cannot be on by default because unprivileged user namespaces are not universally
available; (B) does nothing for the filesystem half.

Secondary reasons, in order:
1. **It cannot break the engine by omission in the way (A) can.** Landlock's failure mode is
   `EACCES` on a path we forgot, reported by the engine with that path in the message, fixable with
   `--sandbox-allow-read`. (A)'s failure mode is `ENOENT` deep inside closed-source CUDA with no
   indication of which bind mount is missing.
2. **It keeps `--ple-io direct` fast** — see §6, which is a correctness requirement, not a nicety.
3. It uses the netns *when available* rather than *depending* on it, so it gets (A)'s complete egress
   denial on the reference machine and most others without inheriting (A)'s availability problem.

---

## 3. The dependency decision: no `libseccomp`

**Decision: hand-written BPF. Do not link `libseccomp`.** (It is installed here — `libseccomp
2.6.0-1`, `/usr/include/seccomp.h` present — so this is a choice, not a constraint.)

The argument for libseccomp is real and should be stated: it normalises multi-architecture filters,
handles the x32 ABI and the `socketcall` multiplexer, resolves syscall names to numbers per arch, and
its `scmp_arg_cmp` API makes argument predicates readable. Those are the right reasons to use it when
building a **default-deny allowlist of ~300 syscalls across several architectures**.

None of them apply here:

1. **One architecture.** The engine is x86-64 CUDA. The Metal/arm64 build has no engine child of
   this kind and the filter is compiled out there anyway (§8). The multi-arch machinery is the bulk
   of libseccomp's value and all of it is dead weight.
2. **The filter is tiny and its shape is the design.** ~34 BPF instructions: an arch gate, an x32
   gate, one argument predicate on `socket`/`socketpair`, 15 syscall-number comparisons, a trailing
   `ALLOW`. That is well inside "something a few lines can do" (coding principles) and well inside
   what one person can read and verify in full. Linking 20k lines of library to emit 34
   instructions inverts the project's whole premise.
3. **It would be a build-graph and provenance liability.** The README's rule 1 wants everything
   built from source and rule 2 wants no network during the build; a system `libseccomp` becomes
   another thing `flake.nix` has to pin and another thing `ldd` reports on the server.
   "The server links zero engine code" is the sentence that
   makes the boundary credible. Adding a shared library to that line to express 34 instructions is
   a bad trade.
4. **Honest counter-argument, recorded:** hand-written classic BPF is write-once-read-carefully code
   where an off-by-one in a jump offset silently changes the policy. That is mitigated structurally,
   not by care: the filter is built by a loop over a table of syscall numbers (so the jump offsets
   are not hand-written per entry), and `server/tests/sandbox_test.cpp` asserts the *behaviour* of every rule from
   a child process, which is the only check that can catch a wrong offset.

Landlock needs no library either way — there is no glibc wrapper, so every implementation calls
`syscall(444/445/446)` directly. `<linux/landlock.h>` is present here; the code carries a 20-line
UAPI fallback so it also builds against older kernel headers.

### Why a denylist, not an allowlist

This is the more important decision and it is deliberate:

- A **default-deny syscall allowlist** for a CUDA process cannot be derived without exhaustive GPU
  runs covering every code path (load, prefill, decode, graph capture, expert swap, conversation
  park, OOM recovery, crash), on every driver version. Chromium's GPU-process policy is the
  cautionary example: large, per-driver, permanently maintained. This project has one maintainer and
  **no GPU in CI**. A filter nobody can regenerate is a filter that gets widened until it means
  nothing, or that bricks the engine after a driver update.
- A **targeted denylist plus an argument allowlist on the one argument that matters** is derivable
  statically (§1), reviewable in full, and cannot break a path we did not test unless that path
  calls one of 15 named syscalls.
- The honest cost is that a denylist is **unsound against a syscall we did not think of**. `io_uring`
  is the proof: it can create a socket and connect without calling `socket` or `connect`. That
  lesson generalises to any future multiplexer. The mitigations are the network namespace (which has
  no syscall-level bypass at all) and the test. This is stated plainly in `security.md` rather than
  argued away.

---

## 4. The syscall allow/deny list

`SECCOMP_RET_ALLOW` is the default; everything not named below is allowed, which is what keeps CUDA
alive and keeps `--ple-io direct` cheap.

### Argument-filtered (allowlist on one scalar)

| Syscall | nr | Rule | Action | Reason |
|---|---|---|---|---|
| `socket` | 41 | `domain == AF_UNIX (1)` → allow; anything else | `ERRNO(EAFNOSUPPORT)` | the one call that can create an outbound-capable fd. `AF_INET`, `AF_INET6`, `AF_PACKET`, `AF_NETLINK`, `AF_VSOCK`, `AF_XDP`, `AF_BLUETOOTH`, `AF_SMC`, `AF_TIPC`, `AF_ALG` and anything added later all fall on the deny side because the rule is an allowlist on the argument, not a list of families to block. `AF_UNIX` stays because CUDA's local IPC needs it (`libcuda` imports `socket`, `socketpair`, `sendmsg`, `recvmsg`). |
| `socketpair` | 53 | same | `ERRNO(EAFNOSUPPORT)` | the second way to get a socket pair; in practice only `AF_UNIX` is ever legal, but filtering it costs one comparison and removes the question. |

`EAFNOSUPPORT` rather than kill, deliberately: a library probing for IPv6 and getting
`EAFNOSUPPORT` sees a kernel without IPv6 — a case every library on earth handles. A kill would turn
a harmless probe inside closed-source CUDA into a dead engine. The denial is still visible: it is
logged once by the probe test, and the netns makes it unnecessary anyway.

**Why argument filtering is sound here.** `socket`'s first parameter is declared `int domain`. The
kernel reads it from the same register seccomp inspected and then truncates it to `int`, so comparing
the low 32 bits of `seccomp_data.args[0]` matches kernel semantics exactly — there is no
sign-extension or high-bit trick, and no TOCTOU, because the value is a scalar already copied into
the register snapshot. This is **only** true for scalars: seccomp cannot dereference a pointer, so
`connect`'s `struct sockaddr *` is invisible to it and `bind`'s address family cannot be checked.
That is precisely why the control point is `socket()` (a scalar) and not `connect()` (a pointer).

### Killed (`SECCOMP_RET_KILL_PROCESS`, falling back to `KILL_THREAD` if the kernel lacks it)

Each entry is here because it would undo one of the other two mechanisms. Nothing in §1(a)-(d) calls
any of them.

| Syscall | nr | Reason it must die |
|---|---|---|
| `ptrace` | 101 | attach to another process of this user (the server, the user's shell, an agent) and run code there — outside this filter and outside this Landlock domain. Yama `ptrace_scope=1` on the reference machine already limits it to descendants, but the engine *has* descendants (ggml's gdb helper) and the sysctl is not ours to rely on. |
| `process_vm_readv` | 310 | read another process's memory — the prompt-and-secrets asset, without ptrace. |
| `process_vm_writev` | 311 | write another process's memory — code injection without ptrace. |
| `pidfd_open` | 434 | the handle `pidfd_getfd` needs. |
| `pidfd_getfd` | 438 | **steal a file descriptor from another process**, including an already-connected socket. This is the clean bypass of a `socket()`-based egress filter and it must be closed. |
| `io_uring_setup` | 425 | an io_uring ring can perform `IORING_OP_SOCKET` and `IORING_OP_CONNECT`; the ring's operations are not syscalls, so **no seccomp filter sees them**. This is the one known general bypass of syscall-level network filtering. |
| `io_uring_enter` | 426 | submits them. |
| `io_uring_register` | 427 | registers the files. |
| `open_by_handle_at` | 304 | opens a file by inode handle, **bypassing path resolution and therefore every path-based LSM, Landlock included**. It needs `CAP_DAC_READ_SEARCH`, which the child *has inside the user namespace we create* — so without this entry, creating the netns would hand the child a Landlock bypass. |
| `name_to_handle_at` | 303 | produces the handle the above consumes. |
| `setns` | 308 | re-enter the parent's network namespace via `/proc/<pid>/ns/net` and undo the egress control. |
| `unshare` | 272 | the child has already been given the namespaces it should have; further namespace creation serves nothing here and `CLONE_NEWNS` inside the userns would let it mount over an allowlisted path. |
| `mount` | 165 | same, directly. |
| `umount2` | 166 | same, inverted. |
| `pivot_root` | 155 | same, wholesale. |
| `chroot` | 161 | re-root the process; the oldest form of the same move. |
| `open_tree` | 428 | the new mount API, which is the modern spelling of `mount(2)`: these seven do together what `mount` does alone, and denying only the old name left the whole API reachable. `clone3(CLONE_NEWUSER\|CLONE_NEWNS)` creates the user namespace first and confers `CAP_SYS_ADMIN` over the mount namespace made in the same call, so the capability is obtainable; Landlock rules are inode-keyed and matched by walking the path upward through mountpoints, so a filesystem grafted under an allowed directory would inherit its rights -- and this policy grants `/dev/shm` the right to make a mountpoint. Found in review, after an earlier version of this table claimed a nested namespace granted nothing. |
| `move_mount` | 429 | graft a detached tree into the namespace: with `open_tree`, enough on its own. |
| `fsopen` | 430 | open a filesystem context. |
| `fsconfig` | 431 | configure it. |
| `fsmount` | 432 | turn it into a mountable tree. |
| `fspick` | 433 | pick up an existing mount into a context. |
| `mount_setattr` | 442 | change mount attributes, including recursively. |
| `bpf` | 321 | load BPF programs (socket filters, tracing). |
| `perf_event_open` | 298 | sample another process. |
| `userfaultfd` | 282 | a standard kernel-exploit and TOCTOU primitive; nothing here uses it. |
| `keyctl` / `add_key` / `request_key` | 250 / 248 / 249 | the kernel keyring is a credential store this process has no business in. |

### AF_UNIX: the socket stays, the destinations go

`socket(AF_UNIX)` is permitted, because CUDA's local IPC needs it and an argument filter cannot tell
CUDA's socket from any other. Permitting the socket is not the same as permitting what it can reach,
and what it could reach was the one real escape found in this design.

**What was reachable.** A pathname AF_UNIX socket is a filesystem object. With `connect` permitted
and no Landlock right governing socket resolution, a contained engine could connect to
`/run/user/<uid>/bus`, `/run/user/<uid>/systemd/private` or `/run/dbus/system_bus_socket`,
authenticate with `AUTH EXTERNAL` (the peer credentials are genuinely ours), and ask systemd to
start a transient unit. That unit runs **outside** the sandbox, with the host network. Neither of
the other two mechanisms covers this: the network namespace is irrelevant because no network is
involved, and the seccomp filter has to keep `connect` for CUDA.

**What closes it.** `LANDLOCK_ACCESS_FS_RESOLVE_UNIX` is added to the ruleset's *handled* set and
granted on **no path at all**. Landlock's default-deny then refuses every pathname AF_UNIX
`connect`, while leaving the socket, `socketpair` and the already-open descriptors CUDA needs
untouched. Three properties make this safe to add rather than a new risk:

- Landlock requires `allowed_access ⊆ handled`, so adding a right to `handled` cannot invalidate the
  ~30 existing path rules.
- The abstract namespace (`\0`-prefixed names) has no path for a rule to name, so Landlock cannot
  see it at all; `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET` covers that variant instead.
- `ruleset_attr_size()` sends the exact struct prefix each ABI understands, so an older kernel
  returns a working ruleset rather than `E2BIG`.

`sandbox_test` asserts `unixpath=denied` under enforcement and `unixpath=ok` with the sandbox off,
so the denial cannot be a probe that merely failed to run.

**What is still open, and accepted:** `SCM_RIGHTS`. A process already compromised on this machine can
pass an open descriptor to the engine over a socket the engine already holds, and the filter never
sees it. That is outside the threat model — anything already running as your user is — and it is
recorded in `docs/security.md` rather than closed here.

**This needs Landlock ABI 9.** See "Fail closed" for what is reported below that.

### Explicitly **not** denied, with the reason

| Syscall | Why it stays |
|---|---|
| `execve`, `execveat` | **the filter is installed before `execv`** — denying them would kill the engine at launch. Landlock controls *what* can be executed; seccomp must not control *whether*. |
| `connect`, `bind`, `listen`, `accept4`, `sendto`, `sendmsg`, `recvmsg`, `setsockopt` | reachable only with a socket fd, and the only obtainable fd is `AF_UNIX`. CUDA's local IPC needs all of them, so they stay permitted at the syscall level and Landlock removes the destinations instead (below). Denying `connect` outright would break `nvidia-persistenced`/MPS paths that may exist on another box. |
| `ioctl` | the entire NVIDIA driver interface. Cannot be denied; cannot be meaningfully argument-filtered either, because the NVIDIA request codes are undocumented and their payloads are pointers. See `security.md`. |
| `mprotect` with `PROT_EXEC`, `memfd_create` | CUDA JITs PTX at runtime, so executable pages are required. Denying `memfd_create` alone would be theatre while `mprotect` must stay open. |
| `clone`, `clone3` | threads. `clone3` passes its flags in a **pointer** struct, so `CLONE_NEWUSER` cannot be denied through it, and denying it via `clone`'s scalar `arg0` would be an incomplete control that reads as a complete one. So the nested namespace is reachable and is **not** prevented. What makes it harmless is that everything it could be used *for* is dead: the independent review showed that `clone3(CLONE_NEWUSER\|CLONE_NEWNS)` grants `CAP_SYS_ADMIN` over the new mount namespace, and that the whole new mount API (`fsopen`, `fsconfig`, `fsmount`, `fspick`, `move_mount`, `open_tree`, `mount_setattr`) was reachable. It is now on the kill list below, together with `chroot`. An earlier version of this table claimed "a nested user namespace grants no capability" — that was wrong, and the test now asserts `fsopen` dies. |
| `prctl`, `seccomp`, `landlock_*` | a *second*, stricter filter can always be added by the child; seccomp filters compose by intersection and can never be relaxed. Denying them buys nothing. |
| everything else | allowed, by design. See §3 on why this is a denylist. |

### How the list was derived

1. The static import sets of the engine and all four CUDA libraries (§1, `nm -D`). Everything the
   engine or CUDA could possibly call has a libc symbol in one of those lists; nothing in the kill
   list appears in any of them. That is the primary evidence and it is reproducible in one command.
2. `grep` over `src/`, `include/` and the llama.cpp submodule for every direct `syscall()`,
   `fork`, `exec*`, `posix_spawn` and `popen` site (§1 and `grep -rn "execlp\|::fork()"`). The only
   hits are ggml's crash-time gdb helper and test files.
3. Known seccomp-bypass primitives, enumerated from the mechanism rather than from the code:
   anything that performs work on behalf of the caller without a syscall per operation (io_uring),
   anything that borrows another process's state (ptrace, `process_vm_*`, `pidfd_getfd`,
   `perf_event_open`), anything that resolves a file without a path (`*_handle_at`), and anything
   that changes the namespace the other mechanisms depend on (`setns`, `unshare`, `mount`).
4. **What is left to measure on a GPU**, and this is the gap: whether CUDA ever calls
   `socket()` with a domain other than `AF_UNIX`. §7 gives the exact experiment, and the design is
   arranged so that running it is a one-constant change with no risk to the engine.

---

## 5. The filesystem allowlist

Landlock is default-deny over the whole filesystem once `landlock_restrict_self` returns; only the
paths below are reachable. Rights are per entry.

### Fixed entries

| Path | Rights | Reason |
|---|---|---|
| `/usr/lib`, `/usr/lib64`, `/lib64` | read + exec | libstdc++, libc, the dynamic loader, `dlopen` of anything the engine or CUDA pulls in |
| `/opt/cuda/lib64` (and its `realpath`, `targets/x86_64-linux/lib`) | read + exec | `libcudart`, `libcublas`, `libcublasLt` |
| `/usr/share` | read | gettext catalogues — `libcublas` imports `dgettext`/`bind_textdomain_codeset` |
| `/etc/ld.so.cache`, `/etc/ld.so.conf.d`, `/etc/localtime` | read | loader cache; `localtime_r` in all four CUDA libraries |
| `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`, `/dev/nvidia-uvm-tools`, `/dev/nvidia-modeset` (each only if it exists) | read + write + **`IOCTL_DEV`** | the GPU. `IOCTL_DEV` is mandatory: on Landlock ABI ≥ 5 the ruleset *handles* that right, so omitting it on these nodes denies every CUDA ioctl and the engine dies in `cuInit`. This is the one entry where getting the rights wrong is instantly fatal. |
| `/dev/null`, `/dev/zero`, `/dev/urandom` | read + write | ordinary |
| `/dev/shm` | read + write + create + delete + truncate | `shm_open` in all four CUDA libraries; `src/core/pinned.cu:89`; `--shared-expert-arena` |
| `/proc/self` (resolved to `/proc/<child pid>` at rule time) | read | `/proc/self/{exe,stat,status,statm,io,task,numa_maps}`. **Not all of `/proc`** — see below |
| `/proc/meminfo`, `/proc/buddyinfo`, `/proc/sys/vm/nr_hugepages` | read | the three global `/proc` files the engine actually reads |
| `/sys/devices/system/cpu`, `/sys/devices/system/node`, `/sys/fs/cgroup`, `/sys/kernel/mm`, `/sys/dev/block`, `/sys/bus/pci/devices` | read | CPU/NUMA topology, cgroup limits, THP, rotational-disk detection. **Not all of `/sys`** |
| `/tmp` | read | the `GENI` picture-rows file the parent writes (`server/strata_context.cpp#open_pictures_file`). Read-only: the child only consumes it |
| the engine executable's `realpath` | read + exec | `execv` of the engine itself happens *after* the filter |
| the engine executable's directory | read + exec | `dlopen("libstrata_kq.so")` beside the exe (`src/prefill/mmq_kq_loader.cpp:43`) |
| the shebang interpreter, if the executable starts with `#!` | read + exec | so a script engine (`server/tests/fake-engine.py`) still runs. Read in the parent, before `fork`. This is a precise 12-line rule instead of granting all of `/usr/bin` |

**Narrowing `/proc` matters and is cheap.** Granting read on all of `/proc` would expose
`/proc/<pid>/environ` and `/proc/<pid>/cmdline` of every process this user runs — an API-key and
token leak straight into the asset the threat model protects. `/proc/self` resolves, at rule-add
time, to the forked child's own `/proc/<pid>` directory; the pid does not change across `exec`, so a
rule on the resolved path is exactly "this process's own `/proc` entry and nothing else".

### Derived entries (from the invocation, logged line by line at startup)

1. The directory of `params.model.path` (`-m`), read-only — the vocabulary GGUF.
2. Every engine argument (everything after `--`) that contains `/` **and exists**: a file is added as
   itself, a directory as a hierarchy, both read-only, both via `realpath`. This covers `--model`,
   `--pack`, `--mtp`, `--expert-profile`, `--ple-gguf`, `--control-vector`, `--vision` and the rest of
   §1(c) without the server needing to know what those options mean.
3. `--sandbox-allow-read PATH` and `--sandbox-allow-write PATH`, repeatable, for anything the
   heuristic misses — in particular the *write* options (`--expert-profile-save`,
   `--prompt-cache-root`, `--shared-expert-arena` outside `/dev/shm`), whose targets may not exist yet
   and are therefore deliberately **not** auto-allowed.

An engine argument that looks like a path but does not exist is **not** silently ignored: it is
logged as `sandbox: not allowed (does not exist): <path>`, so when the engine later fails to write
its expert profile the reason is already in the log and the fix (`--sandbox-allow-write`) is named.
A `--sandbox-allow-*` path that does not exist is a **hard startup error** — the user asked for a
specific policy and we could not install it (rule 5).

Everything else is denied, including `$HOME` outside the model directory, `~/.ssh`, `~/.claude`,
`~/.config`, the repo, `/etc/passwd`, `/etc/shadow`, `/var`, every mount under `/mnt` and
`/run/user/1000`.

**Landlock's limits, stated here so they are not discovered later:** it governs `open`, `read`,
`write`, `execute`, `create`, `delete`, `rename` and `truncate` — it does **not** govern `stat`,
`access` or `readlink`, so path *existence* stays probeable. It is path-based, which is why
`open_by_handle_at` is on the kill list. And `LANDLOCK_ACCESS_FS_REFER` (ABI 2) is needed on *both*
hierarchies for a `rename` across them, which is why the write entries carry it.

**One trap, found by running the code rather than by reading the manual:** `landlock_add_rule()`
returns `EINVAL` when `allowed_access` contains a right that only means something for a directory
(`READ_DIR`, `MAKE_*`, `REMOVE_*`, `REFER`) and the path is **not** a directory. Over half the
entries above are files — `/etc/ld.so.cache`, the engine binary, every `/dev/nvidia*` node — so the
first version of this policy failed at its first entry with a bare `EINVAL` and no indication which
path caused it. `sandbox_plan()` therefore `fstat`s each opened path and strips the directory-only
rights from any non-directory. This is the single most likely thing to be lost in a later
refactor, and `sandbox_test` catches it immediately (every assertion fails at once, with
`sandbox did not install: landlock_rule: Invalid argument`).

---

## 6. `--ple-io direct` and the cost of the filter

`--ple-io direct` does unbuffered 4 KiB `pread`s of the PLE table throughout serving, measured here
at **100,300 rows/s** on the reference machine. So the filter's per-syscall cost is a correctness
requirement.

Three facts, in order of weight:

1. **Landlock costs nothing on the hot path.** It is enforced at `open()`. Once the PLE table's fd
   exists, every `pread` on it is unchecked. The netns costs nothing at all.
2. **The seccomp filter is designed to be fully absorbed by the kernel's seccomp bitmap cache.** On
   x86-64 the kernel precomputes, per syscall number, whether every installed filter returns a
   constant action independent of the arguments; if so, the BPF program is never run for that
   syscall. Our filter inspects arguments for exactly **two** syscalls (`socket`, `socketpair`) and
   decides purely on the syscall number for everything else — so `pread64`, `mmap`, `madvise`,
   `futex`, `ioctl` and every other hot call become cached allows with **zero** BPF execution. This
   is a concrete, performance-based argument for the denylist shape: a default-deny allowlist with
   argument predicates scattered across many syscalls would defeat the cache for all of them.
3. Even without the cache, the worst-case path through the program is ~34 instructions of classic
   BPF, tens of nanoseconds against a ~10 µs unbuffered SSD read — under 0.3%.

Point 2 is a claim about kernel behaviour, not something measured here. The verification step is to
run the engine on a GPU with the sandbox on and compare decode tok/s and the PLE row rate against the
same run with `--no-sandbox` (89-92 tok/s and 100,300 rows/s on the reference machine). **A regression outside noise
means the filter shape is wrong, not that the sandbox is expensive** — and the fix would be to remove
the argument predicates from any syscall that turns out to be hot.

---

## 7. Where the filter is installed, and what is safe there

The code runs in the child between `fork` and `execv` — `server/engine.hpp#::execv(` today. That
child is single-threaded (fork gives one thread) but inherits the parent's heap locks, so **nothing
that allocates, takes a lock, or touches `errno`-reporting machinery may run there**.

### The split

- **Parent, before `fork` (`sandbox_plan`)**: everything that allocates or can fail usefully.
  `realpath()` on every allowlist path, `open(path, O_PATH|O_CLOEXEC)` for each (so a missing path is
  a parent-side error with `strerror` and the path in the message), the `uid_map`/`gid_map` strings
  formatted with `snprintf` into fixed buffers, the BPF program built into a `std::vector<sock_filter>`,
  the Landlock ABI probed, `SECCOMP_RET_KILL_PROCESS` availability probed. The result is a
  `SandboxPlan`: one `std::vector<char>` arena of NUL-terminated paths, a vector of
  `{offset, rights, fd}`, the filter, and two fixed `char[48]` buffers.
- **Child, after `fork` (`sandbox_apply`)**: only `prctl`, `syscall`, `open`, `write`, `close`,
  `_exit` — all async-signal-safe — reading the plan through `.data()`/`.size()` and never allocating.
  The plan's path fds are already open, so the child does no path resolution at all.

### The order, and why each step is where it is

```
1  prctl(PR_SET_NO_NEW_PRIVS, 1)        both Landlock and seccomp need it without CAP_SYS_ADMIN
2  unshare(CLONE_NEWUSER|CLONE_NEWNET)  NEWUSER first in the same call grants the caps for NEWNET
3  write "deny" -> /proc/self/setgroups  must precede gid_map, or the gid_map write is refused
4  write "<uid> <uid> 1" -> uid_map      identity map: the child is never uid 0, even in its own ns
5  write "<gid> <gid> 1" -> gid_map
6  landlock_create_ruleset / add_rule x N / landlock_restrict_self
7  seccomp(SECCOMP_SET_MODE_FILTER)      last: it kills `unshare`, which step 2 needed
8  close_range(3, ~0u, 0)                drop every inherited fd above stderr
9  execv                                 allowed by the filter; permitted by Landlock *by path*
```

Steps 3-5 write to `/proc/self/*`, which must happen **before** step 6 (Landlock does not allow
writes there) and before step 7 (which does not deny them, but the ordering is clearer stated).
Step 7 must be last because it kills `unshare` and `mount`. Step 8 is after 7 because the Landlock
path fds are needed in 6; `execv` closes them anyway via `O_CLOEXEC`, and `close_range` is belt and
braces for anything the parent left open (today, nothing — `server_http_context::init` does not bind;
the listening socket is created in `start()`, which runs *after* `start_engine`. Worth keeping
correct as that order may change.)

### Error reporting back to the parent

A `pipe2(O_CLOEXEC)` created before `fork`. On failure the child writes one fixed 8-byte record
`{uint16 stage; uint16 fatal; int32 saved_errno}` with a single `write()` and calls `_exit(126)`. The
second field is **not** padding: it is what decides whether the server refuses to start or merely
logs a degradation. The
parent reads it: 8 bytes means "the sandbox failed at this stage with this errno", and the parent
turns `stage` into a name from a static table and reports
`could not contain the engine: <stage>: <strerror>`. Note the channel carries non-fatal records too,
so end of file *after* one record is the normal degraded success, not a failure.
**End of file with no bytes means `execv`
succeeded** — the fd is `O_CLOEXEC`, so a successful exec closes it. No strings cross the boundary,
so nothing is formatted in the child.

### Fail closed

Two states only, never three:

- **Enforced** (default). If `sandbox_available()` says the mechanisms are absent, or `sandbox_plan`
  cannot resolve a `--sandbox-allow-*` path, or `sandbox_apply` fails at any stage, the server
  **exits non-zero before the engine serves anything**. No warning-and-continue.

  Inside the enforced state, a Landlock ABI older than this build knows is a degradation rather than
  a failure: unsupported *rights* are masked out of the handled set. The rights the canary test
  depends on (`READ_FILE`, `READ_DIR`, `WRITE_FILE`, `EXECUTE`) are ABI 1 and exist wherever
  Landlock exists, so **the file-access half of the sandbox is never weakened** by the downgrade.
  Two rights above ABI 1 are load-bearing, and they are the exception that must be read carefully:
  `LANDLOCK_ACCESS_FS_RESOLVE_UNIX` is **ABI 9** and `LANDLOCK_SCOPE_SIGNAL` is **ABI 6**. Below
  those, the AF_UNIX path closure and the signal scope are simply absent. Because that is a security
  control going quiet, it is reported and not inferred: the startup line reads `ENFORCED WITHOUT THE
  AF_UNIX PATH CLOSURE (landlock abi < 9)`, `/health` and `/props` report
  `enforced-no-unix-path` rather than `enforced`, and `sandbox_test` prints a `skip:` line naming
  the missing right instead of asserting a denial the kernel cannot give.

  **The kernel floor, stated plainly:** full enforcement as described in this document needs
  **Landlock ABI 9 or newer**. ABI 6–8 gets everything except the AF_UNIX path closure; ABI 1–5 also
  loses the signal scope. The server still runs — the escape below was open on every kernel before
  this closure existed, so refusing to start would remove a configuration that works today — but it
  says which of the two it is.
  Losing the netns because unprivileged user namespaces are disabled is also inside the enforced
  state, because seccomp still denies every internet socket; it is logged as
  `sandbox: unshare: <strerror>; egress is denied by the syscall filter only`, and the startup line
  then reads `ENFORCED WITHOUT A NETWORK NAMESPACE`, with `/health` and `/props` reporting
  `enforced-no-netns` rather than `enforced`.
- **Off**, only via `--no-sandbox`.

### The opt-out: one flag, and why it exists

Arguing against having one is tempting and wrong. Without an opt-out, a user on a kernel without
Landlock cannot run the binary at all, and will run an older release or patch the check out — both
worse than a visible flag. So: exactly one, designed to be impossible to set by accident and
impossible to miss.

- **`--no-sandbox`, a server flag, never an environment variable.** An env var is inherited,
  gets set in a systemd unit or a shell rc where no reviewer looks, and does not appear in `ps`. A
  flag appears in `ps`, in the unit's `ExecStart`, and in the user's own shell history.
- **No middle setting.** There is no `--sandbox=best-effort`. "A verification that cannot run is a
  failure, never a warning" (rule 5) means the only alternative to enforcement is an explicit,
  recorded decision not to enforce.
- **Loud at startup** — four lines to stderr, unconditionally, not at `LOG_WRN` level where a quiet
  logger could drop them. The exact text is in `server/strata_context.cpp`.
- **Visible over the API.** `/props` and `/health` gain `"sandbox": "enforced" | "enforced-no-netns" | "off" | "unavailable"`.
  This is the part that makes the opt-out *testable*: `e2e.sh` can assert the field, and an operator
  or an agent can check at runtime whether the server it is talking to is contained. A security
  state that is only in a startup log is a security state nobody checks.

---

## 8. Portability: how this degrades on Darwin without `#ifdef` sprawl

One `#if defined(__linux__)` in the whole change, around the body of `strata_sandbox.cpp`. The
header declares the same four functions on every platform. On a non-Linux build,
`sandbox_available()` returns `false` with `why = "not available on this platform"`, `sandbox_plan()`
returns a disabled plan, and `sandbox_apply()` returns 0 without doing anything. **No caller needs a
conditional**, which is the point: `engine.hpp` and `strata_context.cpp` compile identically on both
platforms.

The policy difference is one `#if` in `strata_context.cpp`'s startup check, and it is a deliberate
scoping decision, not a fail-open:

- **On Linux, an absent mechanism is a runtime failure.** The kernel has seccomp and Landlock or it
  does not; if it does not, something is wrong with the deployment and the engine must not start.
- **On Darwin, the mechanism does not exist at all**, and that is a compile-time fact about the
  platform. Refusing to start there would turn "the Metal tree is kept" (README rule 7) into
  "macOS, deleted". So the Darwin build prints, once, unconditionally, at startup:
  `sandbox: not available on this platform; the engine runs uncontained` and reports
  `"sandbox": "unavailable"` over `/props`. That is not failing open — failing open is a check that
  silently passes. This is an honest, machine-readable statement that the platform is unprotected,
  and it matches the project's existing claim level for macOS: "compiles, upstream code intact",
  never "verified here".

A real Darwin implementation is separate, later work with its own test: the equivalents are
`sandbox_init` (Seatbelt — deprecated API, no stable successor for this use) plus the hardened-runtime
entitlements, which are a signing-time property rather than a pre-`exec` call, and `/dev/nvidia*` is
meaningless there. It does **not** belong behind an `#ifdef` in this file.

---

## 9. Not built: the same containment for the server

The gap this leaves is in the **server**, and it is deliberate: `aegis` imports `connect` and
`getaddrinfo` from
cpp-httplib's unused client half. The parent cannot have a netns (it must bind 127.0.0.1) and cannot
have the child's Landlock policy (it reads the GGUF, the chat template, `--mmproj`), but it *can*
install a second, tiny seccomp filter **after `ctx_http.start()` has bound and listened**, denying
`socket`, `socketpair`, `connect`, `sendto`, `sendmsg` and `sendmmsg` outright — a server needs none
of them once its listener exists, because new connections arrive through `accept4` on the fd it
already holds.

That would make security.md's invariant 1 structural for *both* processes, and is roughly 20 lines
reusing this file's filter builder. It is left out on purpose rather than forgotten: it is a different
process with a different failure mode, it needs its own end-to-end assertions (does cpp-httplib ever
create a socket per connection? the test harness can answer that without a GPU), and folding it in
would make the existing test ambiguous about which process was contained.


---

## 10. What was measured, and what was not

`server/strata_sandbox.cpp` and `server/tests/sandbox_test.cpp` compile clean under
`g++ -std=c++20 -Wall -Wextra`. Reproduce the block below with `make test`, which builds and runs
`build-server/bin/sandbox_test`; it was last re-derived on the reference machine on 2026-11-08. The
path count is a property of the invocation, not of the code — a real model directory changes it — so
treat it as this run's number, while the 75-instruction filter size is fixed by the code and is
checked against the built filter by `tools/check_docs.py`.

```
sandbox: landlock abi 10, 30 paths, network namespace, seccomp (75 instructions)
probe: inet=denied inet6=denied netlink=denied unix=ok
canary=denied model=ok procself=ok proc_meminfo=ok proc_other=denied
ifaces=1
unixpath=denied signal=denied
probe: ptrace=killed
probe: io_uring=killed
probe: open_tree      killed
probe: move_mount     killed
probe: fsopen         killed
probe: fsconfig       killed
probe: fsmount        killed
probe: fspick         killed
probe: mount_setattr  killed
filter: 75 instructions
probe(off): inet=ok inet6=ok netlink=ok unix=ok
canary=ok model=ok procself=ok proc_meminfo=ok proc_other=ok
ifaces=<the host's interface count>
unixpath=ok signal=ok
probe(off): ptrace=ran
probe(off): io_uring=ran
probe(off): mount_api=ran
sandbox_test: 59 assertions passed
```

The `probe(off)` lines are the inversion, and they are the point: every `denied` above is paired with
an `ok` from the same probe with the sandbox off, so a `denied` cannot be a probe that simply failed
to run. `ifaces` in that arm is the host's live interface count and changes between boots, so it is
the one number here that can never be quoted.

So on this kernel, measured and not argued: the user namespace and network namespace install
unprivileged (`ifaces=1`, only `lo`), Landlock ABI 10 accepts the 30-entry allowlist, the canary
outside it is refused while the model file inside it reads, `/proc` is narrowed so another process's
command line is refused, `AF_INET`/`AF_INET6`/`AF_NETLINK` sockets are refused while `AF_UNIX`
works, and `ptrace` and `io_uring_setup` end the process with `SIGSYS`. The `probe(off)` line is the
same binary with `enabled = false`: every result inverts, which is what makes the rest
non-vacuous.

Deliberate breaks 2, 4 and 5 of the seven listed in `server/tests/sandbox_test.cpp`'s header were
each applied and each produced exactly its named failure and nothing else.

**What this block does not cover: CUDA, a GPU, a model file, or the real engine.** Those were
exercised separately against the engine on the reference machine, and the list of what remains
unknown is in `security.md`.
