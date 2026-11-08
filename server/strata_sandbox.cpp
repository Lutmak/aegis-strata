// aegis-strata: containment of the engine child process. See strata_sandbox.h for the split either side of fork
// and docs/sandbox.md for why the policy is shaped this way.
#include "strata_sandbox.h"

#include <cerrno>
#include <cstddef>
#include <cstdio>
#include <cstring>

#include <poll.h>
#include <unistd.h>

namespace aegis {

namespace {

struct Record {           // what the child writes to the error pipe: fixed size, so nothing is formatted there
    std::uint16_t stage;
    std::uint16_t fatal;
    std::int32_t  err;
};
static_assert(sizeof(Record) == 8, "the error record must stay fixed at 8 bytes");

const char * const kStageNames[] = {
    "none", "no_new_privs", "unshare", "setgroups", "uid_map", "gid_map",
    "landlock_create", "landlock_open", "landlock_rule", "landlock_restrict", "seccomp", "installed",
};

} // namespace

// The child's whole pre-exec sequence is a few dozen syscalls, so ten seconds is four orders of magnitude of
// slack and still bounded.
constexpr int kRecordTimeoutMs = 10000;

const char * sandbox_stage_name(SandboxStage stage) {
    const auto i = (std::size_t) stage;
    return i < sizeof kStageNames / sizeof kStageNames[0] ? kStageNames[i] : "unknown";
}

bool sandbox_read_record(int err_fd, SandboxStage & stage, int & err_no, bool & fatal) {
    Record r{};
    std::size_t got = 0;
    while (got < sizeof r) {
        // A deadline, because everything the child does between fork and exec takes microseconds. Without one, a
        // child stopped by a signal or frozen by a cgroup leaves the parent blocked here for ever: no listener,
        // no log line, and a process a service manager sees as live but which never binds.
        struct pollfd pfd{};
        pfd.fd     = err_fd;
        pfd.events = POLLIN;
        const int ready = ::poll(&pfd, 1, kRecordTimeoutMs);
        if (ready < 0 && errno == EINTR) {
            continue;
        }
        if (ready <= 0) {
            errno = ready == 0 ? ETIMEDOUT : errno;
            return false;   // treated by the caller as "no acknowledgement arrived", which is a failure
        }
        const ssize_t n = ::read(err_fd, (char *) &r + got, sizeof r - got);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            return false;   // end of file: the child reached execv, or died before it could report
        }
        got += (std::size_t) n;
    }
    stage  = (SandboxStage) r.stage;
    err_no = r.err;
    fatal  = r.fatal != 0;
    return true;
}

} // namespace aegis

// ---- Linux ---------------------------------------------------------------------------------------------------------
#if defined(__linux__)

#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <sched.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>

#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>

#include <cstdlib>

