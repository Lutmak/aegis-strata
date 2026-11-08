# Verifying the Nix flake — the commands, in order

**None of this has been run.** Nix is not installed on the reference machine, so
`flake.nix` has never been evaluated. This file is the list of commands that
would verify it and the output that would prove each one, so the work can be
handed to someone with a Nix installation. A step whose output does not match
is a failure, not a warning — the README's rule 6.

Run them in order. Do not skip ahead: step N's pass criterion is step N+1's
precondition. Record the actual output of every step, including the failures;
a step that was never seen to fail proves nothing.

Steps 0–3 are host setup and need `sudo`. Steps 4 onward are the flake itself
and need no privilege at all.

---

## Step 0 — `/nix` is on `/home` before anything is installed

```bash
df -h / /home
sudo mkdir -p /home/nix /nix
# write /etc/systemd/system/nix.mount exactly as in NIX.md, then:
sudo systemctl daemon-reload
sudo systemctl enable --now nix.mount
findmnt /nix
```

**Proves it:** `findmnt /nix` prints a row whose SOURCE column names
`/home/nix` (the device backing `/home`, with `[/nix]` as its subpath) and whose TARGET is `/nix`.

**Fails if:** `findmnt /nix` prints nothing. Stop here. The CUDA closure is tens of
gigabytes; unless `/` has that much free, installing Nix now fills the root filesystem.
Check with `df -h /` before deciding to skip this step.

---

## Step 1 — Nix is installed and flakes are enabled

```bash
sudo pacman -S nix
sudo systemctl enable --now nix-daemon.socket
sudo usermod -aG nix-users "$USER"       # then log out and in, or: newgrp nix-users
printf 'experimental-features = nix-command flakes\n' | sudo tee -a /etc/nix/nix.conf
sudo systemctl restart nix-daemon
nix --version
nix flake --help >/dev/null && echo "flakes ok"
```

**Proves it:** `nix --version` prints `nix (Nix) 2.x`, and `flakes ok` appears
with no message about experimental features.

**Fails if:** any mention of `experimental Nix feature 'nix-command' is
disabled`, or `nix` not found after the group change.

---

## Step 2 — the store really landed on `/home`

```bash
nix build --no-link nixpkgs#hello
df -h /home | tail -1
du -sh /home/nix 2>/dev/null
```

**Proves it:** `/home/nix` exists and is non-empty, and `/` has not lost space.

**Fails if:** `du -sh /nix` grows while `/home/nix` stays empty — the bind
mount is not active and step 0 must be redone.

---

## Step 3 — the flake is in place and the submodule is populated

```bash
cd path/to/aegis-strata
cp <staging>/flake.nix .          # from the staging directory; see the report
git submodule update --init
test -f third_party/llama.cpp/ggml/CMakeLists.txt && echo "submodule ok"
```

**Proves it:** `submodule ok`.

**Fails if:** the file is absent. Nothing downstream can work;
`CMakeLists.txt#if(NOT EXISTS ${STRATA_GGML_DIR}/ggml/CMakeLists.txt)` and the flake's own
`preBuild` check both depend on it.

---

## Step 4 — the flake evaluates at all

```bash
nix flake metadata '.?submodules=1'
```

**Proves it:** a tree listing the single input `nixpkgs` with
`github:NixOS/nixpkgs/f9a75f41be3a74eab55d3a7679d34ad6cc0da977`, and no error.

**Fails if:** any Nix evaluation error. This is the first time the file has
ever been parsed, so expect this step to find syntax or attribute mistakes.
The most likely candidates, in rough order of probability:

1. `pkgs.mkShell.override { stdenv = ...; }` — the shape of `mkShell` changes
   occasionally; if it rejects `stdenv`, drop the override and instead add
   `pkgs.gcc15` to `packages` and keep the `HOSTCXX` export, which is what
   actually makes the compiler deterministic.
2. `cudaPackages_13_4.cudatoolkit` — if the attribute is missing or warns, see
   step 7's fallback.
