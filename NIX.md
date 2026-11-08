# Building aegis-strata with Nix

> ## UNVERIFIED
>
> **This flake has never been evaluated.** Nix is not installed on the reference machine
> (`command -v nix` finds nothing), and installing it needs root. No
> `nix flake check`, no `nix develop`, no `nix build` has been run against
> `flake.nix`. Nothing in this document is a measured result. Every command
> below is what *should* work; the first person to run them should expect to
> fix something, and should record what.
>
> `docs/NIX-VERIFICATION.md` lists the commands that turn this banner into a fact, in
> order, with the output that proves each step. Until that has been done and
> its output pasted somewhere, do not claim this project has reproducible
> builds.

## What this is for

A reproducible build. No project in this family pins its toolchain, so no project
in this family can say what a binary was built from. This flake makes the
compiler, the CUDA toolkit and CMake inputs of the build instead of properties
of whoever's laptop ran `make`.

It does two things:

- `devShells.default` — the exact toolchain on your PATH, so `make build` and
  `make test` work the same way for everyone.
- `packages.default` — a derivation that builds the engine and the front end.
  This is the part PolyStrata's flake does not have, and it is the one that
  matters: a devShell pins what you type, a derivation pins what you get.

## Prerequisites

### 1. Put `/nix` on `/home` — do this *before* installing Nix

The Nix store lives at `/nix` and will hold the CUDA 13.4 toolkit, gcc 15 and
the whole build closure: expect tens of gigabytes. Check what you have with
`df -h / /home`: if `/` cannot hold tens of gigabytes but `/home` can, bind-mount
`/nix` onto `/home` first. Installing Nix without arranging this will fill the
root filesystem.

Moving the store afterwards is painful (store paths are baked into every
built artifact), so bind-mount it first:

```bash
sudo mkdir -p /home/nix /nix

sudo tee /etc/systemd/system/nix.mount >/dev/null <<'EOF'
[Unit]
Description=Nix store on /home
Before=nix-daemon.service
RequiredBy=nix-daemon.service

[Mount]
What=/home/nix
Where=/nix
Type=none
Options=bind

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nix.mount

findmnt /nix            # must print /home/nix before you go any further
```

`findmnt /nix` showing `/home/nix` is the gate. If it prints nothing, stop.

A symlink (`ln -s /home/nix /nix`) also works and is simpler, but some Nix
tooling resolves `/nix` and gets confused about store path identity. The bind
mount is the safe form.

### 2. Install Nix

Arch has it in the official repositories, so use pacman rather than the
upstream curl-to-shell installer:

```bash
sudo pacman -S nix
sudo systemctl enable --now nix-daemon.socket
sudo usermod -aG nix-users "$USER"      # log out and back in, or: newgrp nix-users
```

Flakes are still gated behind an experimental flag:

```bash
printf 'experimental-features = nix-command flakes\n' | sudo tee -a /etc/nix/nix.conf
sudo systemctl restart nix-daemon
```

Check it:

```bash
nix --version                      # expect 2.x
nix flake --help >/dev/null        # must not complain about experimental features
```

**On `sudo`:** the four commands above are a one-time *installation*, performed
by the user. The README's rule 5 ("No privilege escalation": the build never calls
`sudo` and never installs a system package) is about the
build, and the flake honours it — the derivation runs unprivileged in a sandbox
and writes only to its build directory and its store path. Nothing in
`flake.nix` calls `sudo` or touches anything outside the tree and the store.

### 3. Generate the lock file

There is no `flake.lock` in the repository yet, because it cannot be produced
without Nix. `docs/NIX-LOCK.md` explains why in full and gives the command.
Short version, run once from the repository root:

```bash
nix flake lock
git add flake.lock
```

Without it the nixpkgs input is pinned by git revision but not checked against
a hash we computed ourselves — half of the README's rule 6.

### 4. The submodule

`third_party/llama.cpp` is a git submodule and **flakes ignore submodules
unless you ask for them**. Every command below therefore uses
`'.?submodules=1'` rather than `.`. The derivation fails early with a clear
message if the submodule is missing, rather than letting CMake fail further in.

## Entering the shell

```bash
cd path/to/aegis-strata
nix develop '.?submodules=1'
```

The shell prints the versions it provides and then behaves like a normal
terminal where `make` works:

```bash
make build CUDA_ARCHS=86     # sm_86 = RTX 3080 Ti
make test                    # front-end tests against frontend/tests/fake-engine.py; no GPU
```

What the shell pins:

