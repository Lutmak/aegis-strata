// include/strata/prefill/moe_iq4_w4a8.hpp - the routed experts' IQ4_XS / IQ4_NL prompt products on int8 tensor
// cores (sm_80+), a pipelined replacement for llama.cpp's MMQ tiling-mode launch with the same bits.
#pragma once

#include "strata/prefill/moe_mmq.hpp"

namespace strata::prefill::mmq {

/// Runs p (IQ4_XS or IQ4_NL weights, q8_1 activations as Context::run takes them, experts at w + z * expert_bytes or
/// tab->p[z]) and returns true; returns false without launching anything when it does not cover p (another type,
/// rows not a multiple of 128, a row table, or a launch MMQ would split along K with its stream-k fixup - those sums
/// are added in a different order), and the caller runs Context::run.  Every output it writes is MMQ's tiling-mode
/// sum bit for bit: per 32-value block the exact int32 dot, then sum += float(dot) * d_w * d_y in block order.
///
/// REQUIRED of `p.xq`: it must be sized by `mmq::q8_bytes`, not by the live row count.  The kernel copies whole
/// 8-row activation groups, so it reads up to 7 rows (1,008 bytes) past the last live row; `q8_bytes` ends with
/// 128 * sizeof(block_q8_1_mmq) = 18,432 bytes of tail, which is what keeps that read inside the allocation.  A
/// caller that sizes `xq` exactly gets an out-of-bounds device read, and nothing here can detect it.
bool run_iq4_w4a8(const Product& p, void* stream);

/// STRATA_IQ4_W4A8: 0 = off (the default); 1 = Context::run tries run_iq4_w4a8 first for every product; 2 = for the
/// IQ4_NL ones only (the down products - where MMQ wastes the most: K 640 in 256-value iterations).
int iq4_w4a8_mode();

/// The compute capabilities run_iq4_w4a8 will accept.  Exported so the test and this header state the rule once:
/// it was written out by hand in tests/cuda/prefill_iq4_w4a8_test.cu and paraphrased a third time in CMakeLists.txt,
/// where the paraphrase had already lost the cc 120 arm.  A card that passes this can still be refused for want of a
/// device image in this binary -- run_iq4_w4a8 reports that separately.
bool w4a8_arch_ok(int cc);

/// STRATA_W4A8_CFG, read once: which tile width run_iq4_w4a8 launches, 0 (the default, 128 tokens per CTA) .. 3.
/// Exposed so a test can report which configuration it checked rather than leaving it implicit.
int w4a8_config();

}  // namespace strata::prefill::mmq
