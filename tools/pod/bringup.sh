#!/bin/bash
# UnbiasedStrata 2 pod bring-up. bringup.sh <cuda-arch> <small|big>
#   small: the two 24 GB-class models (Flash-Next IQ3_XXS, Qwen3.6 UD-Q3_K_M + UD-IQ4_XS + mmproj); big: those plus
#   DeepSeek V4 Flash UD-IQ2_XXS and GLM 5.3 Flash 3.0bit. Everything fresh from Hugging Face onto the container disk
#   (/opt/us2-models; the RunPod network volume is too slow to serve from). The fork tree is expected at /opt/us2-build
#   (rsync'd from the Mac, submodule checkout included). Logs: /opt/us2-bringup/*.log; BRINGUP_DONE at the end.
set -u
ARCH=${1:?cuda arch}; SIZE=${2:?small|big}
export PATH=/usr/local/cuda/bin:$PATH
B=/opt/us2-bringup; M=/opt/us2-models; F=/opt/us2-build; R=/opt/llamacpp-ref
mkdir -p $B $M $R; cd $B
echo "bringup start $(date -u +%FT%TZ) arch=$ARCH size=$SIZE"
pip install -q --break-system-packages huggingface_hub regex numpy > pip.log 2>&1 && echo "pip ok" || echo "pip FAILED (see pip.log)"
HF=$B/hfget.py
# --- downloads, in parallel ---
( python3 $HF unsloth/Qwen3.6-35B-A3B-MTP-GGUF 5bc3e238 $M/qwen36-mtp "Qwen3.6-35B-A3B-UD-Q3_K_M.gguf" "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf" "mmproj-F16.gguf" ) > dl-qwen36.log 2>&1 &
( python3 $HF ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF ed59f92082b1e93c0e96d60a8b11aab089b52f09 $M "IQ3_XXS/*" "mmproj-Qwen3.8-Flash-Next-BF16.gguf" ) > dl-flash-next.log 2>&1 &
if [ "$SIZE" = big ]; then
  ( python3 $HF unsloth/DeepSeek-V4-Flash-0731-GGUF fbbb5b93fb787c21338159b0af3318bb3f4d9768 $M/deepseek "UD-IQ2_XXS/*" ) > dl-deepseek.log 2>&1 &
  ( python3 $HF neuralll/GLM-5.3-Flash-GSQ-RCO-3.0bit-Q4Kattn-GGUF 7feedfeb5ee924bfbe0064c63b124728b443ddc0 $M/glm "*.gguf" ) > dl-glm.log 2>&1 &
fi
# --- builds, while the downloads run ---
( cd $F && make build CUDA_ARCHS=$ARCH CMAKE_EXTRA=-DSTRATA_BUILD_TESTS=ON > $B/build-fork.log 2>&1 && echo "fork build ok" || echo "fork build FAILED (build-fork.log)" ) &
( cmake -S $F/third_party/llama.cpp -B $R/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=$ARCH -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release > $B/build-ref.log 2>&1 \
  && cmake --build $R/build --target llama -j 32 >> $B/build-ref.log 2>&1 \
  && mkdir -p $R/tools && g++ -O2 -std=c++17 $F/tools/qwen35/q36_llama_dump.cpp -I$F/third_party/llama.cpp/include -I$F/third_party/llama.cpp/ggml/include -L$R/build/bin -Wl,-rpath,$R/build/bin -lllama -lggml -lggml-base -o $R/tools/q36_llama_dump >> $B/build-ref.log 2>&1 \
  && echo "reference build ok" || echo "reference build FAILED (build-ref.log)" ) &
wait
echo "downloads and builds finished $(date -u +%FT%TZ)"; tail -n 1 dl-*.log
# --- Flash-Next pack and MTP draft directory (upstream's tools, as its installer runs them) ---
SH1=$M/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf
( cd $F && python3 tools/iq_pack.py --gguf $SH1 --out $M/pack-iq3-xxs > $B/pack.log 2>&1 && cp data/expert-profile.bin $M/pack-iq3-xxs/expert-profile.bin && echo "pack ok" || echo "pack FAILED (pack.log)" ) &  # the shipped expert ranking, as the installer places it; the engine exits without it
( cd $F && STRATA_SOURCE=huggingface python3 tools/mtp_fetch.py fetch --out $M/mtp-src > $B/mtp.log 2>&1 \
  && python3 tools/mtp_pack.py --src $M/mtp-src --experts q2_0 --out $M/mtp-src/mtp-q2_0.gguf >> $B/mtp.log 2>&1 \
  && python3 tools/mtp_rt.py --gguf $M/mtp-src/mtp-q2_0.gguf --out $M/mtp >> $B/mtp.log 2>&1 \
  && cp data/draft_vocab.bin $M/mtp/draft_vocab.bin && echo "mtp ok" || echo "mtp FAILED (mtp.log)" ) &
wait
# --- hashes of what will be served ---
( cd $M && find . -name "*.gguf" -type f | sort | xargs sha256sum > SHA256SUMS ) 2>/dev/null
echo "bringup end $(date -u +%FT%TZ)"; echo BRINGUP_DONE
