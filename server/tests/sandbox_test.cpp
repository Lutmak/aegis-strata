// aegis-strata: the engine child's containment (server/strata_sandbox.cpp). No GPU, no model, no llama.cpp.
//
//   c++ -std=c++20 -I server server/tests/sandbox_test.cpp server/strata_sandbox.cpp -o sandbox_test
//   ./sandbox_test
//
// The test is the harness and the probe. With no arguments it is the parent: it builds a plan, forks, applies the
// sandbox exactly as Engine::start will, and execs *itself* with --probe. With --probe it tries one thing per
// line and prints the result, which the parent asserts.
//
// Every probe asserts a local kernel decision, never reachability: socket(AF_INET, ...) *succeeds* on a machine
// with no network and only fails when something refuses it, so the test cannot pass vacuously on an airgapped
// box. Each denial is paired with the matching permission (AF_UNIX works, the allowed file reads), so the test
// also cannot pass by the engine being broken rather than contained.
//
// ---------------------------------------------------------------------------------------------------
// HOW TO PROVE THESE ASSERTIONS CAN FAIL
//
// A test never seen to fail proves nothing, and this file guards a security control, so the breaks
// below are part of it. Apply one, run the suite, see the NAMED failure, revert. A break that does not
// produce its named failure means the assertion is not testing what it claims.
//
// Breaks 2, 4 and 5 were applied on the reference machine on 2026-11-08 and each produced exactly its
// named failure and nothing else -- that is what establishes the halves are independent rather than
// one coarse pass/fail. Breaks 1 and 3 need the server wired in; 6 is covered at unit level by
// case 12; 7 needs a kernel without Landlock and is reviewed, not tested.
//
//   1  whole thing       pass --no-sandbox          -> all four e2e containment checks fail; case 13
//                                                     inverts; inet and canary both read `ok`.
//                                                     Catches: a test reading the wrong log field.
//   2  argument filter   kDenyAction = RET_ALLOW    -> inet, inet6, netlink denials fail; probes 5-8
//                                                     and the canary MUST still pass.
//                                                     Catches: the two halves accidentally coupled,
//                                                     i.e. a test that passes when *anything* denies.
//   3  Landlock only     allow the canary's dir     -> probe 5 (canary) fails; probes 1-4 MUST pass.
//                                                     Catches: the inverse coupling, and proves
//                                                     --sandbox-allow-read really does widen policy.
//   4  /proc narrowing   read "/proc" not           -> probe 8 (proc_other) fails.
//                        "/proc/self"                  Catches: a future "just allow /proc" re-exposing
//                                                     every process's cmdline and environment. This is
//                                                     the break most likely to happen for real.
//   5  kill list         drop __NR_ptrace           -> probe 9 fails on `signal == SIGSYS`. It reports
//                                                     errno=1 (EPERM from yama), which is why the
//                                                     assertion is on SIGSYS and not "ptrace failed":
//                                                     the latter would have passed with the bug in.
//                                                     Catches: a table built but never installed.
//   6  fail closed       --sandbox-allow-read       -> MUST fail as a refusal: non-zero exit, the
//                        /nonexistent                  message names the path, and the engine never
//                                                     forked. Catches the worst class here -- a policy
//                                                     that could not be installed being downgraded to
//                                                     a warning.
//   7  no Landlock       a kernel without it        -> sandbox_available() false and the server
//                                                     refuses to start without --no-sandbox. Not
//                                                     reachable on the reference machine, which has
//                                                     CONFIG_SECURITY_LANDLOCK=y; listed so it is not
//                                                     mistaken for tested.
// ---------------------------------------------------------------------------------------------------
//
// Cases, each the shape of a way the containment could be wrong:
//   1-4   the syscall filter: AF_INET, AF_INET6 and AF_NETLINK refused, AF_UNIX kept
//   5-6   the Landlock allowlist: a canary outside it is refused, a file inside it reads
//   7-8   the /proc narrowing: its own /proc/self and /proc/meminfo read, another process's environ does not
//   9-10  the kill list: ptrace and io_uring_setup end the process by SIGSYS
//   11    sandbox_available() answers on this kernel (the fail-closed path depends on it)
//   12    fail closed: a --sandbox-allow-read path that does not exist makes sandbox_plan() fail
//   13    the opt-out is real: with enabled = false the same probes all come back allowed
//   14    the network namespace, independently of seccomp: only `lo` is present
#include "strata_sandbox.h"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <fcntl.h>
#include <csignal>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__linux__)
#include <sys/ptrace.h>
#endif

