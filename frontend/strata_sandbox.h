// aegis-strata: containment of the engine child process, installed by the parent between fork and exec.
//
// The front end spawns the engine as a child (frontend/engine.hpp) and links none of its code, so the process
// boundary is real. This file makes it enforced rather than incidental (THREAT_MODEL.md invariant 1): the child gets a
// network namespace with only `lo` where the kernel allows one, a seccomp filter that refuses `socket()` for every
// domain but AF_UNIX, and a Landlock ruleset that reduces its filesystem to an allowlist. Nothing here needs a
// privilege and nothing here links a library: seccomp is a few dozen hand-written BPF instructions and Landlock has no
// glibc wrapper, so both are raw syscalls (see docs/SANDBOX.md §3 for the libseccomp decision).
//
// The work is split either side of fork, because almost nothing is safe to call in a forked child of a process
// that has a heap:
//
//   sandbox_plan()   parent, before fork. Resolves and opens every allowlist path, builds the BPF program, probes
//                    the kernel. Allocates freely. Every failure is reported here, with the path and strerror.
//   sandbox_apply()  child, after fork. Reads the plan and calls prctl/unshare/landlock/seccomp. Allocates
//                    nothing, formats nothing, throws nothing: only async-signal-safe calls.
//
// Failures in the child travel back over a CLOEXEC pipe as fixed 8-byte records, so no string is built there.
// A successful exec closes the pipe, which the parent reads as end of file.
//
// Linux only. On any other platform every function below is a stub that reports the sandbox unavailable, so no
// caller needs a conditional (docs/SANDBOX.md §8).
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace strata_frontend {

// Where in the sequence a failure happened. Travels over the pipe as a number; named by sandbox_stage_name().
enum class SandboxStage : std::uint16_t {
    None = 0,
    NoNewPrivs,
    Unshare,         // non-fatal: reported as a note, the syscall filter still denies every internet socket
    SetGroups,
    UidMap,
    GidMap,
    LandlockCreate,
    LandlockOpen,
    LandlockRule,
    LandlockRestrict,
    Seccomp,
    // The positive acknowledgement, written last, with `err_no` carrying 1 when the network namespace was
    // installed and 0 when the kernel refused one. Its ABSENCE is a failure: before it existed the parent
    // inferred success from end-of-file, so a record lost for any reason read as "fully contained" -- the
    // inverse of this project's rule that a verification which cannot run is a failure.
    Installed,
};

// What the child may touch. Built by sandbox_default_config() and then widened only by --sandbox-allow-*.
struct SandboxConfig {
    bool enabled = true;   // --no-sandbox turns this off, loudly

    std::vector<std::string> read;       // read a file, list a directory
    std::vector<std::string> read_exec;  // the above plus execute: the engine, its directory, the library paths
    std::vector<std::string> write;      // the above plus create, delete, truncate and rename
    std::vector<std::string> device;     // read, write and ioctl: /dev/nvidia*. Without the ioctl right CUDA dies
};

// One classic-BPF instruction. Layout-identical to `struct sock_filter`; kept here so this header pulls in no
// kernel header (the .cpp static_asserts the equivalence).
struct SandboxFilterInsn {
    std::uint16_t code;
    std::uint8_t  jt;
    std::uint8_t  jf;
    std::uint32_t k;
};

// Everything the child needs, in a shape it can read without allocating: paths live in one NUL-separated arena and
// are referred to by offset, and every path is already open as an O_PATH descriptor.
struct SandboxPlan {
    struct Rule {
        std::uint64_t rights = 0;
        int           fd     = -1;  // -1: the child opens `name` itself (only /proc/self, which the parent cannot)
        std::uint32_t name   = 0;   // offset into `names`
    };

    bool enabled      = false;
    bool want_netns   = false;
    int  landlock_abi = 0;

    std::vector<char>             names;
    std::vector<Rule>             rules;
    std::vector<SandboxFilterInsn> filter;

    // One line per rule actually installed, resolved, naming the argument it came from when they differ. The
    // policy's audit trail: a symlink or a `..` means the argument and the granted subtree are different things.
    std::vector<std::string> granted;

    char uid_map[48] = {0};  // "<uid> <uid> 1\n", formatted before fork
    char gid_map[48] = {0};
};

// What the filter this code builds actually contains.
//
// A document that states these numbers is a copy of a fact that lives in build_filter(), so
// tools/check_docs.py reads them from here instead of re-deriving the arithmetic in Python. That
// matters: a Python model of the builder keeps returning the old length when the builder's FIXED
// part changes -- another socket family, one more register load -- so a document stating the old
// number would pass while being false. Deriving it from a second implementation of the same sum is
// exactly the drift this project treats as a defect.
//
// Needs no kernel: the BPF program is assembled in user space, which is why `sandbox_test --facts`
// answers on a machine that cannot enforce the sandbox at all.
struct SandboxFilterFacts {
    std::size_t instructions = 0;  // the assembled BPF program's length
    std::size_t killed       = 0;  // entries on the kill list
};
SandboxFilterFacts sandbox_filter_facts();

// True when this kernel has everything the enforced state needs. `why` names the first thing missing.
// A false here is a startup failure unless --no-sandbox was given: a verification that cannot run is a
// failure, never a warning (README rule 6).
bool sandbox_available(std::string & why);

// The default policy: the fixed system paths the engine and CUDA need, the engine executable (and, when it is a
// script, its interpreter), the model directory, and every engine argument that names a path that exists.
// `notes` collects one line per decision, for the startup log: the policy must be inspectable.
SandboxConfig sandbox_default_config(const std::string & engine_exe,
                                     const std::string & model_path,
                                     const std::vector<std::string> & engine_args,
                                     const std::vector<std::string> & extra_read,
                                     const std::vector<std::string> & extra_write,
                                     std::vector<std::string> & notes);

// Parent side. False on any failure, with `err` naming it; the caller must then refuse to start the engine.
bool sandbox_plan(const SandboxConfig & cfg, SandboxPlan & plan, std::string & err);

// One line for the startup log, e.g. "landlock abi 10, 34 paths, network namespace, seccomp (75 instructions)".
// `netns_installed` is the OUTCOME, which only the forked child knows, so the caller must pass what came back
// over the pipe. Describing the plan's *request* as though it were the result is how a server ends up logging
// "network namespace" on a kernel that refused one.
std::string sandbox_describe(const SandboxPlan & plan, bool netns_installed);

// Child side, between fork and exec. 0: installed, carry on to execv. -1: a record was written to `err_fd` and the
// caller must _exit() without exec'ing. Async-signal-safe: no allocation, no stdio, no locks.
int sandbox_apply(const SandboxPlan & plan, int err_fd) noexcept;

// Parent side. Reads one record written by sandbox_apply(). True when a record was read; `fatal` false means it is
// a note to log (today: the network namespace could not be created) and the caller should keep reading.
bool sandbox_read_record(int err_fd, SandboxStage & stage, int & err_no, bool & fatal);

const char * sandbox_stage_name(SandboxStage stage);

} // namespace strata_frontend
