#!/bin/bash
# serve.sh <case> : fork front end + strata-poly on the 2x RTX PRO 6000 (96 GB) pod, 2026-10-19.  Engine args as
# docs/DEEPSEEK.md and docs/GLM.md; the recorded 2x B200 variant is bench/unbiased/2026-10-19-step13-acceptance/servers/.
#   deepseek      UD-IQ2_XXS, one card, 128K           (91 GB resident on a 96 GB card)
#   deepseek-q4   UD-Q4_K_XL, both cards, 256K         (155 GB of weights: --layer-split auto, experts beyond VRAM streamed)
#   deepseek-q8   UD-Q8_K_XL, both cards, 256K         (162 GB: same)
#   glm           GLM 5.3 Flash 3.0bit, one card, 128K (113 GB: experts beyond VRAM streamed)
# FE_EXTRA=--verbose for the front end's request log. Ports: DeepSeek 18095, GLM 18096. Logs and profiles in /opt/us2-servers.
set -u; FE_EXTRA=${FE_EXTRA:-}
F=/opt/us2-build; M=/opt/us2-models; S=/opt/us2-servers; mkdir -p $S; cd $S
[ -f $S/glm.profile ] || cp $F/data/expert-profile-glm.txt $S/glm.profile   # the shipped GLM expert ranking as the seed
D=$M/deepseek
case $1 in
  deepseek)    GPU=0;   PORT=18095; ALIAS=deepseek-v4-flash-ud-iq2_xxs-rtxpro; MODEL=$D/UD-IQ2_XXS/DeepSeek-V4-Flash-0731-UD-IQ2_XXS-00001-of-00003.gguf
               ARGS="--model $MODEL --max-context 131072 --expert-profile $S/deepseek.profile --conversation-cache-mib 8192";;
  deepseek-q4) GPU=0,1; PORT=18095; ALIAS=deepseek-v4-flash-ud-q4_k_xl-rtxpro; MODEL=$D/UD-Q4_K_XL/DeepSeek-V4-Flash-0731-UD-Q4_K_XL-00001-of-00005.gguf
               ARGS="--model $MODEL --max-context 262144 --layer-split auto --expert-profile $S/deepseek.profile --conversation-cache-mib 8192";;
  deepseek-q8) GPU=0,1; PORT=18095; ALIAS=deepseek-v4-flash-ud-q8_k_xl-rtxpro; MODEL=$D/UD-Q8_K_XL/DeepSeek-V4-Flash-0731-UD-Q8_K_XL-00001-of-00005.gguf
               ARGS="--model $MODEL --max-context 262144 --layer-split auto --expert-profile $S/deepseek.profile --conversation-cache-mib 8192";;
  glm)         GPU=1;   PORT=18096; ALIAS=glm-5.3-flash-3.0bit-rtxpro; MODEL=$M/glm/GLM-5.3-Flash-GSQ-RCO-3.0bit-q4kattn.gguf
               ARGS="--model $MODEL --max-context 131072 --expert-profile $S/glm.profile --conversation-cache-mib 8192";;
  *) echo "deepseek|deepseek-q4|deepseek-q8|glm"; exit 2;;
esac
[ -f "$MODEL" ] || { echo "missing $MODEL"; exit 1; }
echo "$(date -u +%FT%TZ) start $1 gpu $GPU port $PORT" >> $S/starts.log
ulimit -l unlimited 2>/dev/null
exec env CUDA_VISIBLE_DEVICES=$GPU $F/build/bin/unbiased-strata --engine $F/build/bin/strata-poly -m $MODEL --host 0.0.0.0 --port $PORT --alias $ALIAS --sse-ping-interval 10 $FE_EXTRA -- $ARGS
