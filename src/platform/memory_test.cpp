// src/platform/memory_test.cpp - plan v0.3 P0.1: lock_resident on a 256 MiB region (CPU only, no GPU, no model).
#include "strata/platform/memory.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#if !defined(_WIN32)
#include <sys/mman.h>
#include <sys/resource.h>
#endif

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <psapi.h>
#pragma comment(lib, "psapi.lib")
#endif

namespace {
uint64_t gib(double g) { return (uint64_t) (g * 1073741824.0); }

// #577: the file tier's unbuffered choice (true = the file cache keeps its reads, so they stay buffered)
int file_cache_keeps_cases() {
    using strata::platform::file_cache_keeps;
    struct Case { const char* what; double avail, arena, read; bool keeps; };
    const double experts = 71.7;   // UD-Q4_K_XL's routed experts
    const Case cases[] = {
        // the reporter's 96 GB PC (83.0 GiB available at start): 0.1.38 passed the budget asked for and every
        // shard's bytes (103.7 GiB) and went unbuffered
        {"96 GB, 0.1.38's inputs (budget 72, all shards)", 83.0, 72.0, 103.7, false},
        {"96 GB, budget 72, before the copy (arena = all experts)", 83.0, experts, 0.0, true},
        {"96 GB, budget 72, copy built (48.94 resident)", 83.0 - 48.94, 0.0, experts - 48.94, true},
        {"96 GB, budget 40, before the copy", 83.0, 40.0, experts - 40.0, true},
        {"96 GB, budget 40, copy built", 83.0 - 40.0, 0.0, experts - 40.0, true},
        // #357/#362's low-RAM PCs stay unbuffered
        {"64 GB, budget 40, before the copy", 55.0, 40.0, experts - 40.0, false},
        {"64 GB, budget 40, copy built", 15.0, 0.0, experts - 40.0, false},
        {"32 GB, budget 16, before the copy", 26.0, 16.0, experts - 16.0, false},
        {"32 GB, budget 16, copy built", 10.0, 0.0, experts - 16.0, false},
        {"32 GB, Q2_0 pack (34 GiB experts), budget 8", 26.0, 8.0, 34.0 - 8.0, false},
        {"nothing available", 0.0, 0.0, 1.0, false},
        {"arena larger than the RAM", 10.0, 20.0, 0.0, true},
    };
    int fail = 0;
    for (const Case& c : cases) {
        const bool got = file_cache_keeps(gib(c.avail), gib(c.arena), gib(c.read));
        std::printf("file_cache_keeps %-58s -> %s%s\n", c.what, got ? "buffered" : "unbuffered",
                    got == c.keeps ? "" : "   <-- WRONG");
        fail |= got != c.keeps;
    }
    // #1194, the Linux file tier: a skewed read set, so a twentieth of it (at least 1.5 GiB) is room enough
    const Case linux_cases[] = {
        {"#1194: 31 GB + 12 GB GPU, budget 18 built (6.4 avail, 13.6 read)", 6.4, 0.0, 13.6, true},
        {"#1194: before the copy (avail 25, arena 18, read 13.6)", 25.0, 18.0, 13.6, true},
        {"32 GB cgroup, budget 18, copy built (12.1 avail, 22 read)", 12.1, 0.0, 22.0, true},
        {"32 GB cgroup, budget 24, copy built (3.8 avail, 16 read)", 3.8, 0.0, 16.0, true},
        {"20 GB cgroup, budget 12, copy built (4.7 avail, 28 read)", 4.7, 0.0, 28.0, true},
        {"12 GB cgroup, budget 6, copy built (3.5 avail, 34 read)", 3.5, 0.0, 34.0, true},
        {"no room beside the margin (1 avail, 47 read)", 1.0, 0.0, 47.0, false},
        {"room under the 1.5 GiB floor (2.4 avail, 47 read)", 2.4, 0.0, 47.0, false},
        {"room a twentieth of the reads (3.4 avail, 47 read)", 3.4, 0.0, 47.0, true},
        {"room just under that (3.3 avail, 47 read)", 3.3, 0.0, 47.0, false},
        {"a big read set needs its twentieth (4 avail, 200 read)", 4.0, 0.0, 200.0, false},
        {"a big read set with that room (12 avail, 200 read)", 12.0, 0.0, 200.0, true},
        {"read less than the floor (2.5 avail, 1 read)", 2.5, 0.0, 1.0, true},
        {"nothing available", 0.0, 0.0, 1.0, false},
        {"arena larger than the RAM", 10.0, 20.0, 5.0, false},
    };
    for (const Case& c : linux_cases) {
        const bool got = file_cache_keeps(gib(c.avail), gib(c.arena), gib(c.read), 1ull << 30, 0.05, 3ull << 29);
        std::printf("file_cache_keeps (Linux file tier) %-52s -> %s%s\n", c.what, got ? "buffered" : "unbuffered",
                    got == c.keeps ? "" : "   <-- WRONG");
        fail |= got != c.keeps;
    }
    return fail;
}
}  // namespace

