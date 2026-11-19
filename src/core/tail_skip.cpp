// src/core/tail_skip.cpp - see strata/core/tail_skip.hpp.
#include "strata/core/tail_skip.hpp"

#include <atomic>

namespace strata::core {

namespace {
// The rank from which a missed expert that every token of a verify window routes that low is skipped.  0 -- nothing
// is skipped -- until `set_tail_skip_rank` is called, which the start-up does with `decide_tail_skip`'s answer.
//
// It used to resolve itself from STRATA_ROUTE_TAIL_SKIP on first use, with the start-up overriding that for CUDA.
// Two things were wrong with that.  The environment decided how much of the model ran, so a value left in a shell
// rc or a systemd unit changed the answers of a process nobody had asked to change; and because the lazy read
// happened on whichever thread dispatched first, there was no single place that knew what had been decided and
// could say so.  One setter, called once, from a decision that reads nothing: the start-up line is now the whole
// truth about this setting.
std::atomic<int> g_tail_skip_rank{0};
}  // namespace

int tail_skip_rank() { return g_tail_skip_rank.load(std::memory_order_relaxed); }

void set_tail_skip_rank(int r) {
    // The same 1-9 bound `decide_tail_skip` enforces, and in the same file for that reason: while the two lived
    // apart, nothing stopped a decision of 10 from printing "ON at rank 10" here while the dispatch read 0.
    g_tail_skip_rank.store(r > 0 && r < 10 ? r : 0, std::memory_order_relaxed);
}

bool parse_tail_skip(const char* text, int& rank) {
    // One character, and it is a digit. Written out rather than handed to `atoi` or `from_chars` because both of
    // those accept things this must not: `atoi` reads "7x" as 7 and "" as 0 with no way to tell either from a real
    // value, and `from_chars` would still need the leading-sign and length checks written here anyway.
    if (text == nullptr || text[0] == 0 || text[1] != 0) return false;
    if (text[0] < '0' || text[0] > '9') return false;
    rank = text[0] - '0';
    return true;
}

TailSkipDecision decide_tail_skip(const TailSkipRequest& req) {
    TailSkipDecision d;
    d.requested = req.rank;
    if (req.rank <= 0) {
        // Asked for off, or not asked at all. Which of the two is kept, because the line below says different
        // things about them: an operator who typed `--tail-skip 0` is owed confirmation that the flag arrived,
        // and used to get text that discussed only the default and then advertised `--tail-skip 7` back at them.
        d.why = req.rank == 0 && req.explicitly_set ? TailSkipWhy::RequestedOff : TailSkipWhy::NotRequested;
        return d;
    }
    // Out of range before anything else. The dispatch stores the rank through `set_tail_skip_rank`, which clamps
    // to 1-9 silently, so a decision of 10 announced "ON at rank 10" while the engine skipped nothing -- the
    // printed line disagreeing with the dispatch, in the one direction this whole change exists to prevent.
    // `parse_tail_skip` accepts a single digit, so the CLI cannot reach this today; the guard is here because
    // the two numbers live in different translation units with nothing tying them together, and this header is
    // what callers are told to trust.
    if (req.rank >= 10) {
        d.why = TailSkipWhy::OutOfRange;
        return d;
    }

    // `--batch` first, and the order matters only for which reason a configuration with both is given. A window
    // under `--batch` holds tokens from several requests, and the gate is "every token of this window routes this
    // expert low", so whether one request lost an expert would depend on which other requests happened to share
    // its window. There is no reproducible answer, so the request is refused rather than approximated.
    if (req.batch_slots > 0) {
        d.why = TailSkipWhy::BatchSlots;
        return d;
    }
    // A peer tier or a helper GPU holds experts that this card's cache counts as missing; the dispatch does not
    // skip when either is present, so honouring the request here would print a line the dispatch then ignores.
    if (req.peer_or_helper) {
        d.why = TailSkipWhy::PeerOrHelper;
        return d;
    }
    d.rank = req.rank;
    d.why = TailSkipWhy::Requested;
    return d;
}

std::string tail_skip_line(const TailSkipDecision& d) {
    // Every outcome gets a line. The inherited code printed one only when the skip was ON, so the operator who had
    // deliberately asked for the complete model was the one it would not confirm -- and silence is also what a
    // build with the feature compiled out, or a flag that never reached the engine, looks like.
    if (d.on()) {
        // Phrased as what the dispatch will DO, not as what every token got. The two are not the same: with
        // every expert resident -- a larger card, or a smaller pack -- the dispatch skips nothing at all,
        // because only a missed expert is ever a candidate. The earlier wording ("this token is answered by
        // fewer than ten experts") asserted an outcome the engine may never produce, and it is the kind of
        // over-claim this file exists to stop. It also said "seven to nine of its ten", which is the rank-7
        // case read as if it were every rank: the flag takes 1 to 9, and at rank 1 a token can lose nine.
        const std::string r = std::to_string(d.rank);
        return "route tail skip: ON at rank " + r +
               " - when the GPU cache misses an expert that every token of a verify window routes at rank " + r +
               " or lower (counting from 0, so the " + std::to_string(10 - d.rank) +
               " weakest of the ten), it is neither copied nor computed: its contribution is zero, the other "
               "experts keep the weight the router gave them, and that layer's output is attenuated. Tokens "
               "whose missed experts all rank higher are answered in full";
    }
    const std::string asked = std::to_string(d.requested);
    switch (d.why) {
        case TailSkipWhy::BatchSlots:
            return "route tail skip: OFF - --tail-skip " + asked +
                   " was asked for, and --batch slots put several requests' tokens in one verify window, where one "
                   "request's skip would depend on another's tokens; every routed expert is computed";
        case TailSkipWhy::PeerOrHelper:
            return "route tail skip: OFF - --tail-skip " + asked +
                   " was asked for, and a peer expert tier or helper GPU is in use, where the dispatch does not "
                   "skip; every routed expert is computed";
        case TailSkipWhy::OutOfRange:
            return "route tail skip: OFF - --tail-skip " + asked +
                   " is out of range: the ten routed experts are ranked 0 to 9, so only 1 to 9 can be skipped. "
                   "Every routed expert is computed";
        case TailSkipWhy::RequestedOff:
            return "route tail skip: OFF - --tail-skip 0 was asked for, so every routed expert is copied or "
                   "computed and each token is answered by all ten of the experts its router chose";
        case TailSkipWhy::Requested:
        case TailSkipWhy::NotRequested:
        default:
            return "route tail skip: OFF - every routed expert is copied or computed, so each token is answered by "
                   "all ten of the experts its router chose; --tail-skip 7 drops the three weakest of the ten "
                   "when the GPU cache misses them, which is faster and is not the same model";
    }
}

}  // namespace strata::core