using aegis::SandboxConfig;
using aegis::SandboxPlan;
using aegis::SandboxStage;

static int failures = 0;
static int checks   = 0;   // every assertion, counted, so the final line reports something a bug can move
#define CHECK(cond) do { ++checks; if (!(cond)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); ++failures; } } while (0)

namespace {

// ---- the probe side ------------------------------------------------------------------------------------------

// "denied" means the SANDBOX refused this, which is EAFNOSUPPORT from the filter -- not merely "it failed".
// Scoring any failure as containment is how this test would pass for the wrong reason: a filter that broke
// socket() for an unrelated reason, or a kernel without AF_NETLINK, would have read as success.
const char * try_socket(int domain, int type) {
    const int fd = ::socket(domain, type, 0);
    if (fd >= 0) {
        ::close(fd);
        return "ok";
    }
    return errno == EAFNOSUPPORT ? "denied" : "failed-other";
}

// Likewise: EACCES is Landlock refusing; ENOENT means the probe looked at the wrong path and proves nothing.
const char * try_read(const char * path) {
    const int fd = ::open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return errno == EACCES || errno == EPERM ? "denied" : "failed-other";
    }
    char b[1];
    const ssize_t n = ::read(fd, b, 1);
    ::close(fd);
    return n >= 0 ? "ok" : "unreadable";
}

// Finding from the independent review: the new mount API is the modern spelling of mount(2). clone3 with
// CLONE_NEWUSER|CLONE_NEWNS creates the user namespace first, which confers CAP_SYS_ADMIN over the mount
// namespace made in the same call -- so denying only `mount`/`unshare` left the whole thing reachable.
// One named mount syscall per probe. Testing only fsopen left six of the seven entries unasserted, and
// move_mount + open_tree alone are enough to graft a filesystem under an allowed directory -- the bypass the
// kill list exists to close. x86-64 numbers, which are ABI-stable.
struct MountCall { const char * name; long nr; };
const MountCall kMountCalls[] = {
    {"open_tree", 428}, {"move_mount", 429}, {"fsopen", 430},     {"fsconfig", 431},
    {"fsmount", 432},   {"fspick", 433},     {"mount_setattr", 442},
};

// Landlock governs connect() to a pathname AF_UNIX socket only when LANDLOCK_ACCESS_FS_RESOLVE_UNIX (ABI 9) is
// in the ruleset's handled set. Before it was, a contained engine reached /run/user/<uid>/bus and
// /run/user/<uid>/systemd/private and got `OK <guid>` from both -- a full escape, since systemd will start a
// transient unit for an authenticated caller. The target here is the canary's sibling socket: the canary's
// directory is deliberately outside the allowlist, and the socket is this user's own, so a plain DAC check would
// permit the connect. /etc/passwd is NOT a usable target -- connect() needs write permission, so mode 0644 denies
// it with EACCES whether Landlock is involved or not, which would make this assertion pass for the wrong reason.
const char * try_connect_path(const char * path) {
    const int s = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (s < 0) {
        return "nosocket";
    }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    const int rc = ::connect(s, (sockaddr *) &addr, sizeof addr);
    const int saved = errno;
    ::close(s);
    if (rc == 0) {
        return "ok";
    }
    return saved == EACCES || saved == EPERM ? "denied" : "other";
}

// LANDLOCK_SCOPE_SIGNAL (ABI 6). The parent is outside the Landlock domain, so a scoped child cannot signal it.
// Without the scope the engine could SIGKILL its own supervisor, the operator's shell or their compositor.
const char * try_signal_parent() {
    return ::kill(::getppid(), 0) == 0 ? "ok" : (errno == EPERM ? "denied" : "other");
}