namespace aegis {

namespace {

static_assert(sizeof(SandboxFilterInsn) == sizeof(struct sock_filter), "SandboxFilterInsn must match sock_filter");

// ---- kernel interfaces we call by number -----------------------------------------------------------------------
// Landlock has no glibc wrapper and its UAPI struct has grown a field per ABI, so the structs are declared here
// rather than included: that way the `size` argument passed to the kernel is exactly the one this code intends,
// and the file builds against older kernel headers as well.

#ifndef __NR_landlock_create_ruleset
#define __NR_landlock_create_ruleset 444
#endif
#ifndef __NR_landlock_add_rule
#define __NR_landlock_add_rule 445
#endif
#ifndef __NR_landlock_restrict_self
#define __NR_landlock_restrict_self 446
#endif
#ifndef __NR_close_range
#define __NR_close_range 436
#endif
#ifndef SECCOMP_RET_KILL_PROCESS
#define SECCOMP_RET_KILL_PROCESS 0x80000000U
#endif
#ifndef SECCOMP_RET_KILL_THREAD
#define SECCOMP_RET_KILL_THREAD 0x00000000U
#endif
#ifndef SECCOMP_GET_ACTION_AVAIL
#define SECCOMP_GET_ACTION_AVAIL 2
#endif

// x86-64 numbers, for building against kernel headers older than the syscalls on the kill list.
#ifndef __NR_io_uring_setup
#define __NR_io_uring_setup 425
#endif
#ifndef __NR_io_uring_enter
#define __NR_io_uring_enter 426
#endif
#ifndef __NR_io_uring_register
#define __NR_io_uring_register 427
#endif
#ifndef __NR_pidfd_open
#define __NR_pidfd_open 434
#endif
#ifndef __NR_pidfd_getfd
#define __NR_pidfd_getfd 438
#endif

constexpr std::uint32_t kLandlockVersionQuery = 1;  // LANDLOCK_CREATE_RULESET_VERSION
constexpr int           kRulePathBeneath      = 1;  // LANDLOCK_RULE_PATH_BENEATH

struct LlRulesetAttr {
    std::uint64_t handled_access_fs;
    std::uint64_t handled_access_net;   // ABI 4; not sent to an older kernel (see ruleset_attr_size())
    std::uint64_t scoped;               // ABI 6; likewise
};

struct LlPathBeneathAttr {
    std::uint64_t allowed_access;
    std::int32_t  parent_fd;
} __attribute__((packed));

// Landlock filesystem rights, by bit position. Named locally so the mask is explicit and does not change meaning
// when the installed kernel headers do.
constexpr std::uint64_t kLlExecute    = 1ULL << 0;
constexpr std::uint64_t kLlWriteFile  = 1ULL << 1;
constexpr std::uint64_t kLlReadFile   = 1ULL << 2;
constexpr std::uint64_t kLlReadDir    = 1ULL << 3;
constexpr std::uint64_t kLlRemoveDir  = 1ULL << 4;
constexpr std::uint64_t kLlRemoveFile = 1ULL << 5;
constexpr std::uint64_t kLlMakeChar   = 1ULL << 6;
constexpr std::uint64_t kLlMakeDir    = 1ULL << 7;
constexpr std::uint64_t kLlMakeReg    = 1ULL << 8;
constexpr std::uint64_t kLlMakeSock   = 1ULL << 9;
constexpr std::uint64_t kLlMakeFifo   = 1ULL << 10;
constexpr std::uint64_t kLlMakeBlock  = 1ULL << 11;
constexpr std::uint64_t kLlMakeSym    = 1ULL << 12;
constexpr std::uint64_t kLlRefer      = 1ULL << 13;  // ABI 2: rename/link across hierarchies
constexpr std::uint64_t kLlTruncate   = 1ULL << 14;  // ABI 3
constexpr std::uint64_t kLlIoctlDev   = 1ULL << 15;  // ABI 5: ioctl on a device file
// ABI 9. Without this bit in `handled`, Landlock does not govern connect() to a pathname AF_UNIX socket at all,
// and the filter permits connect() because CUDA needs it. A contained engine could therefore reach
// /run/user/<uid>/bus, /run/user/<uid>/systemd/private and /run/dbus/system_bus_socket, authenticate with
// AUTH EXTERNAL, and ask systemd to start a transient unit -- which runs outside this sandbox with the host
// network. Measured: all three answered `OK <guid>` before this bit was handled. No path is granted it, so every
// such connect is now refused; if a future CUDA feature needs one (MPS, nvidia-persistenced), grant it on that
// exact socket rather than widening this.
constexpr std::uint64_t kLlResolveUnix = 1ULL << 16;

// ABI 6 scopes. These restrict what the sandboxed process may reach OUTSIDE its own Landlock domain; they do not
// affect the supervisor signalling the child, because the supervisor is not in the domain.
constexpr std::uint64_t kLlScopeAbstractUnix = 1ULL << 0;   // LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET
constexpr std::uint64_t kLlScopeSignal       = 1ULL << 1;   // LANDLOCK_SCOPE_SIGNAL

// ABI 4 network rights. Handled with no rule added, so every TCP bind and connect is refused. This is a second
// lock only: it is TCP-only, so it cannot be the control for invariant 1 (UDP would walk straight past it).
constexpr std::uint64_t kLlNetBindTcp    = 1ULL << 0;
constexpr std::uint64_t kLlNetConnectTcp = 1ULL << 1;

// The rights granted per allowlist kind.
constexpr std::uint64_t kRightsRead  = kLlReadFile | kLlReadDir;
constexpr std::uint64_t kRightsExec  = kRightsRead | kLlExecute;
constexpr std::uint64_t kRightsWrite = kRightsRead | kLlWriteFile | kLlTruncate | kLlMakeReg | kLlMakeDir |
                                       kLlRemoveFile | kLlRemoveDir | kLlMakeFifo | kLlMakeSock | kLlRefer;
// /dev/nvidia*: without kLlIoctlDev every CUDA ioctl is refused and the engine dies in cuInit.
constexpr std::uint64_t kRightsDevice = kLlReadFile | kLlWriteFile | kLlIoctlDev;

// Rights that only mean something for a directory. landlock_add_rule() returns EINVAL when any of them is asked
// for on a path that is not one, so a rule on a file (/etc/ld.so.cache, the engine binary, /dev/nvidia0) must
// have them stripped. Found the hard way: without this every plan fails at the first non-directory entry.
std::string describe_rule(const std::string & resolved, const std::string & from, std::uint64_t rights) {
    std::string what;
    const auto flag = [&what, rights](std::uint64_t bit, const char * name) {
        if ((rights & bit) != 0) {
            what += what.empty() ? "" : "+";
            what += name;
        }
    };
    flag(kLlReadFile | kLlReadDir, "read");
    flag(kLlExecute, "exec");
    flag(kLlWriteFile | kLlMakeReg | kLlMakeDir, "write");
    flag(kLlIoctlDev, "ioctl");
    return resolved + (from == resolved ? "" : "   (from " + from + ")") + "   [" + (what.empty() ? "none" : what) + "]";
}

constexpr std::uint64_t kDirectoryOnlyRights = kLlReadDir | kLlRemoveDir | kLlRemoveFile | kLlMakeChar |
                                               kLlMakeDir | kLlMakeReg | kLlMakeSock | kLlMakeFifo |
                                               kLlMakeBlock | kLlMakeSym | kLlRefer;

std::uint64_t landlock_handled_fs(int abi) {
    std::uint64_t h = kLlExecute | kLlWriteFile | kLlReadFile | kLlReadDir | kLlRemoveDir | kLlRemoveFile |
                      kLlMakeChar | kLlMakeDir | kLlMakeReg | kLlMakeSock | kLlMakeFifo | kLlMakeBlock | kLlMakeSym;
    if (abi >= 2) h |= kLlRefer;
    if (abi >= 3) h |= kLlTruncate;
    if (abi >= 5) h |= kLlIoctlDev;
    if (abi >= 9) h |= kLlResolveUnix;
    return h;
}

std::size_t ruleset_attr_size(int abi) {
    // One field per ABI step, so the size is the prefix the kernel understands: fs only (ABI 1-3), plus net
    // (ABI 4-5), plus scoped (ABI 6+). Sending a longer struct than the kernel knows is E2BIG.
    if (abi >= 6) return sizeof(LlRulesetAttr);                 // fs + net + scoped
    if (abi >= 4) return 2 * sizeof(std::uint64_t);             // fs + net
    return sizeof(std::uint64_t);                               // fs
}

int landlock_abi() {
    const long r = ::syscall(__NR_landlock_create_ruleset, nullptr, (std::size_t) 0, kLandlockVersionQuery);
    return r < 0 ? -1 : (int) r;
}

bool seccomp_action_available(std::uint32_t action) {
    return ::syscall(__NR_seccomp, (unsigned) SECCOMP_GET_ACTION_AVAIL, 0u, &action) == 0;
}

// ---- the seccomp filter ----------------------------------------------------------------------------------------
// Syscalls that must not run. Every one of them would undo one of the other two mechanisms; none appears in the
// import set of the engine or of any of the four CUDA libraries it loads (docs/sandbox.md §4).
// The numbers are back-filled rather than #ifdef'd away: a guard that drops an entry on a host with older
// kernel headers would shrink the denylist silently. x86-64 syscall numbers are ABI-stable, so hard-coding is safe.
#ifndef __NR_fsopen
#define __NR_fsopen 430
#endif
#ifndef __NR_fsconfig
#define __NR_fsconfig 431
#endif
#ifndef __NR_fsmount
#define __NR_fsmount 432
#endif
#ifndef __NR_fspick
#define __NR_fspick 433
#endif
#ifndef __NR_move_mount
#define __NR_move_mount 429
#endif
#ifndef __NR_open_tree
#define __NR_open_tree 428
#endif
#ifndef __NR_mount_setattr
#define __NR_mount_setattr 442
#endif

const int kKilled[] = {
    __NR_ptrace,              // run code in another process of this user, outside this filter
    __NR_process_vm_readv,    // read another process's memory without ptrace
    __NR_process_vm_writev,   // write it
    __NR_pidfd_open,          // the handle pidfd_getfd needs
    __NR_pidfd_getfd,         // steal a connected socket from another process: the clean bypass of a socket() filter
    __NR_io_uring_setup,      // ring operations are not syscalls, so no seccomp filter sees IORING_OP_CONNECT
    __NR_io_uring_enter,
    __NR_io_uring_register,
    __NR_name_to_handle_at,   // produces an inode handle...
    __NR_open_by_handle_at,   // ...that opens a file without path resolution, bypassing every path-based LSM
    __NR_setns,               // re-enter the parent's network namespace and undo the egress control
    __NR_unshare,             // the namespaces this child should have are already installed
    __NR_mount,
    __NR_umount2,
    __NR_pivot_root,
    // The new mount API is the modern spelling of mount(2), and clone3(CLONE_NEWUSER|CLONE_NEWNS) is the modern
    // spelling of the unshare above: the user namespace is created first and confers CAP_SYS_ADMIN over the mount
    // namespace made in the same call. Denying only the old names left the whole API reachable. Landlock rules are
    // inode-keyed and matched by walking the path upward through mountpoints, so a filesystem mounted under an
    // allowed directory would inherit its rights -- and this policy grants /dev/shm the right to make one.
    __NR_fsopen,
    __NR_fsconfig,
    __NR_fsmount,
    __NR_fspick,
    __NR_move_mount,
    __NR_open_tree,
    __NR_mount_setattr,
    __NR_chroot,
    __NR_bpf,
    __NR_perf_event_open,
    __NR_userfaultfd,
    __NR_keyctl,
    __NR_add_key,
    __NR_request_key,
};

// A silently shorter kill list is a silently weaker sandbox, so its length is a build-time fact. Changing it
// means changing this number, docs/sandbox.md's table, and the filter length the test asserts -- deliberately.
constexpr std::size_t kKilledCount = sizeof kKilled / sizeof kKilled[0];
static_assert(kKilledCount == 29, "the kill list changed size: update this count, the table in docs/sandbox.md, and sandbox_test's expected filter length");

constexpr int kAfUnix = 1;   // AF_UNIX: CUDA's local IPC. Every other domain, present or future, is refused.

// `kDenyAction` and `kKillAction` are the two constants to change to SECCOMP_RET_LOG when deriving the list
// against the real engine on a GPU: the filter then refuses nothing and dmesg names every call it would have
// refused. docs/sandbox.md §4 "How the list was derived" has the procedure.
constexpr std::uint32_t kDenyAction = SECCOMP_RET_ERRNO | (EAFNOSUPPORT & SECCOMP_RET_DATA);

void build_filter(std::vector<SandboxFilterInsn> & out, std::uint32_t kill_action) {
    const auto insn = [&out](std::uint16_t code, std::uint8_t jt, std::uint8_t jf, std::uint32_t k) {
        out.push_back(SandboxFilterInsn{code, jt, jf, k});
    };
    const auto load  = [&insn](std::uint32_t off) { insn(BPF_LD | BPF_W | BPF_ABS, 0, 0, off); };
    const auto ret   = [&insn](std::uint32_t action) { insn(BPF_RET | BPF_K, 0, 0, action); };

    // The filter is inherited across fork and kept across exec, and it is written so that every syscall other than
    // socket/socketpair is decided on its number alone. That is what lets the kernel's seccomp bitmap cache answer
    // for the hot calls (pread64, mmap, ioctl, futex) without running a single instruction - which is why
    // --ple-io direct at ~100,000 reads/s is unaffected (docs/sandbox.md §6).

    // Refuse anything that is not the x86-64 ABI: a filter written for one syscall table means nothing against
    // another, and i386/x32 reach the kernel through different numbers.
    load((std::uint32_t) offsetof(struct seccomp_data, arch));
    insn(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, AUDIT_ARCH_X86_64);
    ret(kill_action);

    load((std::uint32_t) offsetof(struct seccomp_data, nr));
    insn(BPF_JMP | BPF_JGE | BPF_K, 0, 1, 0x40000000u);   // the x32 numbers carry bit 30
    ret(kill_action);

    // socket(domain, ...) and socketpair(domain, ...): an allowlist on the one argument that matters.
    // `domain` is declared `int`, and the kernel truncates it to int as well, so comparing the low 32 bits of
    // args[0] matches kernel semantics exactly. This works only because the argument is a scalar: seccomp cannot
    // dereference a pointer, which is why connect()'s struct sockaddr * is not the control point here.
    for (const int nr : {__NR_socket, __NR_socketpair}) {
        insn(BPF_JMP | BPF_JEQ | BPF_K, 0, 3, (std::uint32_t) nr);
        load((std::uint32_t) offsetof(struct seccomp_data, args));   // args[0], the low word of `domain`
        insn(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, kAfUnix);
        ret(kDenyAction);
        load((std::uint32_t) offsetof(struct seccomp_data, nr));   // the accumulator held args[0]; put nr back
    }

    for (const int nr : kKilled) {
        insn(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, (std::uint32_t) nr);
        ret(kill_action);
    }

    ret(SECCOMP_RET_ALLOW);
}

// ---- helpers for the parent side -------------------------------------------------------------------------------

bool path_exists(const std::string & p) {
    struct stat st{};
    return !p.empty() && ::stat(p.c_str(), &st) == 0;
}

// Every directory it would be absurd to grant: the filesystem root, and this user's home as named by each
// source that can name it.
//
// `$HOME` is NOT authoritative and must not be the only source. It is an ordinary environment variable, so a
// value that is absolute but wrong -- `HOME=/`, or a systemd unit with `Environment=HOME=/` -- used to make the
// whole check vacuous: with `home = "/"`, `/home/<user>` is neither equal to it nor an ancestor of it, so the
// rule was accepted and the engine got the home directory anyway. The passwd entry is the authoritative answer
// and is not environment-influenced, so both are collected and the union is forbidden. Adding a source can only
// ever make this stricter.
std::vector<std::string> forbidden_roots() {
    std::vector<std::string> out;
    const auto add = [&out](const char * dir) {
        if (dir == nullptr || dir[0] != '/') {
            return;
        }
        char resolved[PATH_MAX];
        if (::realpath(dir, resolved) == nullptr) {
            return;
        }
        for (const std::string & s : out) {
            if (s == resolved) {
                return;
            }
        }
        out.emplace_back(resolved);
    };
    add("/");
    add(::getenv("HOME"));
    const struct passwd * pw = ::getpwuid(::getuid());
    add(pw != nullptr ? pw->pw_dir : nullptr);
    return out;
}

// One audit line for a rule: the resolved path, the argument it came from when they differ, and what was
// actually granted. The rights matter: read, read+execute, write and device are four very different grants and
// an operator reading `sandbox: grants /dev/shm` cannot otherwise tell which one it is.
std::string describe_rule(const std::string & resolved, const std::string & from, std::uint64_t rights);

std::string dir_of(const std::string & p) {
    const std::size_t slash = p.find_last_of('/');
    return slash == std::string::npos ? std::string(".") : (slash == 0 ? std::string("/") : p.substr(0, slash));
}

void add_if_exists(std::vector<std::string> & into, const std::string & p) {
    if (path_exists(p)) {
        into.push_back(p);
    }
}

std::string shebang_first_word(const std::string & exe, int index);

// `prog` resolved against PATH, or empty. Only used for the second word of a `#!/usr/bin/env prog` shebang.
std::string path_lookup(const std::string & prog) {
    if (prog.empty() || prog.find('/') != std::string::npos) {
        return path_exists(prog) ? prog : std::string();
    }
    const char * path = ::getenv("PATH");
    const std::string dirs = path != nullptr ? path : "/usr/bin:/bin";
    std::size_t at = 0;
    while (at <= dirs.size()) {
        const std::size_t sep = dirs.find(':', at);
        const std::string dir = dirs.substr(at, sep == std::string::npos ? std::string::npos : sep - at);
        if (!dir.empty()) {
            const std::string cand = dir + "/" + prog;
            if (::access(cand.c_str(), X_OK) == 0) {
                return cand;
            }
        }
        if (sep == std::string::npos) {
            break;
        }
        at = sep + 1;
    }
    return {};
}

// A script engine (server/tests/fake-engine.py) is exec'd through its interpreter, which must therefore be
// executable under the ruleset. Reading the shebang avoids granting all of /usr/bin.
//
// `#!/usr/bin/env python3` needs TWO entries, not one: Landlock governs every execve, so allowing `env` is not
// enough -- env then exec's `python3`, which the ruleset refuses with EACCES and the engine dies with
// "env: 'python3': Permission denied". So when the interpreter is env, its first argument is resolved against
// PATH and returned as well. Found by running server/tests/e2e.sh against the real ruleset.
// `unresolved` receives env's program name when it was found in the shebang but could not be resolved, so the
// caller can say so instead of letting the engine die at exec with nothing in the log.
std::vector<std::string> shebang_executables(const std::string & exe, std::string & unresolved) {
    std::vector<std::string> out;
    unresolved.clear();
    const std::string first = shebang_first_word(exe, 0);
    if (first.empty()) {
        return out;
    }
    out.push_back(first);
    // Only the real coreutils env gets its argument resolved against PATH. Matching on the basename alone meant
    // `#!/tmp/env python3` derived a read+exec grant on /usr/bin/python3 from a filename -- a guess about what an
    // unknown binary will exec. For anything else the operator names the interpreter with --sandbox-allow-read.
    if (first != "/usr/bin/env" && first != "/bin/env") {
        return out;
    }
    // skip env's own options (-i, -S, NAME=VALUE) and take the first bare word: the program it will exec
    for (int word = 1; word < 16; ++word) {
        const std::string w = shebang_first_word(exe, word);
        if (w.empty()) {
            break;
        }
        if (w[0] == '-' || w.find('=') != std::string::npos) {
            continue;
        }
        const std::string resolved = path_lookup(w);
        if (resolved.empty()) {
            unresolved = w;   // e.g. a relative ./python3, or a name not on PATH
        } else {
            out.push_back(resolved);
        }
        break;
    }
    return out;
}

// Word `index` of the shebang line (0 = the interpreter), or empty.
std::string shebang_first_word(const std::string & exe, int index) {
    const int fd = ::open(exe.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return {};
    }
    char buf[256] = {0};
    const ssize_t n = ::read(fd, buf, sizeof buf - 1);
    ::close(fd);
    if (n < 3 || buf[0] != '#' || buf[1] != '!') {
        return {};
    }
    // the shebang line only
    std::size_t end = 2;
    while (end < (std::size_t) n && buf[end] != '\n' && buf[end] != '\r') {
        ++end;
    }
    std::size_t i = 2;
    for (int word = 0;; ++word) {
        while (i < end && (buf[i] == ' ' || buf[i] == '\t')) {
            ++i;
        }
        const std::size_t start = i;
        while (i < end && buf[i] != ' ' && buf[i] != '\t') {
            ++i;
        }
        if (i == start) {
            return {};
        }
        if (word == index) {
            return std::string(buf + start, i - start);
        }
    }
}

} // namespace

SandboxFilterFacts sandbox_filter_facts() {
    std::vector<SandboxFilterInsn> filter;
    build_filter(filter, (std::uint32_t) SECCOMP_RET_KILL_PROCESS);
    return SandboxFilterFacts{filter.size(), kKilledCount};
}

bool sandbox_available(std::string & why) {
    if (!seccomp_action_available(SECCOMP_RET_KILL_PROCESS)) {
        why = "this kernel has no SECCOMP_RET_KILL_PROCESS (pre-4.14), so a kill-list hit would end one thread "
              "of the engine instead of the process, and the sandbox would report itself as enforced while a "
              "control had silently degraded";
        return false;
    }
    if (::prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) < 0) {
        why = "this kernel has no PR_SET_NO_NEW_PRIVS, which seccomp and Landlock both need without privileges";
        return false;
    }
    std::uint32_t errno_action = SECCOMP_RET_ERRNO;
    if (!seccomp_action_available(errno_action)) {
        why = "this kernel has no seccomp filter support (CONFIG_SECCOMP_FILTER)";
        return false;
    }
    const int abi = landlock_abi();
    if (abi < 1) {
        why = "this kernel has no Landlock (CONFIG_SECURITY_LANDLOCK, and `landlock` in the lsm= list)";
        return false;
    }
    return true;
}

