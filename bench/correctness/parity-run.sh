#!/bin/bash
# Run-order step 2 (CORRECTNESS-PLAN.md section 4): the component parities whose formats are in use. Registered ones
# through ctest (one directory per test under parity/, with stdout+stderr and the exit code); the hand-run poly_*
# programs after. Reconfigures first so iq_parity registers now that numpy is present.
set -u
# Paths (the defaults are the homelab VM's; set them for another machine): US2_MODELS, the directory holding
# qwen36/<file>.gguf (required); US2_BUILD, the UnbiasedStrata tree with build/bin (default: this script's
# repository); US2_RESULTS, where results go (default ~/us2-results/correctness).
cd "${US2_BUILD:-$(cd "$(dirname "$0")/../.." && pwd)}" || exit 1
R=${US2_RESULTS:-$HOME/us2-results/correctness}/parity; mkdir -p $R
make configure CMAKE_EXTRA=-DSTRATA_BUILD_TESTS=ON CUDA_ARCHS=86 > $R/reconfigure.log 2>&1
cmake --build build -j 8 --target iq_parity >> $R/reconfigure.log 2>&1; echo "iq_parity build exit $?" >> $R/reconfigure.log
ctest --test-dir build -N > $R/ctest-list.txt 2>&1
A="iq_parity iq_avx2_parity iq_avx2_parity_iq3s_mt1 q8k_quant_parity q8k_quant_parity_avx native_grouped_parity iq_multi_parity mmvq_multi_parity prefill_fused_iq_test prefill_mmq_scatter_test kv_q8_parity kv_stream_parity kv_steps_parity kv_window_parity gdn_parity gdn_rec_parity qsa_parity qsa_topk_parity verify_parity spec_verify_parity sampler_parity native_multi_parity rope_parity elementwise_parity decode_cluster_parity quantize_act_parity dequant_wide_parity pdl_parity dma_batch_parity batch_rows_parity verify_batch_parity router_top10_parity route_window_parity native_expert_parity_q4_K_q8_0 native_expert_parity_q6_K_q8_0 native_expert_parity_q8_0_q8_0 poly_kv_q8_test poly_workers_test"
for t in $A; do
  d=$R/$t; mkdir -p $d
  cmd=$(ctest --test-dir build -N -V -R "^$t\$" 2>/dev/null | grep "Test command" | sed 's/.*Test command: //')
  echo "$cmd" > $d/command.sh
  if [ -z "$cmd" ]; then echo "NOT_REGISTERED_OR_NOT_BUILT" > $d/exit.txt; echo "$t: not registered"; continue; fi
  ( cd build && timeout 1500 ctest --test-dir . -R "^$t\$" --output-on-failure --timeout 1200 -V > $d/ctest.txt 2>&1; echo $? > $d/exit.txt )
  echo "$t: exit $(cat $d/exit.txt) $(grep -oE "[0-9]+% tests passed|Passed|Failed|Not Run|Timeout" $d/ctest.txt | head -2 | tr '\n' ' ')"
done
# hand-run programs (section 2.2 "hand")
M=${US2_MODELS:?set US2_MODELS to the directory holding qwen36/}/qwen36
Q3=$M/Qwen3.6-35B-A3B-UD-Q3_K_M.gguf; IQ4=$M/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
run() { n=$1; shift; d=$R/$n; mkdir -p $d; echo "$*" > $d/command.sh; timeout 3600 "$@" > $d/stdout.txt 2> $d/stderr.txt; echo $? > $d/exit.txt; echo "$n: exit $(cat $d/exit.txt), $(wc -l < $d/stdout.txt) stdout lines"; }
B=$PWD/build/bin
run poly_route_test $B/poly_route_test
run poly_norm_q8_test $B/poly_norm_q8_test
run poly_gemm_small_test $B/poly_gemm_small_test
run poly_attend_test $B/poly_attend_test
run poly_expert_parity_q3km $B/poly_expert_parity $Q3
run poly_expert_parity_iq4xs $B/poly_expert_parity $IQ4
run poly_draft_race_test_q3km $B/poly_draft_race_test $Q3
echo ALL_DONE