const char * try_mount_api(long nr) {
    const long rc = ::syscall(nr, 0, 0, 0, 0, 0);   // must not return: SIGSYS
    if (rc >= 0) {
        return "reachable";
    }
    return "errno";
}

int count_interfaces() {
    // /proc/self/net/dev, not /sys/class/net: sysfs shows the network namespace of whoever mounted it, while
    // procfs resolves `net` against the reading process's own namespace. Reached through the /proc/self rule.
    const int fd = ::open("/proc/self/net/dev", O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    char buf[8192] = {0};
    const ssize_t n = ::read(fd, buf, sizeof buf - 1);
    ::close(fd);
    if (n <= 0) {
        return -1;
    }
    int lines = 0;
    for (ssize_t i = 0; i < n; ++i) {
        if (buf[i] == '\n') {
            ++lines;
        }
    }
    return lines > 2 ? lines - 2 : 0;   // two header lines
}

int probe(int argc, char ** argv) {
    const char * canary  = argc > 2 ? argv[2] : "";
    const char * allowed = argc > 3 ? argv[3] : "";
    const char * what    = argc > 4 ? argv[4] : "all";
    // another process of this user: /proc/<ppid>/cmdline is mode 0444 and, unlike environ, is not ptrace-gated,
    // so a denial here is Landlock's doing and nothing else's
    const std::string other = "/proc/" + std::to_string((long) ::getppid()) + "/cmdline";

    if (std::strcmp(what, "ptrace") == 0) {
#if defined(__linux__)
        ::ptrace(PTRACE_ATTACH, ::getppid(), nullptr, nullptr);   // must not return: SIGSYS
#endif
        std::printf("ptrace=ran errno=%d\n", errno);
        return 0;
    }
    if (std::strncmp(what, "mount_api:", 10) == 0) {
        std::printf("mount_api=%s\n", try_mount_api(std::atol(what + 10)));   // must not return: SIGSYS
        return 0;
    }
    if (std::strcmp(what, "io_uring") == 0) {
        unsigned char params[256] = {0};
        ::syscall(425 /* io_uring_setup */, 1u, params);          // must not return: SIGSYS
        std::printf("io_uring=ran errno=%d\n", errno);
        return 0;
    }

    std::printf("inet=%s inet6=%s netlink=%s unix=%s\n",
                try_socket(AF_INET, SOCK_STREAM), try_socket(AF_INET6, SOCK_DGRAM),
                try_socket(AF_NETLINK, SOCK_RAW), try_socket(AF_UNIX, SOCK_STREAM));
    std::printf("canary=%s model=%s procself=%s proc_meminfo=%s proc_other=%s\n",
                try_read(canary), try_read(allowed),
                try_read("/proc/self/status"), try_read("/proc/meminfo"), try_read(other.c_str()));
    std::printf("ifaces=%d\n", count_interfaces());
    std::printf("unixpath=%s signal=%s\n",
                try_connect_path((std::string(canary) + ".sock").c_str()), try_signal_parent());
    std::fflush(stdout);
    return 0;
}

// ---- the harness side ----------------------------------------------------------------------------------------

struct Outcome {
    std::string out;
    int         status = 0;
    bool        exited = false;
    int         signal = 0;
    std::vector<std::string> notes;   // non-fatal records from the child (today: no network namespace)
    std::string fail;                 // the fatal record, if any
};

// fork, apply the sandbox exactly as server/engine.hpp's Engine::start will, exec ourselves as the probe.
Outcome run_probe(const SandboxPlan & plan, const std::string & self, const std::string & canary,
                  const std::string & allowed, const char * what) {
    Outcome o;
    int out_pipe[2], err_pipe[2];
    if (::pipe(out_pipe) != 0 || ::pipe2(err_pipe, O_CLOEXEC) != 0) {
        o.fail = "pipe";
        return o;
    }
    const pid_t pid = ::fork();
    if (pid < 0) {
        o.fail = "fork";
        return o;
    }
    if (pid == 0) {
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        if (aegis::sandbox_apply(plan, err_pipe[1]) != 0) {
            ::_exit(126);
        }
        const char * argv[] = {self.c_str(), "--probe", canary.c_str(), allowed.c_str(), what, nullptr};
        ::execv(self.c_str(), (char * const *) argv);
        ::_exit(127);
    }
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);

    SandboxStage stage = SandboxStage::None;
    int err_no = 0;
    bool fatal = false;
    while (aegis::sandbox_read_record(err_pipe[0], stage, err_no, fatal)) {
        const std::string line = std::string(aegis::sandbox_stage_name(stage)) + ": " + std::strerror(err_no);
        if (fatal) {
            o.fail = line;
        } else {
            o.notes.push_back(line);
        }
    }
    ::close(err_pipe[0]);

    char buf[4096];
    ssize_t n;
    while ((n = ::read(out_pipe[0], buf, sizeof buf)) > 0) {
        o.out.append(buf, (std::size_t) n);
    }
    ::close(out_pipe[0]);

    int status = 0;
    ::waitpid(pid, &status, 0);
    o.exited = WIFEXITED(status);
    o.status = o.exited ? WEXITSTATUS(status) : 0;
    o.signal = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
    return o;
}