SandboxConfig sandbox_default_config(const std::string & engine_exe,
                                     const std::string & model_path,
                                     const std::vector<std::string> & engine_args,
                                     const std::vector<std::string> & extra_read,
                                     const std::vector<std::string> & extra_write,
                                     std::vector<std::string> & notes) {
    SandboxConfig cfg;

    // The engine's and CUDA's own code. Execute as well as read: the dynamic loader needs it for every .so, and
    // the engine dlopen()s libstrata_kq.so from beside its own binary (src/prefill/mmq_kq_loader.cpp).
    for (const char * p : {"/usr/lib", "/usr/lib64", "/lib", "/lib64", "/usr/local/lib",
                           "/opt/cuda/lib64", "/usr/local/cuda/lib64"}) {
        add_if_exists(cfg.read_exec, p);
    }
    for (const char * p : {"/usr/share", "/etc/ld.so.cache", "/etc/ld.so.conf.d", "/etc/ld.so.preload",
                           "/etc/localtime"}) {
        add_if_exists(cfg.read, p);
    }

    // The GPU. /dev/nvidia* are mode 0666 on a box without nvidia-container-toolkit, which is why they can be
    // allowed by name rather than through a CDI hook.
    for (const char * p : {"/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset",
                           "/dev/null", "/dev/zero", "/dev/urandom", "/dev/random"}) {
        add_if_exists(cfg.device, p);
    }
    for (int i = 0; i < 16; ++i) {
        char dev[32];
        std::snprintf(dev, sizeof dev, "/dev/nvidia%d", i);
        add_if_exists(cfg.device, dev);
    }
    add_if_exists(cfg.write, "/dev/shm");   // shm_open in all four CUDA libraries; --shared-expert-arena

    // /proc, narrowed. Granting all of /proc would expose every other process's environ and cmdline - the exact
    // asset the threat model protects. /proc/self resolves to this child's own directory and the pid does not
    // change across exec, so this is "its own entry and nothing else".
    cfg.read.push_back("/proc/self");
    for (const char * p : {"/proc/meminfo", "/proc/buddyinfo", "/proc/sys/vm/nr_hugepages"}) {
        add_if_exists(cfg.read, p);
    }
    // /sys, narrowed to the hierarchies the engine reads: CPU and NUMA topology, cgroup limits, THP, and the
    // rotational flag --ple-io uses to decide between direct and mmap.
    for (const char * p : {"/sys/devices/system/cpu", "/sys/devices/system/node", "/sys/fs/cgroup",
                           "/sys/kernel/mm", "/sys/dev/block", "/sys/bus/pci/devices"}) {
        add_if_exists(cfg.read, p);
    }

    // The picture rows the server encodes and hands over on the GENI line live in a mkstemp file under /tmp
    // (server/strata_context.cpp). Read-only: the child only consumes them.
    // NOT /tmp. A Landlock rule on a directory grants its whole subtree, and /tmp holds other programs'
    // scratch data -- on this box the coding agent's own session transcripts. The engine needs exactly one
    // thing there: the picture rows the server hands it on the GENI line. The server therefore makes a
    // directory of its own for those and passes it in `extra_read`, so the grant is that directory alone.

    // The engine itself, its directory, and - when it is a script - its interpreter.
    if (path_exists(engine_exe)) {
        cfg.read_exec.push_back(engine_exe);
        cfg.read_exec.push_back(dir_of(engine_exe));
        std::string unresolved;
        for (const std::string & interp : shebang_executables(engine_exe, unresolved)) {
            if (path_exists(interp)) {
                cfg.read_exec.push_back(interp);
                notes.push_back("script engine: " + interp + " is allowed to execute");
            } else {
                notes.push_back("script engine: " + interp + " does not exist");
            }
        }
        if (!unresolved.empty()) {
            notes.push_back("script engine: its interpreter \"" + unresolved +
                            "\" is not on PATH, so it is NOT allowed to execute; the engine will fail at exec"
                            "  [--sandbox-allow-read <the interpreter> fixes it]");
        }
    }

    // The vocabulary GGUF's directory, and every engine argument that names a path that exists. The server does
    // not know what the engine's options mean, so the policy is derived from the invocation and logged line by
    // line instead of hardcoded. A path-looking argument that does not exist is named in the notes rather than
    // allowed, so that when the engine later fails to write there the reason is already in the log.
    if (!model_path.empty()) {
        cfg.read.push_back(dir_of(model_path));
    }
    for (const std::string & arg : engine_args) {
        // `--opt=/path` has to be handled as well as `--opt /path`. Skipping anything starting with `--` meant
        // the `=` spelling of the documented invocation produced neither a grant nor a note: the engine then got
        // EACCES on every model file and the log said nothing at all about why.
        std::string a = arg;
        if (a.rfind("--", 0) == 0) {
            const std::size_t eq = a.find('=');
            if (eq == std::string::npos) {
                continue;         // a bare flag
            }
            a = a.substr(eq + 1);
        }
        if (a.empty()) {
            continue;
        }
        if (a.find('/') == std::string::npos) {
            // No slash, so it is almost always a value rather than a path (`--kv int8`, `--spec 4`). It is not
            // granted -- guessing from bare words would widen the policy by accident -- but if such a value does
            // name something that exists relative to the working directory, say so, because the engine will then
            // fail on it and the reason belongs in the log rather than in a support thread.
            if (path_exists(a)) {
                notes.push_back("engine argument \"" + a +
                                "\" names an existing path but has no directory part, so it is NOT granted"
                                "  [pass it as an absolute path, or --sandbox-allow-read <dir>]");
            }
            continue;
        }
        if (path_exists(a)) {
            cfg.read.push_back(a);
            notes.push_back("engine argument allowed to read: " + a);
        } else {
            notes.push_back("engine argument not allowed (does not exist): " + a +
                            "  [--sandbox-allow-write <dir> if the engine is meant to create it]");
        }
    }

    for (const std::string & p : extra_read) {
        cfg.read.push_back(p);
        notes.push_back("--sandbox-allow-read " + p);
    }
    for (const std::string & p : extra_write) {
        cfg.write.push_back(p);
        notes.push_back("--sandbox-allow-write " + p);
    }
    return cfg;
}