| tool | version | why that one |
|---|---|---|
| nvcc | CUDA 13.4.3 (`cudaPackages_13_4`) | 13.4 is the minor measured working here (nvcc 13.4.92, Phase 0). CUDA *minor* versions matter in this family — PolyStrata's flake records "13.2.0/13.2.1 miscompile for sm_120". |
| g++ / gcc | 15.3.0 (`gcc15`) | The system default is gcc 16.2.1 and nvcc 13.4.92 rejects it. 15.3.0 is exactly what the Makefile's probe selected in the Phase 0 build. |
| cmake | 4.4.4 | Floor is 3.24 (`CMakeLists.txt#cmake_minimum_required(VERSION 3.24)`). The box has 4.4.5; the flake's is 4.4.4. |
| python3 + numpy, regex, pyyaml, tqdm | from the pinned nixpkgs | Offline tooling only (`tools/iq_pack.py`, `tools/mtp_pack.py`). Never in the serving path. |
| uv, curl, git | from the pinned nixpkgs | `uv` for the project's usual Python workflow, `curl` for `frontend/tests/e2e.sh`, `git` for the submodule. |

The shell exports `HOSTCXX` so the Makefile's compiler probe (`Makefile:40-42`)
never runs. That probe is the right answer by accident in this shell —
nixpkgs names the binary `g++`, not `g++-15`, so the loop falls through to
plain `g++`, which here *is* gcc 15.3.0 — and `HOSTCXX` makes it the right
answer on purpose.

## Building the artifact

```bash
nix build '.?submodules=1' --print-out-paths
ls -l result/bin/
```

You get `result/bin/` containing whatever `make build` produces — at the time
of writing `strata`, `libstrata_kq.so` and `aegis` (the front end,
to be renamed `aegis`), since `strata-poly` has just been dropped from the
Makefile's `TARGETS`. The install step copies `build/bin` wholesale rather than
naming targets, exactly so that deletions like that one do not break it; it
asserts only that `strata` and `aegis` exist.

Everything lands in one directory on purpose: `libstrata_kq.so` is `dlopen`'d
by bare name from beside the engine binary
(`include/strata/prefill/mmq_kq_abi.hpp:11`), so splitting `bin/` and `lib/`
would break the K-quant MMQ path.

Two options are exposed, and nothing else:

```bash
# a different card
nix build '.?submodules=1' --impure --expr '(builtins.getFlake (toString ./.)).packages.x86_64-linux.aegis-strata-for { cudaArchs = "89"; }'

# host-tuned instead of portable — see the warning below
nix build '.?submodules=1' --impure --expr '(builtins.getFlake (toString ./.)).packages.x86_64-linux.aegis-strata-for { portable = false; }'
```

### Why the default build is `PORTABLE=1`

With `STRATA_PORTABLE=OFF` (the Makefile default) ggml-cpu compiles with
`-march=native`, which bakes the build host's CPU into the binary. That makes
Phase 7's own verification criterion — two builds on *different machines*
producing identical hashes — impossible by construction. So the flake sets
`PORTABLE=1`.

This costs less than it sounds. `PORTABLE=1` only drops ggml-cpu's baseline to
AVX2 + FMA + F16C + BMI2; Strata's own AVX-512 expert kernels are compiled
per-file with explicit `-mavx512*` flags and selected at run time either way
(`CMakeLists.txt`, the CUDA toolkit and architecture blocks). If you want the
host-tuned build, use the Makefile directly or pass `portable = false` — and
drop the cross-machine hash claim along with it.

### Running the result on Arch

The binary needs `libcuda.so.1`, which is part of the NVIDIA kernel driver's
userspace half and is **not** in the Nix store and never can be. The flake
adds `/run/opengl-driver/lib` to the binaries' RUNPATH (`autoAddDriverRunpath`),
which is where NixOS keeps it. On Arch that directory does not exist.

This has not been tested. The likely fixes, in order of preference:

```bash
# 1. point the loader at Arch's driver
LD_LIBRARY_PATH=/usr/lib result/bin/aegis --help

# 2. or give the store-built binary the path it expects
sudo mkdir -p /run/opengl-driver/lib
sudo ln -sf /usr/lib/libcuda.so.1 /run/opengl-driver/lib/
```

Note that `make test` needs none of this: the front-end tests build without
CUDA and need no GPU, which is exactly what makes CI possible.

## How a third party reproduces the hash

This is the Phase 7 deliverable. The claim is **"two builds of the same commit
match"** — never "the same tree matches", and never across different commits,
for the reason in the next section.

```bash
# 1. get the exact source
git clone https://github.com/Lutmak/aegis-strata
cd aegis-strata
git checkout <commit>
git submodule update --init

# 2. check the pin has not moved
nix flake metadata --json | jq '.locks.nodes.nixpkgs.locked.rev'
#    must print "f9a75f41be3a74eab55d3a7679d34ad6cc0da977"

# 3. ask what the output *will* be, without building
nix path-info --derivation '.?submodules=1#default'

# 4. build and hash
nix build '.?submodules=1'
nix hash path ./result
sha256sum result/bin/*
```

Step 4's `sha256sum` output is what gets published. A third party running the
same four steps on their own machine must get the same lines. If they do not,
the build is not reproducible and the difference must be explained, not
averaged away.

To check determinism on one machine without a second machine:

```bash
nix build '.?submodules=1' --rebuild
```