3. `pkgs.autoAddDriverRunpath` — renamed from `addOpenGLRunpath` historically;
   if absent at this revision, drop it and rely on `LD_LIBRARY_PATH` at run
   time (NIX.md covers this).

---

## Step 5 — the lock file is generated and pins what the flake says

```bash
nix flake lock
nix flake metadata --json '.?submodules=1' | jq '.locks.nodes.nixpkgs.locked'
```

**Proves it:** the JSON object shows
`"rev": "f9a75f41be3a74eab55d3a7679d34ad6cc0da977"`, `"owner": "NixOS"`,
`"repo": "nixpkgs"`, `"type": "github"`, and — the point of the exercise — a
`narHash` beginning `sha256-`.

**Fails if:** `rev` differs, or `narHash` is absent. A differing `rev` means
someone ran `nix flake update`; revert `flake.lock`.

Then commit it: `git add flake.lock`. Until this is committed, hard rule 5 is
only half satisfied (`docs/NIX-LOCK.md` explains why).

---

## Step 6 — `nix flake check` passes

```bash
nix flake check '.?submodules=1'
```

**Proves it:** exits 0 with no output other than progress lines. This
evaluates every output, so it catches mistakes in the devShell that a `nix
build` alone would not.

**Fails if:** any output fails to evaluate. Note this will *build* the default
package on some Nix versions; if you want evaluation only, use
`nix flake check --no-build`.

---

## Step 7 — the devShell gives the pinned toolchain

```bash
nix develop '.?submodules=1' --command bash -c '
  nvcc --version | tail -2
  g++ --version | head -1
  cmake --version | head -1
  python3 --version
  python3 -c "import numpy, regex, yaml, tqdm; print(numpy.__version__)"
  echo "HOSTCXX=$HOSTCXX"
'
```

**Proves it**, line by line:

| expected | why it matters |
|---|---|
| `Cuda compilation tools, release 13.4, V13.4.x` | the minor measured working here. A different *minor* invalidates the pin — PolyStrata records 13.2.0/13.2.1 miscompiling for sm_120. |
| `g++ (GCC) 15.3.0` | the compiler nvcc 13.4.92 accepted in Phase 0. If this says 16.x the pin failed and the build will too. |
| `cmake version 4.4.4` | above the 3.24 floor. |
| `Python 3.x` then `2.5.3` | the offline tooling imports resolve. |
| `HOSTCXX=/nix/store/...-gcc-wrapper-15.3.0/bin/g++` | the Makefile's probe will be skipped, not merely expected to pick the right answer. |

**Fails if:** `g++` reports 16.x, or `nvcc` reports a minor other than 13.4, or
any import raises `ModuleNotFoundError`.

**The one real check inside the shell** — that nvcc actually accepts this gcc,
which is the whole reason the pin exists:

```bash
nix develop '.?submodules=1' --command bash -c '
  t=$(mktemp -d); printf "int main(){return 0;}\n" > $t/p.cu
  nvcc -ccbin "$HOSTCXX" -c $t/p.cu -o $t/p.o && echo "nvcc accepts HOSTCXX"
'
```

**Proves it:** `nvcc accepts HOSTCXX`. This is the same test the Makefile's
probe runs (`Makefile:40-42`), run once against the pinned pair. If it fails,
nothing else in this document matters: raise the gcc pin to `gcc14` (nvcc 13.4
accepts majors 6–16 per nixpkgs' own table, and the Makefile probes g++-14
next) and re-run.

---

## Step 8 — the native build works inside the shell

This is the cheapest real confirmation and it reuses the Phase 0 gate, so it
compares against a measurement that already exists.

```bash
nix develop '.?submodules=1' --command bash -c 'make build CUDA_ARCHS=86 JOBS=16'
ls -l build/bin/
nix develop '.?submodules=1' --command bash -c 'make test'; echo "exit $?"
```