bool sandbox_plan(const SandboxConfig & cfg, SandboxPlan & plan, std::string & err) {
    plan = SandboxPlan{};
    if (!cfg.enabled) {
        return true;   // --no-sandbox: apply() is a no-op and the caller has already shouted about it
    }

    plan.enabled    = true;
    plan.want_netns = true;   // always attempted; the child reports whether it got one
    plan.landlock_abi = landlock_abi();
    if (plan.landlock_abi < 1) {
        err = "Landlock is not available on this kernel";
        return false;
    }
    const std::uint64_t handled = landlock_handled_fs(plan.landlock_abi);

    // A Landlock rule on a directory grants its whole subtree, so a rule that resolves to a home directory or to
    // the filesystem root is not a containment policy at all -- it is the absence of one. This is reachable by
    // accident rather than by misuse: `dir_of()` returns "." for a path with no slash, so the natural
    // `cd ~ && aegis -m model.gguf` resolved to $HOME and granted the engine ~/.ssh and ~/.claude. Refusing is the
    // right failure: the operator passes an absolute model path, or says --sandbox-allow-read explicitly.
    const std::vector<std::string> forbidden = forbidden_roots();
    const auto dangerous_root = [&forbidden](const std::string & resolved) -> bool {
        for (const std::string & root : forbidden) {
            // the root itself, or any ancestor of it (`/home` when a home is `/home/<user>`, and so on up)
            if (resolved == root) {
                return true;
            }
            if (root.size() > resolved.size() && root.compare(0, resolved.size(), resolved) == 0 &&
                root[resolved.size()] == '/') {
                return true;
            }
        }
        return false;
    };

    std::vector<std::string> seen;        // resolved paths, so a path named twice becomes one rule
    std::vector<std::size_t> seen_rule;   // and the index of that rule, so the second mention can widen it
    const auto add = [&](const std::string & path, std::uint64_t rights, bool open_in_child) -> bool {
        char resolved[PATH_MAX];
        if (!open_in_child) {
            if (::realpath(path.c_str(), resolved) == nullptr) {
                err = "sandbox: cannot resolve " + path + ": " + std::strerror(errno);
                return false;
            }
            if (dangerous_root(resolved)) {
                err = std::string("sandbox: refusing to grant ") + resolved + ", which the argument \"" + path +
                      "\" resolves to. A Landlock rule on a directory grants its whole subtree, and that subtree holds "
                      "everything -- ssh keys, agent transcripts, every other project -- so such a rule is not a "
                      "policy at all. Name the directory that actually holds what the engine needs: an absolute "
                      "path to the model and to the engine, or --sandbox-allow-read <dir>.";
                return false;
            }
        } else {
            std::snprintf(resolved, sizeof resolved, "%s", path.c_str());
        }
        // Merge, never drop. The four sets are installed in a fixed order (read_exec, read, write, device), so
        // a path named in two of them used to keep only the rights of whichever came first: a path that was both
        // an engine argument and a --sandbox-allow-write target silently became read-only, and
        // --sandbox-allow-read /dev/nvidia0 stripped IOCTL_DEV from the node, which is instantly fatal to CUDA.
        // The log confirmed a grant that had not been made.
        for (std::size_t i = 0; i < seen.size(); ++i) {
            if (seen[i] == resolved) {
                plan.rules[seen_rule[i]].rights |= (rights & handled);
                if (plan.rules[seen_rule[i]].fd >= 0) {
                    struct stat st{};
                    if (::fstat(plan.rules[seen_rule[i]].fd, &st) == 0 && !S_ISDIR(st.st_mode)) {
                        plan.rules[seen_rule[i]].rights &= ~kDirectoryOnlyRights;
                    }
                }
                plan.granted[seen_rule[i]] = describe_rule(resolved, path, plan.rules[seen_rule[i]].rights);
                return true;
            }
        }
        SandboxPlan::Rule rule;
        rule.rights = rights & handled;
        rule.name   = (std::uint32_t) plan.names.size();
        if (open_in_child) {
            rule.fd = -1;   // /proc/self only, and that is a directory
        } else {
            rule.fd = ::open(resolved, O_PATH | O_CLOEXEC);
            if (rule.fd < 0) {
                err = std::string("sandbox: cannot open ") + resolved + ": " + std::strerror(errno);
                return false;
            }
            struct stat st{};
            if (::fstat(rule.fd, &st) != 0) {
                err = std::string("sandbox: cannot stat ") + resolved + ": " + std::strerror(errno);
                ::close(rule.fd);
                return false;
            }
            if (!S_ISDIR(st.st_mode)) {
                rule.rights &= ~kDirectoryOnlyRights;
            }
        }
        if (rule.rights == 0) {
            err = std::string("sandbox: no usable Landlock right left for ") + resolved;
            if (rule.fd >= 0) {
                ::close(rule.fd);
            }
            return false;
        }

        // All four vectors are appended together, after every failure path, so `seen_rule[i]` always indexes a
        // rule that exists and `granted[i]` always describes it. Recording `seen` earlier left a dangling index
        // behind a failed add(), and pushing `granted` on only one of the two branches put the two out of step --
        // either one corrupts the heap the first time a path is named twice.
        seen.emplace_back(resolved);
        seen_rule.push_back(plan.rules.size());
        plan.names.insert(plan.names.end(), resolved, resolved + std::strlen(resolved) + 1);
        plan.granted.push_back(describe_rule(resolved, path, rule.rights));
        plan.rules.push_back(rule);
        return true;
    };

    for (const std::string & p : cfg.read_exec) {
        if (!add(p, kRightsExec, false)) return false;
    }
    for (const std::string & p : cfg.read) {
        // /proc/self cannot be opened by the parent: there it names the parent. The child opens it itself.
        if (!add(p, kRightsRead, p == "/proc/self")) return false;
    }
    for (const std::string & p : cfg.write) {
        if (!add(p, kRightsWrite, false)) return false;
    }
    for (const std::string & p : cfg.device) {
        if (!add(p, kRightsDevice, false)) return false;
    }
    if (plan.rules.empty()) {
        err = "sandbox: the allowlist is empty, which would stop the engine from loading at all";
        return false;
    }

    // No KILL_THREAD fallback. On the multithreaded engine that would turn every kill-list hit from "the
    // process dies and the operator sees signal 31" into "one thread dies and the rest carry on", while the log
    // still said `enforced` -- a security control degrading silently, which is the one thing this project must
    // not do. sandbox_available() makes its absence a startup failure instead.
    build_filter(plan.filter, (std::uint32_t) SECCOMP_RET_KILL_PROCESS);

    // Identity maps: the child keeps its own uid inside its user namespace, so it is never root there. Formatted
    // here because snprintf must not run in the forked child.
    std::snprintf(plan.uid_map, sizeof plan.uid_map, "%u %u 1\n", (unsigned) ::getuid(), (unsigned) ::getuid());
    std::snprintf(plan.gid_map, sizeof plan.gid_map, "%u %u 1\n", (unsigned) ::getgid(), (unsigned) ::getgid());
    return true;
}

