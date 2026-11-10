// include/strata/core/nv_defaults.hpp - the per-compute-capability defaults of NVIDIA speed switches.
//
// The CUDA counterpart of strata/core/arch_defaults.hpp, and it differs from that table in one way that matters, so
// read this before adding to it.
//
// The AMD tables next door in arch_defaults.hpp (gfx1151 AND gfx12/RDNA4) admit only switches proven
// BYTE-IDENTICAL, and keep every bit-changing switch opt-in.
// The sm_86 table below does NOT meet that bar and is not claimed to: with it on, this engine returns different token
// ids from the same prompt than with it off. Both answers are a valid greedy decode -- what changes is the point at
// which an expert swapped into the VRAM tier is first used, so a later token diverges and the rest of the text follows
// it. Nothing here is a rounding-level approximation of a kernel.
//
// That trade was made deliberately, for +9.5% decode at no VRAM cost, and it is disclosed in docs/speed.md with the
// measurements and with how to re-measure on another card. `STRATA_SM86_PERF=0` turns the whole table off and gets
// the old token ids back.
//
// Measured on ONE machine, ONE model and ONE quantisation (the reference machine in README.md; Qwen3.8-Flash-Next
// GSQ-RCO IQ3_XXS). Another Ampere card may well want a different set -- upstream's own list, measured on an RTX 3060
// and an RTX A4000, includes a switch worth nothing here. Treat the table as this card's answer, not Ampere's.
//
// A switch the user set, to anything, is never overridden.
#pragma once

#include <string>
#include <utility>
#include <vector>

namespace strata::core {

/// The environment settings that are the defaults for compute capability `major.minor`: empty for every capability
/// without a measured table (which is every one but 8.6).
std::vector<std::pair<std::string, std::string>> nv_default_env(int major, int minor);

/// Sets `nv_default_env(major, minor)` in this process's environment where the user has not set the variable, prints
/// one line to stderr when it set anything, and returns the names it set.  Call once, before any engine code reads
/// its switches (they are read lazily, at first use).
std::vector<std::string> apply_nv_defaults(int major, int minor);

}  // namespace strata::core