**Proves it:** `build/bin/` contains everything the Makefile's `TARGETS` names
— as of this writing `strata`, `libstrata_kq.so` and `aegis`, since
`strata-poly` was removed from `TARGETS` while this was being written; `make
build`'s own closing `ls -1` is the authority. `make test` exits 0. Build time in the
same ballpark as the native build on the reference machine (4m 2s, 337 objects). Fewer
than ~100 objects compiled means a stale incremental build is being reported as
a pass; clear `build/` and rerun.

**Fails if:** any CUDA compile error. Capture the first 20 verbatim before
changing anything: the first error is the one that explains the rest.

> Note for whoever runs this: `build/` and `build-frontend/` are owned by the
> main session's native build. Use a separate checkout or
> `BUILD_DIR=build-nix`, or coordinate first.

---

## Step 9 — `packages.default` builds

```bash
nix build '.?submodules=1' --print-out-paths --print-build-logs
ls -l result/bin/
```

**Proves it:** one `/nix/store/...-aegis-strata-0.1.45` path printed, and
`result/bin/` holding the same three artifacts as step 8 (`strata`, `libstrata_kq.so`, `aegis`).

**Fails if:**

- `third_party/llama.cpp is empty` — you dropped `?submodules=1`.
- `Could NOT find CUDAToolkit` — `find_package(CUDAToolkit REQUIRED)` did not
  resolve the symlinked root. Fallback: keep `cudatoolkit` but also add the
  split packages to `buildInputs`
  (`cudaPackages.cuda_nvcc`, `cuda_cudart`, `libcublas`, `cccl`) and append
  `-DCUDAToolkit_INCLUDE_DIR=${cudaToolkit}/include` to `env.cmakeExtra`.
- `__cxa_call_terminate` at link time — two libstdc++ generations met, meaning
  the host compiler and the CUDA host compiler diverged. Check that all three
  `-DCMAKE_*_COMPILER` flags in `env.cmakeExtra` point at the same gcc 15.
- `engine binary 'strata' was not produced` — the install assertion fired; the
  build silently produced nothing and correctly refused to install an empty
  directory.

Then check the RUNPATH before trusting the artifact, because this is the one
failure that shows up only at run time:

```bash
patchelf --print-rpath result/bin/strata
objdump -p result/bin/strata | grep -i runpath
```

**Fails if** the output contains a path ending in `/lib/stubs`. The CUDA
redistributables ship a stub `libcuda.so` with no implementation in it, purely
so the link step resolves; nixpkgs normally strips that directory from RUNPATH
with `removeStubsFromRunpathHook`, which the split packages propagate but the
`cudatoolkit` symlinkJoin may not. A binary that finds the stub at run time
links successfully and then fails to talk to the GPU. The fix is to add
`cudaPackages.removeStubsFromRunpathHook` to `nativeBuildInputs`.

---

## Step 10 — the build really had no network

```bash
grep -E 'sandbox|filter-syscalls' /etc/nix/nix.conf /etc/nix/nix.conf.d/* 2>/dev/null
nix show-config | grep -E '^sandbox'
```

**Proves it:** `sandbox = true`. On Linux that is the default and it gives the
build a private network namespace with only loopback. Combined with step 9
succeeding, the README's rule 2 ("No network during the build") is enforced by
the builder rather than asserted by us — the native build had to be wrapped in
`bwrap --unshare-net` by hand to show the same thing.

**Fails if:** `sandbox = false` or `sandbox = relaxed`. Then the claim is void;
set `sandbox = true` in `/etc/nix/nix.conf`, restart `nix-daemon`, and redo
step 9.

---

## Step 11 — determinism on one machine

```bash
nix build '.?submodules=1' --rebuild --print-build-logs
echo "exit $?"
```

**Proves it:** exit 0 and no `differs` lines. `--rebuild` builds the derivation
again from scratch and compares the result against the store path already
there.

**Fails if:** Nix reports `output ... differs from ...`. Keep the report: the
differing files are the non-determinism, and that is the single most valuable
output of this whole exercise. Look first for an embedded path, an embedded
timestamp, or `-march=native` having leaked back in.

