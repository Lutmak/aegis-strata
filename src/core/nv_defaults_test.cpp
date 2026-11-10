// src/core/nv_defaults_test.cpp - the sm_86 default table (strata/core/nv_defaults.hpp). No GPU, no model.
//
// What is worth testing here is not the speed -- that is measured, not asserted -- but the table's contract: it
// applies to sm_86 and nothing else, it never overrides a switch the operator set, STRATA_SM86_PERF=0 turns it off,
// and the two ADAPT variables stay together. Each of those is a way an operator could be surprised.
#include "strata/core/nv_defaults.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

static int failures = 0;
static int checks = 0;

#define CHECK(cond)                                                                      \
    do {                                                                                 \
        ++checks;                                                                        \
        if (!(cond)) {                                                                   \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
            ++failures;                                                                  \
        }                                                                                \
    } while (0)

using strata::core::apply_nv_defaults;
using strata::core::nv_default_env;

static bool has(const std::vector<std::pair<std::string, std::string>>& t, const std::string& k,
                const std::string& v) {
    return std::any_of(t.begin(), t.end(), [&](const auto& kv) { return kv.first == k && kv.second == v; });
}

static bool names(const std::vector<std::string>& v, const std::string& k) {
    return std::find(v.begin(), v.end(), k) != v.end();
}

int main() {
    // FIRST, before any assertion: clear every variable this code reads. Without this the suite went red for any
    // operator who had STRATA_SM86_PERF=0 exported -- which is exactly what nv_defaults.hpp, docs/speed.md and the
    // engine's own startup line tell them to set -- and `make test` stopped here, before e2e.sh, check_docs.py and
    // sandbox_test ran at all. STRATA_EMULATE_CC had the same hold, because the capability test goes through
    // cc_major_of/cc_minor_of. A test for a default must not inherit the environment that overrides the default.
    for (const char* name : {"STRATA_SM86_PERF", "STRATA_EMULATE_CC",
                             "STRATA_ADAPT_NOWAIT", "STRATA_ADAPT_LAG", "STRATA_SIDE_MIXER", "STRATA_ATTN_DAILY",
                             "STRATA_GR_UP75", "STRATA_QSA_ROWS", "STRATA_B6_MMVQ_ROWS",
                             "STRATA_CPU_IQ_ALL_EXACT1", "STRATA_GU_SWIGLU"}) {
        unsetenv(name);
    }

    // The table is sm_86's alone: no other Ampere capability, and nothing above or below it, has been measured.
    CHECK(nv_default_env(8, 6).size() == 7);
    CHECK(nv_default_env(8, 0).empty());   // A100
    CHECK(nv_default_env(8, 9).empty());   // RTX 40
    CHECK(nv_default_env(7, 5).empty());
    CHECK(nv_default_env(9, 0).empty());
    CHECK(nv_default_env(12, 0).empty());
    CHECK(nv_default_env(0, 0).empty());

    // Every switch the measurement justified, at the value it was measured at.
    const auto t = nv_default_env(8, 6);
    CHECK(has(t, "STRATA_ADAPT_NOWAIT", "1"));
    CHECK(has(t, "STRATA_ADAPT_LAG", "2"));
    CHECK(has(t, "STRATA_SIDE_MIXER", "2"));
    CHECK(has(t, "STRATA_ATTN_DAILY", "1"));
    CHECK(has(t, "STRATA_GR_UP75", "1"));
    CHECK(has(t, "STRATA_QSA_ROWS", "1"));
    CHECK(has(t, "STRATA_B6_MMVQ_ROWS", "1"));

    // The two that are deliberately NOT defaults, so a well-meaning edit that adds them has to change this test and
    // read why. CPU_IQ_ALL_EXACT1 measured as worth nothing on this card; GU_SWIGLU has no kernel in this tree.
    CHECK(!has(t, "STRATA_CPU_IQ_ALL_EXACT1", "1"));
    CHECK(!has(t, "STRATA_GU_SWIGLU", "1"));

    // ADAPT_NOWAIT and ADAPT_LAG are one knob: splitting them made the output unstable run to run, so the table must
    // never carry one without the other.
    const bool nowait = has(t, "STRATA_ADAPT_NOWAIT", "1");
    const bool lag = has(t, "STRATA_ADAPT_LAG", "2");
    CHECK(nowait == lag);

    // STRATA_SM86_PERF is an off switch and it FAILS CLOSED: anything that is not plainly "on" disables the
    // table. It used to test only the first character, so `=` (the usual way to blank a variable), `false`, `no`
    // and `off` all left the switches ON -- the one escape hatch for a change that alters the model's token ids,
    // failing open. Losing ~10% speed by accident is visible (no startup line); silently keeping output-changing
    // switches when the operator asked for reproducibility is not.
    for (const char* v : {"0", "", "false", "FALSE", "no", "off", "Off", "0abc", "00", "nonsense", " "}) {
        setenv("STRATA_SM86_PERF", v, 1);
        CHECK(nv_default_env(8, 6).empty());
    }
    for (const char* v : {"1", "true", "TRUE", "yes", "on", "On"}) {
        setenv("STRATA_SM86_PERF", v, 1);
        CHECK(nv_default_env(8, 6).size() == 7);
    }
    unsetenv("STRATA_SM86_PERF");
    CHECK(nv_default_env(8, 6).size() == 7);

    // apply: sets what the operator has not set, and reports exactly those.
    for (const auto& kv : nv_default_env(8, 6)) unsetenv(kv.first.c_str());
    const auto set = apply_nv_defaults(8, 6);
    CHECK(set.size() == 7);
    CHECK(names(set, "STRATA_ADAPT_LAG"));
    const char* lag_v = std::getenv("STRATA_ADAPT_LAG");
    CHECK(lag_v != nullptr && std::string(lag_v) == "2");

    // Applying twice sets nothing the second time: the values are already there, and the operator's environment --
    // which is what it now looks like -- wins.
    CHECK(apply_nv_defaults(8, 6).empty());

    // The operator's own setting is kept, including one that turns a switch off. This is the case that would be
    // worst to get wrong: a default silently overriding a deliberate STRATA_SIDE_MIXER=0.
    for (const auto& kv : nv_default_env(8, 6)) unsetenv(kv.first.c_str());
    setenv("STRATA_SIDE_MIXER", "0", 1);
    setenv("STRATA_GR_UP75", "7", 1);
    const auto set2 = apply_nv_defaults(8, 6);
    CHECK(!names(set2, "STRATA_SIDE_MIXER"));
    CHECK(!names(set2, "STRATA_GR_UP75"));
    CHECK(set2.size() == 5);
    CHECK(std::string(std::getenv("STRATA_SIDE_MIXER")) == "0");
    CHECK(std::string(std::getenv("STRATA_GR_UP75")) == "7");

    // A capability with no table sets nothing at all.
    for (const auto& kv : nv_default_env(8, 6)) unsetenv(kv.first.c_str());
    CHECK(apply_nv_defaults(8, 9).empty());
    CHECK(std::getenv("STRATA_ADAPT_LAG") == nullptr);

    if (failures == 0) {
        std::printf("nv_defaults_test: %d assertions passed\n", checks);
        return 0;
    }
    std::fprintf(stderr, "nv_defaults_test: %d of %d assertions failed\n", failures, checks);
    return 1;
}
