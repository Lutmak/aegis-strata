// include/strata/core/tail_skip.hpp - whether the route tail skip is on, and what the engine says about it.
//
// WHAT THE SKIP IS. This model routes each token through ten of the 512 experts in each of its 48 layers. A card
// that cannot hold every expert misses some of them, and a miss is normally either copied over PCIe or computed on
// the CPU. The route tail skip is a third answer: a missed expert that EVERY token of a verify window routes at
// rank R or lower - the smallest of the ten router weights - is neither copied nor computed. Its contribution is
// zero, and there is no renormalisation, so the other experts keep the weight the router gave them and the layer's
// output is attenuated. R is an index from 0, so the skip can take the (10 - R) weakest: at the measured rank 7
// that is ranks 7, 8 and 9, and such a token is answered by seven to nine of its ten experts. The flag accepts 1
// to 9, though, so do not read "seven to nine" as a property of the feature -- at `--tail-skip 1` a token can
// lose nine of its ten. And a token whose missed experts all rank above R is answered in full, so this describes
// the worst a token can get, not what every token gets.
//
// WHY IT IS A SEPARATE DECISION FROM EVERY OTHER SPEED SETTING HERE. The engine's other default-on settings change
// the ORDER floating-point work happens in, so they move the token ids while computing the same model. This one
// computes less of the model. That is a different kind of trade, and the one the owner of the machine should make,
// so it is requested by name with `--tail-skip N` and is off until it is.
//
// The engine inherited it as a default-on setting for CUDA, where it was worth about a sixth of this card's decode
// speed. It is off here instead, and the shipped command line in the README decides for itself whether to ask for
// it. What it costs and what it buys, measured on this card, are in docs/speed.md.
#ifndef STRATA_CORE_TAIL_SKIP_HPP
#define STRATA_CORE_TAIL_SKIP_HPP

#include <string>

namespace strata::core {

/// What became of the request, so the engine can say one true thing in one line.
enum class TailSkipWhy {
    NotRequested,   ///< no `--tail-skip`: every routed expert is copied or computed
    RequestedOff,   ///< `--tail-skip 0`: the same outcome, but asked for, and the line says so
    Requested,      ///< `--tail-skip N`, and nothing stops it
    OutOfRange,     ///< `--tail-skip N` with N >= 10: the ten experts are ranked 0-9, so there is no such rank
    BatchSlots,     ///< `--batch`: one window mixes requests, so a skip would depend on another request's tokens
    PeerOrHelper,   ///< a peer tier or helper GPU holds experts; the dispatch does not skip there
};

/// What the operator asked for, and the two facts that can stop it being honoured. Deliberately NOT here: how much
/// of the expert set is resident. It cannot change the decision - the dispatch simply never finds a miss to skip
/// when everything is resident - and `--kv-grow` hands expert-cache slots to the context as a conversation fills,
/// so any claim this header made about what "will" be skipped could be falsified later in the same session.
struct TailSkipRequest {
    int  rank = 0;               ///< `--tail-skip N` as parsed; 0 = off
    int  batch_slots = 0;        ///< `--batch N`
    bool peer_or_helper = false; ///< a peer expert tier, or a helper GPU, is in use
    bool explicitly_set = false; ///< the operator passed `--tail-skip`, so rank 0 means "off, and I said so"
};

struct TailSkipDecision {
    int rank = 0;                              ///< the rank handed to `set_tail_skip_rank`; 0 = nothing is skipped
    TailSkipWhy why = TailSkipWhy::NotRequested;
    int requested = 0;                         ///< what was asked for, so a refusal can say so
    bool on() const { return rank > 0; }
};

/// Parse a `--tail-skip` value: one decimal digit, `0` to `9`, and nothing else - no sign, no spaces, no boolean
/// words. `rank` is left untouched unless the parse succeeds, so a caller that ignores the result cannot act on a
/// value that was never there. Returns false for a null pointer.
///
/// Strict because both directions of a silent misread are bad: a value that defaulted to "off" would hand back the
/// complete model while the operator believed they had asked for speed, and one that defaulted to "on" would hand
/// back less than the complete model while they believed the opposite. Neither is worth guessing, so a typo is an
/// error the caller reports.
bool parse_tail_skip(const char* text, int& rank);

/// The decision. Reads no environment and touches no device, which is what makes it testable without a GPU.
TailSkipDecision decide_tail_skip(const TailSkipRequest& req);

/// The one line the engine prints for the decision, without a trailing newline. Never empty: every outcome is
/// announced, including "off because you asked for it off", which the inherited code left silent and which this
/// file at first folded into the generic default line -- so an operator who typed `--tail-skip 0` was told about
/// a default they had overridden and then offered `--tail-skip 7` back.
///
/// The ON line describes what the dispatch WILL DO, not what every token got. Only a missed expert is ever a
/// candidate, so on a card that holds every expert nothing is skipped however this is set, and a line phrased as
/// an outcome would be an over-claim in exactly the way this file exists to prevent.
std::string tail_skip_line(const TailSkipDecision& d);

/// The rank the expert dispatch reads, and the one call that sets it. Here rather than beside the dispatch so that
/// the clamp and `decide_tail_skip`'s range check are the same bound in the same file: a rank the decision
/// announced and the dispatch then ignored is the exact disagreement this module exists to make impossible.
/// `set_tail_skip_rank` keeps 1-9 and stores 0 for anything else. Reads no environment.
int tail_skip_rank();
void set_tail_skip_rank(int r);

}  // namespace strata::core

#endif  // STRATA_CORE_TAIL_SKIP_HPP
