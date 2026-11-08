# Pod bring-up

`bringup.sh <cuda-arch> <small|big>` provisions a fresh RunPod box for this fork: the fork is built from the tree
at `/opt/us2-build` (rsync it there first, submodule checkout included: `rsync -a --exclude='/build' --exclude='/build-frontend'
./ root@pod:/opt/us2-build/`; an unanchored `--exclude='build*'` also drops llama.cpp's `build-info.cmake` and the
configure fails; macOS's openrsync has no `--info` flag), llama.cpp at the pin is built as the correctness reference with the dump tool
(`/opt/llamacpp-ref`), and every model file is downloaded fresh from Hugging Face at the pinned revision onto the
container disk (`/opt/us2-models`). Never serve from `/workspace`: the RunPod network volume read at 150 MB/s idle
and stalled to a few MB/s under the engine's expert-cache fill (2026-10-17). `small` is the two 24 GB-class models
(Flash-Next IQ3_XXS, Qwen3.6 UD-Q3_K_M and UD-IQ4_XS with the vision projector); `big` adds DeepSeek V4 Flash
UD-IQ2_XXS and GLM 5.3 Flash 3.0bit. The Flash-Next pack and MTP directory are made with upstream's tools as its
installer runs them. Logs under `/opt/us2-bringup/`; `BRINGUP_DONE` on the last line of `bringup.log`.

Pod facts that matter: RunPod exposes every host CPU but enforces a cgroup quota (`/sys/fs/cgroup/cpu.max`: 31 cores
on the 3090 pod, 46 on the B200 pod), so `--pool-workers` must be set below it or the engine pins threads it cannot
run; `nvcc` lives in `/usr/local/cuda/bin`, not on PATH; `pip` needs `--break-system-packages` on Ubuntu 24.04;
`pkill -f` with an unanchored pattern over ssh kills the ssh session itself.

Copying the tree: from this Mac to a pod, rsync of the 8.8 GB tree (submodule included) took hours on 2026-10-19; between two
pods it took 90 s. With two pods up, rsync the tree once, then from that pod to the next (`ssh-keygen` a key on the
first pod, append its public key to the second's `authorized_keys`, `rsync -a --delete --exclude=/build
--exclude=/build-frontend /opt/us2-build/ root@second:/opt/us2-build/`; `/opt/us2-bringup/push-to-3090.sh` on the
B200 pod is that chain, ending in the second pod's `bringup.sh`). Start `bringup.sh` only after the tree is there:
started early it fails every build step in a second and writes `BRINGUP_DONE` all the same.