std::string sandbox_describe(const SandboxPlan & plan, bool netns_installed) {
    if (!plan.enabled) {
        return "off";
    }
    char buf[192];
    // The netns line reports what the child achieved, not what the parent asked for. Without it the syscall
    // filter is the only thing denying egress, and that is a denylist -- an operator has to be able to tell.
    std::snprintf(buf, sizeof buf, "landlock abi %d, %zu paths, %s, seccomp (%zu instructions)",
                  plan.landlock_abi, plan.rules.size(),
                  netns_installed ? "network namespace"
                                  : (plan.want_netns ? "NO NETWORK NAMESPACE (the kernel refused one)"
                                                     : "no network namespace (not requested)"),
                  plan.filter.size());
    return buf;
}

// ---- the child side ------------------------------------------------------------------------------------------
// Everything below runs between fork and exec. Only async-signal-safe calls: prctl, syscall, open, write, close,
// _exit. No allocation, no stdio, no std::string, nothing that takes a lock the parent might hold.

namespace {

void report(int fd, SandboxStage stage, int err_no, bool fatal) {
    if (fd < 0) {
        return;
    }
    Record r{(std::uint16_t) stage, (std::uint16_t) (fatal ? 1 : 0), (std::int32_t) err_no};
    ssize_t ignored = ::write(fd, &r, sizeof r);
    (void) ignored;
}

bool write_once(const char * path, const char * data, std::size_t len) {
    const int fd = ::open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        return false;
    }
    const ssize_t n = ::write(fd, data, len);
    const int saved = errno;
    ::close(fd);
    errno = saved;
    return n == (ssize_t) len;
}

} // namespace