That rebuilds from scratch and compares against the existing store path. It
reports any file that differs. Non-zero exit is a failure of invariant 6.

## What is honestly not reproducible yet

### The commit is compiled into the front end

`third_party/llama.cpp/cmake/build-info.cmake` and
`third_party/llama.cpp/ggml/CMakeLists.txt` both run
`git rev-parse --short HEAD` and compile the result in, as
`LLAMA_BUILD_COMMIT` (via `common/build-info.cpp.in`) and `GGML_COMMIT` (via
`ggml/src/ggml-version.h.in`). Two builds of **different commits can never
match**. The honest claim is only ever *two builds of the same commit match*.

The flake pins these to the flake's own revision (`self.shortRev`) so the
value is a function of the source rather than of whether a `.git` directory
happened to be present in the sandbox. For a dirty tree it records `dirty`,
which is the correct thing to say.

**On `STRATA_BUILD_ID`.** Upstream embedded the build commit under that name, and
it was still there at the import commit — but entirely inside the DeepSeek
engine's block, applied only to that engine's own main for its `--version`
string. Deleting the DeepSeek engine took it along: this tree has **zero**
occurrences of `STRATA_BUILD_ID` and exactly **one** `execute_process`, the
`import numpy` probe. The engine binaries `strata` and `libstrata_kq.so` embed
no commit at all; only the front end does, through llama.cpp. That makes the
reproducibility story simpler, not harder.

### The CUDA toolkit is a prebuilt binary

`cudaPackages_13_4` is NVIDIA's redistributable nvcc, cudart and cuBLAS,
unpacked and patchelf'd. Nothing in it was compiled from source in this tree.

This is worth stating plainly against **the README's rule 1** ("No prebuilt
binaries, ever. Not downloaded, not vendored, not as a fallback. Everything
that executes is compiled from source in the tree") and with **invariant 2**
("Nothing executes that was not compiled from source in this tree").

The tension is unavoidable and we do not pretend otherwise. NVIDIA does not
publish nvcc's source. There is no source-built route to a CUDA binary, and a
CUDA engine without nvcc is not a project. What the flake buys is that the
toolkit stops being "whatever was in `/opt/cuda`" and becomes a named,
version-pinned, hash-checked input that a third party can obtain identically.

Note also that this is *not a new* exposure: the native Arch build already
compiles with the same vendor binary. The flake only makes it visible.

**Resolved, and the README says so:** rule 1 is about what this project ships and
runs, not about the compiler that builds it — a distinction every build of every
project relies on. The toolchain carve-out is stated in rule 1 itself and
repeated in `THREAT_MODEL.md` under "Not covered" beside the unaudited CUDA
kernels. Leaving the rule unscoped would mean the build file quietly
contradicted it, which is worse than naming the exception.

### The Nix build is not byte-identical to the Arch build

It will not become so, and that is fine. nixpkgs applies hardening flags
(fortify, stack protector, PIE) that Arch's gcc does not, and the pinned
toolkit is CUDA 13.4.3 against the box's 13.4.92 from a 13.4.0-era package —
same minor, different patch. The Nix artifact is reproducible *against itself*,
on any machine. Reproducibility is a property of a build recipe, not a property
shared between two different recipes.

### Still not covered by this flake

- **No CI job.** Phase 7 step 2 is "build in CI from the pinned toolchain and
  publish the binary hashes". This flake is step 1. A `checks` output running
  `make test` (front end only, no GPU, no model) is the obvious next addition
  and is the cheapest possible CI; `doCheck` is off here because it would
  configure a second CUDA-free build directory and roughly double the build.
- **No `packages.frontend`.** A CUDA-free front-end-only derivation would let
  CI run on a box with no GPU and no CUDA toolkit at all. Not added yet.
- **No macOS.** The Metal tree is kept (README rule 7), but CUDA does not exist on
  Darwin, so a Mac output is a second derivation with `STRATA_ENABLE_CUDA=OFF`
  and the Metal path. That is a different build nobody can test here, so it was
  left out rather than guessed at.
- **Two Python pinning mechanisms.** The devShell takes numpy, regex, pyyaml
  and tqdm from the pinned nixpkgs (built from source, pinned with the flake),
  while the repository carries `tools/requirements.txt`
  (`--require-hashes`, prebuilt wheels). Versions already differ — nixpkgs
  numpy is 2.5.3 at the pinned revision against 2.5.4 in the lock. Four
  packages do not justify two mechanisms; pick one and document the other as
  the fallback.
- **The flag list could drift.** The derivation runs `make build` rather than
  re-listing the cmake flags, precisely so it cannot drift from the Makefile.
  But it does hardcode `CUDA_ARCHS` and `PORTABLE`, and it hardcodes nothing
  about which targets `make build` produces — `install` copies `build/bin`
  wholesale, so the queued `strata-poly` deletion will not break it.
- **Nothing here has been run.** See the banner.