bool has(const std::string & hay, const char * needle) {
    return hay.find(needle) != std::string::npos;
}

} // namespace

#if !defined(__linux__)
// seccomp, Landlock, AF_NETLINK and pipe2 are Linux. 77 is the suite's "skip" code, and the Makefile runs this
// test last precisely so a skip cannot stop the rest of the suite.
int main() {
    std::printf("sandbox_test: skipped, this platform has no seccomp or Landlock\n");
    return 77;
}
#else

int main(int argc, char ** argv) {
    if (argc > 1 && std::strcmp(argv[1], "--probe") == 0) {
        return probe(argc, argv);
    }
    // What the built filter contains, for tools/check_docs.py. Answered before the kernel-support gate
    // below on purpose: building the program needs no kernel, so a document's filter size stays
    // checkable on a machine where the sandbox itself cannot be installed.
    if (argc > 1 && std::strcmp(argv[1], "--facts") == 0) {
        const aegis::SandboxFilterFacts f = aegis::sandbox_filter_facts();
        std::printf("filter_instructions=%zu\nkilled=%zu\n", f.instructions, f.killed);
        return 0;
    }

    char self_buf[4096] = {0};
    const ssize_t self_n = ::readlink("/proc/self/exe", self_buf, sizeof self_buf - 1);
    if (self_n <= 0) {
        std::fprintf(stderr, "sandbox_test: cannot find my own path\n");
        return 2;
    }
    const std::string self(self_buf);

    // 11: the probe the fail-closed path depends on
    std::string why;
    const bool available = aegis::sandbox_available(why);
    if (!available) {
        std::printf("sandbox_test: skipped, this kernel cannot enforce it: %s\n", why.c_str());
        return 77;   // a skip, not a pass: the kernel, not the code, is what is missing
    }

    // a model directory with one file in it, and a canary outside everything the policy allows
    char dir[] = "/tmp/aegis-sandbox-test-XXXXXX";
    if (::mkdtemp(dir) == nullptr) {
        std::perror("mkdtemp");
        return 2;
    }
    const std::string model_dir  = std::string(dir) + "/model";
    const std::string model_file = model_dir + "/vocab.gguf";
    const std::string canary     = std::string(dir) + "/canary";
    ::mkdir(model_dir.c_str(), 0700);
    for (const std::string & p : {model_file, canary}) {
        const int fd = ::open(p.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        CHECK(fd >= 0);
        if (fd >= 0) { CHECK(::write(fd, "x", 1) == 1); ::close(fd); }
    }

    // A listening socket beside the canary, for the two probes above. It stays open for the whole run; the
    // address is unlinked with the temp directory at the end.
    const std::string victim_sock = canary + ".sock";
    const int victim_fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    CHECK(victim_fd >= 0);
    if (victim_fd >= 0) {
        sockaddr_un vaddr{};
        vaddr.sun_family = AF_UNIX;
        std::snprintf(vaddr.sun_path, sizeof vaddr.sun_path, "%s", victim_sock.c_str());
        CHECK(::bind(victim_fd, (sockaddr *) &vaddr, sizeof vaddr) == 0);
        CHECK(::listen(victim_fd, 4) == 0);
    }

    // The policy: the defaults, with the model directory added as the engine's own and the canary's directory
    // deliberately left out. /tmp is in the default read set for the GENI rows file, so the canary must not sit
    // directly in /tmp - it sits in a mkdtemp directory, whose own entry is never added.
    std::vector<std::string> notes;
    SandboxConfig cfg = aegis::sandbox_default_config(self, model_file, {}, {}, {}, notes);
    // /tmp read would reach the canary through the hierarchy, so the test removes it: the real server keeps it
    // for pictures, which is why e2e.sh puts its canary outside /tmp.
    for (auto it = cfg.read.begin(); it != cfg.read.end();) {
        it = (*it == "/tmp") ? cfg.read.erase(it) : it + 1;
    }
    cfg.read.push_back(model_dir);

    SandboxPlan plan;
    std::string err;
    CHECK(aegis::sandbox_plan(cfg, plan, err));
    if (!err.empty()) {
        std::fprintf(stderr, "plan: %s\n", err.c_str());
    }
    // true: this test's probe child does get a netns on this kernel; case 13 covers the off case.
    std::printf("sandbox: %s\n", aegis::sandbox_describe(plan, true).c_str());

    // 1-8 and 14
    Outcome o = run_probe(plan, self, canary, model_file, "all");
    std::printf("probe: %s", o.out.c_str());
    for (const std::string & n : o.notes) {
        std::printf("note:  %s\n", n.c_str());
    }
    if (!o.fail.empty()) {
        std::fprintf(stderr, "sandbox did not install: %s\n", o.fail.c_str());
    }
    CHECK(o.fail.empty());
    CHECK(has(o.out, "inet=denied"));
    CHECK(has(o.out, "inet6=denied"));
    CHECK(has(o.out, "netlink=denied"));
    CHECK(has(o.out, "unix=ok"));
    CHECK(has(o.out, "canary=denied"));
    CHECK(has(o.out, "model=ok"));
    CHECK(has(o.out, "procself=ok"));
    CHECK(has(o.out, "proc_meminfo=ok"));
    CHECK(has(o.out, "proc_other=denied"));
    // Gated on the kernel's Landlock ABI, like the network-namespace check below it, because the sandbox
    // deliberately degrades rather than refusing to start: RESOLVE_UNIX (the pathname AF_UNIX closure) needs
    // ABI 9 and the SIGNAL scope needs ABI 6, and sandbox_available() accepts anything from ABI 1. Asserted
    // unconditionally, these turned every pre-ABI-9 kernel's `make test` red in a way that reads as a
    // regression in this fork rather than "this kernel cannot enforce that control". A skip line says which.
    if (plan.landlock_abi >= 9) {
        CHECK(has(o.out, "unixpath=denied"));
    } else {
        std::printf("skip:  landlock abi %d < 9: no RESOLVE_UNIX, so a pathname AF_UNIX socket is NOT denied "
                    "(docs/sandbox.md names the floor)\n", plan.landlock_abi);
    }
    if (plan.landlock_abi >= 6) {
        CHECK(has(o.out, "signal=denied"));
    } else {
        std::printf("skip:  landlock abi %d < 6: no SIGNAL scope, so signalling out is NOT denied\n",
                    plan.landlock_abi);
    }
    const bool netns = o.notes.empty();
    if (netns) {
        CHECK(has(o.out, "ifaces=1"));   // only `lo`
    } else {
        std::printf("skip:  no network namespace on this kernel; the syscall filter alone denies egress\n");
    }

    // 9-10: these must end the process, so each runs in its own fork
    Outcome p = run_probe(plan, self, canary, model_file, "ptrace");
    std::printf("probe: ptrace=%s\n", p.signal == SIGSYS ? "killed" : (p.out.empty() ? "no output" : p.out.c_str()));
    CHECK(p.signal == SIGSYS);
    Outcome u = run_probe(plan, self, canary, model_file, "io_uring");
    std::printf("probe: io_uring=%s\n", u.signal == SIGSYS ? "killed" : (u.out.empty() ? "no output" : u.out.c_str()));
    CHECK(u.signal == SIGSYS);
    for (const MountCall & mc : kMountCalls) {
        char what[32];
        std::snprintf(what, sizeof what, "mount_api:%ld", mc.nr);
        Outcome m = run_probe(plan, self, canary, model_file, what);
        std::printf("probe: %-14s %s\n", mc.name, m.signal == SIGSYS ? "killed" : (m.out.empty() ? "no output" : m.out.c_str()));
        CHECK(m.signal == SIGSYS);
    }

    // The filter's size is the only operator-visible signal that the kill list is the length it should be, and
    // the kill list is what closes the mount-API bypass. 17 fixed instructions (3 arch gate + 3 x32 gate + 5
    // socket + 5 socketpair + 1 allow) plus 2 per kill-list entry, 29 of them.
    std::printf("filter: %zu instructions\n", plan.filter.size());
    CHECK(plan.filter.size() == 17 + 2 * 29);

    // sandbox_describe must report the OUTCOME, not the request: with no network namespace it has to say so,
    // because that is the state in which only the syscall denylist denies egress.
    const std::string with_ns = aegis::sandbox_describe(plan, true);
    const std::string no_ns   = aegis::sandbox_describe(plan, false);
    std::printf("describe(netns=true):  %s\ndescribe(netns=false): %s\n", with_ns.c_str(), no_ns.c_str());
    CHECK(has(with_ns, "network namespace") && !has(with_ns, "NO NETWORK NAMESPACE"));
    CHECK(has(no_ns, "NO NETWORK NAMESPACE"));

    // 12: fail closed on a policy that cannot be installed
    {
        SandboxConfig bad = cfg;
        bad.read.push_back("/nonexistent-path-for-the-sandbox-test");
        SandboxPlan bad_plan;
        std::string bad_err;
        CHECK(!aegis::sandbox_plan(bad, bad_plan, bad_err));
        CHECK(has(bad_err, "/nonexistent-path-for-the-sandbox-test"));
    }

    // 13: the opt-out is real, which is also what proves cases 1-8 are not environmental
    {
        SandboxConfig off = cfg;
        off.enabled = false;
        SandboxPlan off_plan;
        std::string off_err;
        CHECK(aegis::sandbox_plan(off, off_plan, off_err));
        Outcome w = run_probe(off_plan, self, canary, model_file, "all");
        std::printf("probe(off): %s", w.out.c_str());
        // Every probe the enforced run asserts must invert here, or the enforced run proved nothing about the
        // sandbox and was only describing this machine.
        CHECK(w.fail.empty());
        CHECK(has(w.out, "inet=ok"));
        CHECK(has(w.out, "inet6=ok"));
        CHECK(has(w.out, "netlink=ok"));
        CHECK(has(w.out, "unix=ok"));
        CHECK(has(w.out, "canary=ok"));
        CHECK(has(w.out, "unixpath=ok"));
        CHECK(has(w.out, "signal=ok"));
        CHECK(has(w.out, "model=ok"));
        CHECK(has(w.out, "proc_other=ok"));
        // and the kill list, which case 13 never used to touch: without the filter these RUN instead of dying
        Outcome wp = run_probe(off_plan, self, canary, model_file, "ptrace");
        std::printf("probe(off): ptrace=%s\n", wp.signal == SIGSYS ? "killed" : "ran");
        CHECK(wp.signal != SIGSYS);
        Outcome wu = run_probe(off_plan, self, canary, model_file, "io_uring");
        std::printf("probe(off): io_uring=%s\n", wu.signal == SIGSYS ? "killed" : "ran");
        CHECK(wu.signal != SIGSYS);
        Outcome wm = run_probe(off_plan, self, canary, model_file, "mount_api");
        std::printf("probe(off): mount_api=%s\n", wm.signal == SIGSYS ? "killed" : "ran");
        CHECK(wm.signal != SIGSYS);
    }

    // 14: a rule that would grant a home directory, or /, is refused. (Independent review, finding 1: a
    // relative -m made dir_of() return "." which resolved to $HOME, and a Landlock rule on a directory grants
    // its whole subtree -- so the natural `cd ~ && aegis -m model.gguf` silently handed the engine ~/.ssh.)
    {
        SandboxConfig root = cfg;
        root.read.push_back("/");
        SandboxPlan root_plan;
        std::string root_err;
        CHECK(!aegis::sandbox_plan(root, root_plan, root_err));
        CHECK(has(root_err, "refusing to grant /"));
        std::printf("refuse-root: %s\n", root_err.empty() ? "(none)" : "refused");

        const char * home = ::getenv("HOME");
        if (home != nullptr && home[0] == '/') {
            SandboxConfig h = cfg;
            h.read.push_back(home);
            SandboxPlan h_plan;
            std::string h_err;
            CHECK(!aegis::sandbox_plan(h, h_plan, h_err));
            CHECK(has(h_err, "refusing to grant"));
            std::printf("refuse-home: %s\n", h_err.empty() ? "(none)" : "refused");
        }
    }

    // 15: the CHILD's fatal path. Case 12 covers the parent refusing to build a plan; this covers a plan that
    // builds and then fails inside the forked child, which is the path Engine::start turns into a refusal to
    // start the engine at all. An invalid rule fd makes landlock_add_rule fail there.
    {
        SandboxPlan broken = plan;
        CHECK(!broken.rules.empty());
        if (!broken.rules.empty()) {
            broken.rules[0].fd = 9999;   // not an open descriptor in the child
            Outcome b = run_probe(broken, self, canary, model_file, "all");
            std::printf("child-fatal: fail=%s exited=%d status=%d\n",
                        b.fail.empty() ? "(none)" : b.fail.c_str(), (int) b.exited, b.status);
            CHECK(!b.fail.empty());                       // a fatal record came back over the pipe
            CHECK(has(b.fail, "landlock_rule"));          // naming the stage that failed
            CHECK(!has(b.out, "inet="));                  // and the probe never ran
        }
    }

    // 16: a path named in two sets keeps the rights of BOTH. The four sets install in a fixed order
    // (read_exec, read, write, device), and the duplicate check used to return without merging -- so
    // `--sandbox-allow-write <dir>` on a directory that was also an engine argument silently became read-only,
    // and `--sandbox-allow-read /dev/nvidia0` stripped IOCTL_DEV from the node, which is fatal to CUDA.
    {
        SandboxConfig both = cfg;
        both.read.push_back(model_dir);    // the order the four sets are installed in puts read first
        both.write.push_back(model_dir);
        SandboxPlan both_plan;
        std::string both_err;
        CHECK(aegis::sandbox_plan(both, both_plan, both_err));
        // Asserted on the audit line, not on a raw rights bit: that line is what an operator actually reads,
        // and a hand-written bit mask is how this check first passed vacuously (1ULL << 3 is READ_DIR, which is
        // set either way, so it proved nothing).
        bool found = false, writable = false;
        for (std::size_t i = 0; i < both_plan.rules.size(); ++i) {
            if (std::string(both_plan.names.data() + both_plan.rules[i].name) == model_dir) {
                found = true;
                std::printf("merge: %s\n", both_plan.granted[i].c_str());
                writable = has(both_plan.granted[i], "write");
            }
        }
        CHECK(found);
        CHECK(writable);   // read-only here is the bug this case exists for
    }

    // Leave nothing behind under /tmp: the fixtures are ours and the run is over. Every earlier run left its
    // mkdtemp directory in place, which is how a /tmp listing filled up with aegis-sandbox-test-* directories.
    for (const std::string & f : {canary, model_file}) {
        ::remove(f.c_str());
    }
    ::rmdir(model_dir.c_str());
    ::rmdir(dir);

    if (failures) {
        std::fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    // The count is computed, not written: a hardcoded "16 cases passed" stayed 16 whether cases were added,
    // deleted or silently skipped, so it was never evidence of anything.
    std::printf("sandbox_test: %d assertions passed\n", checks);
    return 0;
}
#endif  // __linux__