int sandbox_apply(const SandboxPlan & plan, int err_fd) noexcept {
    if (!plan.enabled) {
        return 0;
    }

#define SANDBOX_FAIL(stage) do { report(err_fd, (stage), errno, true); return -1; } while (0)

    // 1. No-new-privs: both seccomp and Landlock need it when the caller has no CAP_SYS_ADMIN. It also means the
    //    exec below cannot gain privileges through a setuid bit.
    if (::prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        SANDBOX_FAIL(SandboxStage::NoNewPrivs);
    }

    // 2. A network namespace with only `lo`. This is the complete egress control - a namespace has no syscall-level
    //    bypass - but it needs an unprivileged user namespace, which some kernels refuse. Losing it is a note, not
    //    a failure: the filter installed in step 4 still refuses every internet socket.
    bool netns = false;
    if (plan.want_netns) {
        if (::unshare(CLONE_NEWUSER | CLONE_NEWNET) == 0) {
            // setgroups must be denied before gid_map is written, or the gid_map write is refused.
            if (!write_once("/proc/self/setgroups", "deny", 4)) {
                SANDBOX_FAIL(SandboxStage::SetGroups);
            }
            if (!write_once("/proc/self/uid_map", plan.uid_map, std::strlen(plan.uid_map))) {
                SANDBOX_FAIL(SandboxStage::UidMap);
            }
            if (!write_once("/proc/self/gid_map", plan.gid_map, std::strlen(plan.gid_map))) {
                SANDBOX_FAIL(SandboxStage::GidMap);
            }
            netns = true;
        } else {
            report(err_fd, SandboxStage::Unshare, errno, false);
        }
    }
    const bool netns_ok = netns;

    // 3. Landlock: the filesystem becomes an allowlist and can never be widened again for this process or any of
    //    its descendants. Added after the /proc/self writes above, which it would otherwise refuse.
    {
        LlRulesetAttr attr{};
        attr.handled_access_fs  = landlock_handled_fs(plan.landlock_abi);
        attr.handled_access_net = plan.landlock_abi >= 4 ? (kLlNetBindTcp | kLlNetConnectTcp) : 0;
        // Abstract sockets have no path for a rule to name, so scoping is the only way to deny them; signals are
        // scoped so a contained engine cannot SIGKILL the supervisor, the operator's shell or their compositor.
        attr.scoped             = plan.landlock_abi >= 6 ? (kLlScopeAbstractUnix | kLlScopeSignal) : 0;
        const long rs = ::syscall(__NR_landlock_create_ruleset, &attr, ruleset_attr_size(plan.landlock_abi), 0u);
        if (rs < 0) {
            SANDBOX_FAIL(SandboxStage::LandlockCreate);
        }
        const int ruleset_fd = (int) rs;

        for (std::size_t i = 0; i < plan.rules.size(); ++i) {
            const SandboxPlan::Rule & r = plan.rules[i];
            int fd = r.fd;
            if (fd < 0) {   // only /proc/self: the parent could not open it on this child's behalf
                fd = ::open(plan.names.data() + r.name, O_PATH | O_CLOEXEC);
                if (fd < 0) {
                    const int saved_open = errno;
                    ::close(ruleset_fd);
                    errno = saved_open;
                    SANDBOX_FAIL(SandboxStage::LandlockOpen);
                }
            }
            LlPathBeneathAttr pb{};
            pb.allowed_access = r.rights;
            pb.parent_fd      = fd;
            const long added = ::syscall(__NR_landlock_add_rule, ruleset_fd, kRulePathBeneath, &pb, 0u);
            const int saved_add = errno;   // before the closes below, which would otherwise overwrite it
            if (fd != r.fd) {
                ::close(fd);
            }
            if (added != 0) {
                ::close(ruleset_fd);
                errno = saved_add;
                SANDBOX_FAIL(SandboxStage::LandlockRule);
            }
        }

        const long restricted = ::syscall(__NR_landlock_restrict_self, ruleset_fd, 0u);
        const int saved = errno;
        ::close(ruleset_fd);
        if (restricted != 0) {
            errno = saved;
            SANDBOX_FAIL(SandboxStage::LandlockRestrict);
        }
    }

    // 4. The syscall filter. Last, because it refuses `unshare`, which step 2 needed.
    {
        struct sock_fprog prog;
        prog.len    = (unsigned short) plan.filter.size();
        prog.filter = (struct sock_filter *) const_cast<SandboxFilterInsn *>(plan.filter.data());
        if (::syscall(__NR_seccomp, (unsigned) SECCOMP_SET_MODE_FILTER, 0u, &prog) != 0) {
            SANDBOX_FAIL(SandboxStage::Seccomp);
        }
    }

    // 5. Drop every inherited descriptor above stderr. Today the parent holds none that matters - the HTTP
    //    listener is created in server_http_context::start(), which runs after the engine is forked - but that
    //    order is not ours to depend on, and a listening socket in the child's hands would let it read requests.
    //    This also closes err_fd, which the parent reads as "the exec happened".
    // The acknowledgement. Written before close_range below, which closes err_fd and is what the parent
    // otherwise sees as end-of-file. `netns_ok` is the outcome, not the request.
    report(err_fd, SandboxStage::Installed, netns_ok ? 1 : 0, false);

    if (::syscall(__NR_close_range, 3u, ~0u, 0u) != 0) {
        for (int fd = 3; fd < 1024; ++fd) {
            ::close(fd);
        }
    }
    return 0;

#undef SANDBOX_FAIL
}

} // namespace aegis

// ---- every other platform ------------------------------------------------------------------------------------
#else

namespace aegis {

bool sandbox_available(std::string & why) {
    why = "not available on this platform: seccomp-bpf and Landlock are Linux interfaces";
    return false;
}

SandboxConfig sandbox_default_config(const std::string &, const std::string &,
                                     const std::vector<std::string> &,
                                     const std::vector<std::string> &,
                                     const std::vector<std::string> &,
                                     std::vector<std::string> & notes) {
    notes.push_back("sandbox: not available on this platform; the engine runs uncontained");
    SandboxConfig cfg;
    cfg.enabled = false;
    return cfg;
}

bool sandbox_plan(const SandboxConfig &, SandboxPlan & plan, std::string &) {
    plan = SandboxPlan{};
    return true;   // a disabled plan; the caller's platform check is what reports it
}

std::string sandbox_describe(const SandboxPlan &, bool) {
    return "unavailable";
}

int sandbox_apply(const SandboxPlan &, int) noexcept {
    return 0;
}

} // namespace aegis

#endif