int main() {
    int fail_keeps = file_cache_keeps_cases();
    const uint64_t bytes = 256ull << 20;
    void* p = std::malloc(bytes);
    if (p == nullptr) return 2;
    std::memset(p, 1, bytes);
    const strata::platform::LockResult r = strata::platform::lock_resident(p, bytes);
    std::printf("lock_resident: ok=%d locked=%llu MiB (%s)\n", (int) r.ok, (unsigned long long) (r.locked_bytes >> 20),
                r.note.c_str());
    // RLIMIT_MEMLOCK below the region is the machine's configuration, not a defect in lock_resident: on a default
    // Linux box the limit is 8 MiB and this asks for 256. That is worth a skip rather than a failure, so the rest of
    // the suite stays a usable gate -- but it has to be ESTABLISHED, not inferred.
    //
    // The first version of this gate asked `r.note.find("ulimit -l")`, which is not evidence: lock_resident sets
    // that one string for every non-zero mlock() return (memory.cpp:176), errno is never looked at, and
    // `rlim_cur < bytes` is permanently true on any box whose limit is under 256 MiB. So a real defect -- a stale
    // or misaligned pointer, a length past the mapping, a region never mapped -- produced the identical note and
    // the identical ENOMEM, satisfied both halves, and printed "RLIMIT_MEMLOCK is 8 MiB" while the regression went
    // unreported. A 4 KiB unmapped range reproduces it exactly, well under the limit.
    //
    // So probe positively instead: lock something that MUST succeed if the limit is the only obstacle. A page is
    // under every non-zero limit, and this process has not locked anything else yet. If the small lock works and
    // the large one did not, the limit is the difference and nothing else is. If the small lock fails too, the
    // limit is not the whole story and this stays a failure.
#if !defined(_WIN32)
    if (!fail_keeps && !r.ok) {
        rlimit lim{};
        const bool got = getrlimit(RLIMIT_MEMLOCK, &lim) == 0;
        const bool under = got && lim.rlim_cur != RLIM_INFINITY && lim.rlim_cur < bytes;
        bool one_page_locks = false;
        if (under) {
            const size_t page = 4096;
            void* probe = std::malloc(page);
            if (probe != nullptr) {
                std::memset(probe, 1, page);
                one_page_locks = mlock(probe, page) == 0;
                if (one_page_locks) munlock(probe, page);
                std::free(probe);
            }
        }
        if (under && one_page_locks) {
            std::fprintf(stderr, "platform_memory_test: SKIP - RLIMIT_MEMLOCK is %llu bytes, this needs %llu. One page\n"
                         "           locks here, so the limit is the only obstacle. Raise it (ulimit -l, or\n"
                         "           LimitMEMLOCK= in a unit file) to run this check.\n",
                         (unsigned long long) lim.rlim_cur, (unsigned long long) bytes);
            strata::platform::unlock_resident(p, bytes);
            std::free(p);
            return 77;
        }
    }
#endif
    int fail = !(r.ok && r.locked_bytes == bytes) | fail_keeps;
#if defined(_WIN32)
    PROCESS_MEMORY_COUNTERS pmc{};
    GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof pmc);
    std::printf("working set %llu MiB\n", (unsigned long long) (pmc.WorkingSetSize >> 20));
    fail |= pmc.WorkingSetSize < bytes;
#endif
    strata::platform::unlock_resident(p, bytes);
    std::free(p);
    std::printf("platform_memory_test: %s\n", fail ? "FAILED" : "OK");
    return fail;
}