A useful detail already checked by reading the tree: there is **no**
`__DATE__`, `__TIME__` or `__TIMESTAMP__` anywhere in `src/`, `include/`,
`frontend/`, or in llama.cpp's `common/`, `src/` and `tools/server/`. The
usual first cause of non-determinism is already absent.

---

## Step 12 — the published hash

```bash
nix hash path "$(readlink -f result)"
sha256sum result/bin/*
nix path-info --json '.?submodules=1' | jq '.[].narHash'
```

**Proves it:** a `sha256-...` NAR hash for the whole output and one line per
binary. These lines are the Phase 7 artifact — the thing that gets published
so a third party can check it.

Record alongside them: the commit, the nixpkgs `rev` from step 5, and
`CUDA_ARCHS`. A hash without those three is not checkable.

---

## Step 13 — a second machine reproduces it

This is the actual criterion for a reproducible build, and **nothing before
this step satisfies it.** Steps 11 and 12 show the build is deterministic on
one machine; only this one shows it is deterministic as a recipe.

On a second x86-64 Linux machine with Nix installed:

```bash
git clone https://github.com/Lutmak/aegis-strata && cd aegis-strata
git checkout <the same commit>
git submodule update --init
nix flake metadata --json | jq -r '.locks.nodes.nixpkgs.locked.rev'
nix build '.?submodules=1'
nix hash path "$(readlink -f result)"
sha256sum result/bin/*
```

**Proves it:** the `rev` matches step 5, and the hashes match step 12 exactly.

**Fails if:** anything differs. Likely culprits, in order:

1. A different `flake.lock` — the second machine resolved a different nixpkgs.
2. `portable = false` on one side — `-march=native` baked in the local CPU.
3. A different commit — remember the front end embeds
   `LLAMA_BUILD_COMMIT`/`GGML_COMMIT`, so **two builds of different commits can
   never match**. The claim being tested is only ever "two builds of the same
   commit match".
4. A different CPU count or `NIX_BUILD_CORES` — should not matter and would be
   a genuine bug worth reporting upstream if it does.

---

## Not checked

- Everything above. No step in this file has been executed. Nix is not
  installed where this was written, so none of it has been executed.
- `flake.nix` has never been parsed by Nix. Syntax, attribute names and the
  `mkShell.override` shape are all unconfirmed; step 4 exists to find out.
- Whether `find_package(CUDAToolkit REQUIRED)` resolves against
  `cudaPackages_13_4.cudatoolkit`'s symlinked root. This is the single most
  likely place the derivation fails first. Step 9 carries the fallback.
- Whether nvcc from CUDA 13.4.3 accepts gcc 15.3.0. It is the same *minor* as
  the measured-working 13.4.92 and nixpkgs' own compatibility table allows gcc
  majors 6–16 for CUDA 13.4, but the exact pair has not been run. Step 7's
  `-ccbin` test is the check.
- Whether the Nix-built binaries find `libcuda.so.1` on Arch. They almost
  certainly need help; NIX.md has the two candidate fixes and neither has been
  tried.
- Whether `make test` passes under the pinned toolchain. Step 8 tests it in the
  devShell; the derivation sets `doCheck = false`, so the packaged build does
  not run it at all.
- The Makefile changed on disk while this flake was being written (another
  agent owns that branch): `TARGETS` went from
  `strata strata-poly strata_kq aegis` to
  `strata strata_kq aegis` mid-task. It was re-read afterwards and
  `HOSTCXX ?=`, `PORTABLE`, and `$(CMAKE_EXTRA)` being appended **last** on the
  `configure:` cmake line all still hold — that last one is what makes the
  flake's compiler flags authoritative, so re-check it after any further
  Makefile change.
- `src/poly/` and `src/qwen35/` have since been deleted, changing which binaries
  `build/bin` holds. The derivation copies that directory wholesale and asserts
  only on `strata` and `aegis`, so it should survive that — untested.
