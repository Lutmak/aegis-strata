// src/core/tail_skip_test.cpp - the route tail skip's decision (strata/core/tail_skip.hpp). No GPU, no model.
//
// The skip is the one setting in this engine that changes what is COMPUTED rather than the order things are added
// in: a missed expert that every token of a verify window routes at rank R or lower is neither copied nor
// computed, and its contribution is zero without renormalisation. Ranks count from 0, so at the measured R = 7
// that is ranks 7, 8 and 9 and such a token is answered by seven to nine of its ten experts -- but the flag takes
// 1 to 9, and at R = 1 a token can lose nine, so that range is the measured case and not a property of the
// feature. That makes it an owner's decision, and the contract worth asserting is the shape of that decision:
//
//   - nothing is skipped unless the operator asked for it, whatever else is true;
//   - the two configurations where the dispatch cannot honour a request say so instead of skipping;
//   - the value is parsed strictly, because a typo that read as "off" would quietly return the complete model
//     while a typo that read as "on" would quietly return less than it, and neither should be guessable;
//   - the environment variable this used to be read from no longer decides anything -- asserted against the
//     real getter, which is the thing that used to read it, and not merely against the decision;
//   - a rank the decision would announce is a rank the dispatch will actually use, because the two share this
//     file: announcing "ON at rank 10" while the dispatch stored 0 was reachable while they lived apart;
//   - every outcome produces exactly one line, so an operator can never be left guessing which they got.
//
// The speeds and the quality cost are measured, not asserted; docs/speed.md carries them.
#include "strata/core/tail_skip.hpp"

#include <cstdio>
#include <cstdlib>
#include <string>

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

using strata::core::decide_tail_skip;
using strata::core::parse_tail_skip;
using strata::core::TailSkipDecision;
using strata::core::TailSkipRequest;
using strata::core::tail_skip_line;
using strata::core::TailSkipWhy;

static bool contains(const std::string& haystack, const char* needle) {
    return haystack.find(needle) != std::string::npos;
}

