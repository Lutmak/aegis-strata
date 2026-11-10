// src/core/nv_defaults.cpp - see include/strata/core/nv_defaults.hpp.
#include "strata/core/nv_defaults.hpp"

#include "strata/core/emulate.hpp"

#include <cctype>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace strata::core {

namespace {
// The kill switch fails CLOSED: set to anything that is not plainly "on", and the table is off.
//
// It used to be `v[0] == '0'`, which failed open in the direction that matters. `STRATA_SM86_PERF=` -- the usual
// way to blank a variable -- left the table ON, as did `false`, `no` and `off`; only a leading '0' disabled it.
// This is the one escape hatch for a change that alters the model's token ids, so an operator who clearly meant
// to disable it must not silently get the switches instead. Losing the switches by accident costs ~10% speed and
// is visible (no startup line); keeping them by accident costs the reproducibility the operator asked for.
// `STRATA_SM86_PERF=1` still turns them on, which is the only reason to set it at all.
bool off(const char* name) {
    const char* v = std::getenv(name);
    if (v == nullptr) {
        return false;   // unset: the default applies
    }
    std::string s(v);
    for (char& c : s) {
        c = (char) std::tolower((unsigned char) c);
    }
    return !(s == "1" || s == "true" || s == "yes" || s == "on");
}
}  // namespace

std::vector<std::pair<std::string, std::string>> nv_default_env(int major, int minor) {
    std::vector<std::pair<std::string, std::string>> t;
    const int mj = strata::cc_major_of(major), mn = strata::cc_minor_of(minor);
    if (mj == 8 && mn == 6) {
        if (off("STRATA_SM86_PERF")) return t;
        // Reference machine (RTX 3080 Ti, IQ3_XXS, the README's command line), decode over a 256-token answer to a
        // fixed prompt, server restarted for every run, one warm-up answer discarded. The whole table against the
        // empty environment, A/B interleaved in one sitting, 6 pairs: 88.95 -> 97.5 tok/s, +9.5%, 6 of 6 pairs in
        // favour and the two ranges not overlapping. VRAM 11,608 -> 11,610 MiB of 12,288. Within-arm spread <= 1.1%.
        //
        // The cost in points of removing each from the full table (drop-one, 3 rounds each) is on its line. The two
        // ADAPT variables are one knob and are measured, and shipped, together: see the note below them.
        t = {
            {"STRATA_ADAPT_NOWAIT", "1"},        // the pair: -3.66 points removed, the largest single contributor
            {"STRATA_ADAPT_LAG", "2"},           //           LAG=2 on its own is +4.2% of the +9.5%
            {"STRATA_SIDE_MIXER", "2"},          // -2.16
            {"STRATA_ATTN_DAILY", "1"},          // -2.06
            {"STRATA_GR_UP75", "1"},             // -1.07
            {"STRATA_QSA_ROWS", "1"},            // -0.42
            {"STRATA_B6_MMVQ_ROWS", "1"},        // -0.36
        };
        // NOT in the table, and both departures from upstream's list for sm_86 (their commit turns on nine):
        //
        // STRATA_CPU_IQ_ALL_EXACT1 cost upstream 3.3 points on an RTX A4000 and 0.6 on an RTX 3060. Here removing it
        // from the full table measured +0.02 tok/s, which is inside the noise: it buys this card nothing, so it does
        // not ship as a default. This is the whole reason the table was re-measured instead of copied.
        //
        // STRATA_GU_SWIGLU is upstream's ninth and is absent from this tree's sources; it needs a kernel this fork
        // does not have.
        //
        // And NOWAIT is kept paired with LAG rather than dropped, although it adds no measurable speed of its own
        // (the table without it measured 97.65 against 97.61). Dropping it while keeping LAG=2 made the output
        // UNSTABLE: two different answers in three runs of the same binary on the same prompt, where the full table
        // gave one answer in fourteen runs and the empty environment one in seventeen of eighteen. Pairing them is
        // upstream's choice too. Do not split them without re-measuring reproducibility, not just speed.
    }
    return t;
}

std::vector<std::string> apply_nv_defaults(int major, int minor) {
    std::vector<std::string> set;
    for (const auto& kv : nv_default_env(major, minor)) {
        if (std::getenv(kv.first.c_str()) != nullptr) continue;   // the user's setting wins, whatever it is
        // The return is checked before the name is reported. Reporting it regardless meant the startup line could
        // name a switch that is off -- and that line is the operator's only signal, so a wrong one is worse than
        // none. The switches themselves cannot be applied any other way, so a failure here is worth saying out loud.
        if (setenv(kv.first.c_str(), kv.second.c_str(), 0) != 0) {
            std::fprintf(stderr, "strata: sm_86: could not set %s, so it is NOT on: %s\n",
                         kv.first.c_str(), std::strerror(errno));
            continue;
        }
        set.push_back(kv.first);
    }
    if (!set.empty()) {
        std::fprintf(stderr, "strata: sm_86: %zu speed switches on by default, and the token ids differ from this "
                             "engine without them (docs/speed.md; STRATA_SM86_PERF=0 turns them off, a switch you "
                             "set is kept):", set.size());
        // Strip the shared prefix only where it is actually there. A bare `+ 7` reads past the end of the first
        // key that does not begin "STRATA_", and both this file's header and docs/speed.md invite someone to add
        // an entry.
        static constexpr char kPrefix[] = "STRATA_";
        static constexpr size_t kPrefixLen = sizeof(kPrefix) - 1;
        for (const std::string& name : set) {
            const bool prefixed = name.compare(0, kPrefixLen, kPrefix) == 0;
            std::fprintf(stderr, " %s", name.c_str() + (prefixed ? kPrefixLen : 0));
        }
        std::fprintf(stderr, "\n");
    }
    return set;
}

}  // namespace strata::core