int main() {
    // The variable this setting used to be read from, exported as an operator following any older document would
    // have exported it. Every assertion below must hold with it set, because "the environment no longer decides"
    // is half the point of the change: a stale STRATA_ROUTE_TAIL_SKIP=7 in a shell rc or a systemd unit must not
    // put the engine back to answering from nine experts without saying so.
    setenv("STRATA_ROUTE_TAIL_SKIP", "7", 1);
    setenv("STRATA_TAIL_SKIP_MAX_CACHED", "90", 1);

    // ---- nothing is skipped unless it was asked for -------------------------------------------------------------
    // Every combination of the other facts, at rank 0. There is no path to a non-zero rank without a request, and
    // this is the assertion that pins the change: the engine's own default is now the complete model.
    for (int batch : {0, 1, 4}) {
        for (bool peer : {false, true}) {
            TailSkipRequest req;
            req.rank = 0;
            req.batch_slots = batch;
            req.peer_or_helper = peer;
            const TailSkipDecision d = decide_tail_skip(req);
            CHECK(d.rank == 0);
            CHECK(!d.on());
            CHECK(d.why == TailSkipWhy::NotRequested);
        }
    }

    // ---- a request is honoured, at the rank asked for -----------------------------------------------------------
    for (int rank = 1; rank <= 9; ++rank) {
        TailSkipRequest req;
        req.rank = rank;
        const TailSkipDecision d = decide_tail_skip(req);
        CHECK(d.rank == rank);
        CHECK(d.on());
        CHECK(d.why == TailSkipWhy::Requested);
    }

    // ---- the two configurations the dispatch cannot honour ------------------------------------------------------
    // --batch: one window mixes several requests' tokens, and the gate is "every token of this window routes it
    // low", so one request's answer would depend on which other requests happened to share its window. Refusing
    // is the only reproducible answer.
    {
        TailSkipRequest req;
        req.rank = 7;
        req.batch_slots = 4;
        const TailSkipDecision d = decide_tail_skip(req);
        CHECK(d.rank == 0);
        CHECK(d.why == TailSkipWhy::BatchSlots);
    }
    // A peer tier or helper GPU holds experts the dispatch would otherwise count as missed; it does not skip there.
    {
        TailSkipRequest req;
        req.rank = 7;
        req.peer_or_helper = true;
        const TailSkipDecision d = decide_tail_skip(req);
        CHECK(d.rank == 0);
        CHECK(d.why == TailSkipWhy::PeerOrHelper);
    }
    // Both at once: still off, and it names one reason rather than inventing a third.
    {
        TailSkipRequest req;
        req.rank = 7;
        req.batch_slots = 2;
        req.peer_or_helper = true;
        const TailSkipDecision d = decide_tail_skip(req);
        CHECK(d.rank == 0);
        CHECK(d.why == TailSkipWhy::BatchSlots || d.why == TailSkipWhy::PeerOrHelper);
    }

    // ---- the value is parsed strictly ---------------------------------------------------------------------------
    // Accepted: one digit, 0 through 9. Rank 10 would mean "skip an expert the router put in its top ten at any
    // rank", which is every miss, and there is no such setting.
    for (int rank = 0; rank <= 9; ++rank) {
        const std::string text = std::to_string(rank);
        int out = -1;
        CHECK(parse_tail_skip(text.c_str(), out));
        CHECK(out == rank);
    }
    // Rejected, and `out` is left alone so a caller that ignores the return value cannot act on a parsed value
    // that does not exist. "on", "true" and "yes" are rejected deliberately: this is a rank, not a boolean, and a
    // reader who writes `--tail-skip on` has not said which rank they meant.
    for (const char* bad : {"", " ", "7x", "x7", "-1", "10", "99", " 7", "7 ", "+7", "on", "true", "yes", "off",
                            "0x7", "7.0", "seven"}) {
        int out = -42;
        CHECK(!parse_tail_skip(bad, out));
        CHECK(out == -42);
    }
    {
        int out = -42;
        CHECK(!parse_tail_skip(nullptr, out));
        CHECK(out == -42);
    }

    // ---- every outcome produces exactly one line ----------------------------------------------------------------
    // An operator's only signal is this line, so the states must not be confusable. The engine used to print
    // nothing at all when the skip was off because someone had turned it off -- the one configuration where
    // somebody had deliberately asked for the complete model was the one it would not confirm.
    {
        TailSkipRequest req;
        req.rank = 0;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(!line.empty());
        CHECK(contains(line, "route tail skip: OFF"));
        CHECK(!contains(line, "ON at rank"));
        CHECK(line.find('\n') == std::string::npos);   // the caller adds the newline
    }
    {
        TailSkipRequest req;
        req.rank = 7;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(contains(line, "route tail skip: ON at rank 7"));
        CHECK(!contains(line, "OFF"));
    }
    // A rank other than 7, because 7 is the only rank anyone has measured and is therefore the one a hard-coded
    // string would carry. The line explains itself in prose as well as naming the rank, and both have to agree:
    // "ON at rank 3 ... routes at rank 7 or lower" is a line that contradicts itself, and a reader would have to
    // guess which half to believe.
    {
        TailSkipRequest req;
        req.rank = 3;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(contains(line, "route tail skip: ON at rank 3"));
        CHECK(contains(line, "at rank 3 or lower"));
        CHECK(!contains(line, "rank 7"));
    }
    // A refusal says what was asked for as well as that it was refused: "OFF" alone would read as "you did not
    // ask", and the operator did ask.
    {
        TailSkipRequest req;
        req.rank = 7;
        req.batch_slots = 4;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(contains(line, "route tail skip: OFF"));
        CHECK(contains(line, "7"));
        CHECK(contains(line, "--batch"));
    }
    {
        TailSkipRequest req;
        req.rank = 7;
        req.peer_or_helper = true;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(contains(line, "route tail skip: OFF"));
        CHECK(contains(line, "7"));
    }
    // The off line names the flag that turns it on, because a document can go stale and this line cannot.
    {
        TailSkipRequest req;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(contains(line, "--tail-skip"));
    }

    // ---- the rank the decision announces is the rank the dispatch will read -----------------------------------
    // The getter is what used to resolve STRATA_ROUTE_TAIL_SKIP for itself, and it now lives in this module, so
    // this file can finally assert the claim the comment above makes. While it lived elsewhere, re-adding the
    // lazy getenv there would have left all of these checks green: the test named a regression it could not see.
    strata::core::set_tail_skip_rank(0);
    CHECK(strata::core::tail_skip_rank() == 0);
    for (int r : {1, 5, 7, 9}) {
        strata::core::set_tail_skip_rank(r);
        CHECK(strata::core::tail_skip_rank() == r);
    }
    // Out of range, from either end, stores "nothing is skipped" rather than an invented rank.
    for (int r : {-1, 0, 10, 42, 999}) {
        strata::core::set_tail_skip_rank(r);
        CHECK(strata::core::tail_skip_rank() == 0);
    }
    // With STRATA_ROUTE_TAIL_SKIP=7 exported (top of this function) the getter must still say 0 after being set
    // to 0. This is the assertion that fails if the environment read ever comes back.
    strata::core::set_tail_skip_rank(0);
    CHECK(strata::core::tail_skip_rank() == 0);
    CHECK(std::getenv("STRATA_ROUTE_TAIL_SKIP") != nullptr);   // the premise of the check above

    // And the decision never hands the setter a rank the setter would refuse: whatever the decision says is ON
    // survives the store, for every rank the flag accepts.
    for (int r = 1; r <= 9; ++r) {
        TailSkipRequest req;
        req.rank = r;
        const TailSkipDecision d = decide_tail_skip(req);
        CHECK(d.on());
        strata::core::set_tail_skip_rank(d.rank);
        CHECK(strata::core::tail_skip_rank() == r);
    }
    // A rank past the ten routed experts is refused by the decision, named in the line, and leaves nothing set.
    for (int r : {10, 11, 99}) {
        TailSkipRequest req;
        req.rank = r;
        const TailSkipDecision d = decide_tail_skip(req);
        CHECK(!d.on());
        CHECK(d.why == TailSkipWhy::OutOfRange);
        CHECK(d.requested == r);
        const std::string line = tail_skip_line(d);
        CHECK(contains(line, "route tail skip: OFF"));
        CHECK(contains(line, "out of range"));
        CHECK(contains(line, std::to_string(r).c_str()));
        strata::core::set_tail_skip_rank(d.rank);
        CHECK(strata::core::tail_skip_rank() == 0);
    }

    // ---- "off because I asked for it off" is not the same line as "off by default" -----------------------------
    // Both compute the whole model, so nothing is wrong either way, but an operator who typed --tail-skip 0 was
    // told about a default they had just overridden and then offered --tail-skip 7 back.
    {
        TailSkipRequest asked;
        asked.rank = 0;
        asked.explicitly_set = true;
        const TailSkipDecision d_asked = decide_tail_skip(asked);
        CHECK(!d_asked.on());
        CHECK(d_asked.why == TailSkipWhy::RequestedOff);
        const std::string asked_line = tail_skip_line(d_asked);
        CHECK(contains(asked_line, "route tail skip: OFF"));
        CHECK(contains(asked_line, "--tail-skip 0 was asked for"));

        TailSkipRequest silent;
        silent.rank = 0;
        const TailSkipDecision d_silent = decide_tail_skip(silent);
        CHECK(d_silent.why == TailSkipWhy::NotRequested);
        CHECK(tail_skip_line(d_silent) != asked_line);
    }

    // ---- the ON line describes the setting, not an outcome every token gets ------------------------------------
    // With every expert resident the dispatch skips nothing whatever this is set to, because only a MISSED expert
    // is a candidate. A line that said "this token is answered by fewer than ten experts" asserted something the
    // engine may never produce, and at rank 1 the old "seven to nine" was wrong by six.
    for (int r : {1, 3, 7, 9}) {
        TailSkipRequest req;
        req.rank = r;
        const std::string line = tail_skip_line(decide_tail_skip(req));
        CHECK(contains(line, "when the GPU cache misses"));
        CHECK(contains(line, "answered in full"));
        CHECK(contains(line, std::to_string(10 - r).c_str()));   // how many of the ten this rank can take
        CHECK(!contains(line, "seven to nine"));
    }

    std::fprintf(stderr, "tail_skip_test: %d checks, %d failed\n", checks, failures);
    return failures == 0 ? 0 : 1;
}
