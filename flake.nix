#
# aegis-strata — pinned build toolchain.  The first step toward a reproducible build.
#
# ===========================================================================
#  UNVERIFIED.  This flake has never been evaluated or built.  Nix is not
#  installed on the dev box (`command -v nix` finds nothing) and installing it
#  needs root, so no `nix flake check`, no `nix develop`, no `nix build` has
#  run against this file.  Treat every claim below as a stated intention until
#  the commands in docs/NIX-VERIFICATION.md have been run and their output recorded.
#  README rule 6: a verification that cannot run is a failure, never
#  a warning.  This banner is that failure, written down instead of hidden.
# ===========================================================================
#
# What this gives that the Makefile cannot:
#
#   1. The compiler is pinned, not probed.  On the dev box the system default
#      is gcc 16.2.1 and nvcc 13.4.92 rejects it; the Makefile copes by
#      probing g++-15, g++-14, ... against `nvcc -ccbin` and taking the newest
#      one that works.  That is a property of the host, not of the tree: on a
#      box without gcc15 installed the same `make build` silently selects a
#      different compiler, or none.  Here gcc is an input.
#   2. No network inside the build.  Nix runs the build in a sandbox with no
#      interface up, so README rule 2 ("No network during the build") is enforced by the builder
#      rather than asserted by us.  The native build was *measured* clean
#      under `bwrap --unshare-net` by hand; under Nix it cannot be
#      otherwise, for anyone, without us remembering to wrap it.
#   3. No writes outside the tree.  The derivation writes only to its build
#      directory and $out.  Note the Makefile's compiler probe writes a
#      scratch .cu file into /tmp; we pass HOSTCXX so that probe never runs.
#      README rule 5 ("No privilege escalation") holds here with nothing left to trust.
#   4. An artifact, not just an environment.  PolyStrata's flake is a devShell
#      only.  A devShell pins what you type; a derivation pins what you get.
#      Only the second one has a hash a third party can reproduce.
#
# What is NOT reproducible yet — read this before claiming anything:
#
#   * The commit is embedded in the binary.  llama.cpp's
#     cmake/build-info.cmake and ggml/CMakeLists.txt both run
#     `git rev-parse --short HEAD` and compile the result in (as
#     LLAMA_BUILD_COMMIT / GGML_COMMIT, via common/build-info.cpp.in and
#     ggml/src/ggml-version.h.in).  Two builds of *different* commits can
#     therefore never match.  The only honest claim is "two builds of the same
#     commit match".  Below we pin these to the flake's own revision so the
#     value is a function of the source and not of whether a .git directory
#     happened to be present.
#
#     STRATA_BUILD_ID is worth a note: upstream embedded the commit under that
#     name, and at the import commit it was still there, but it lived entirely
#     inside the `strata-dsv41` block and
#     was applied only to src/dsv41/ds4_main.cpp.  Commit 8cd4adf deleted the
#     DeepSeek engine and took it with it: the current tree has zero
#     occurrences of STRATA_BUILD_ID and exactly one execute_process (the
#     `import numpy` probe, CMakeLists.txt#import numpy).  So the engine binaries
#     `strata` / `strata-poly` / `libstrata_kq.so` now embed no commit at all;
#     only the front end does, through llama.cpp.
#
#   * The CUDA toolkit is a redistributable binary.  cudaPackages_13_4 is
#     NVIDIA's prebuilt nvcc, cudart and cuBLAS, unpacked and patchelf'd.
#     Nothing in it was compiled from source in this tree.  That is in direct
#     tension with README rule 1 ("No prebuilt binaries, ever") and
#     invariant 2 ("Nothing executes that was not compiled from source in this
#     tree").  The compiler is the one place that tension cannot be removed:
#     NVIDIA does not publish nvcc's source, so there is no source-built route
#     to a CUDA binary, and a GPU engine without nvcc is not a project.  We do
#     not pretend otherwise.  What the flake buys is that the toolkit becomes
#     a *named, hash-pinned* input instead of whatever the host happened to
#     have in /opt/cuda.  MAIN SESSION: decide how this is documented —
#     THREAT_MODEL.md is the right home, and hard rule 1 probably needs an
#     explicit, narrow carve-out for the compiler rather than being quietly
#     contradicted by a build file.
#
#   * `nix build` alone does not reproduce the Arch build.  nixpkgs applies
#     its own hardening flags (fortify, stack protector, PIE) that Arch's
#     gcc does not, and the pinned nvcc is CUDA 13.4.3 where the box has
#     13.4.92 from a 13.4.0-era package.  Same minor, different patch.  The
#     Nix artifact is reproducible *against itself*, on any machine.  It is
#     not byte-identical to `make build` on Arch and will not become so.
#
#   * -march=native had to go.  With STRATA_PORTABLE=OFF (the Makefile
#     default) ggml-cpu compiles with -march=native, which bakes the build
#     host's CPU into the binary, which makes the reproducible-build goal
#     ("two builds on different machines, identical hashes") impossible by
#     construction.  packages.default therefore sets PORTABLE=1.  This is
#     cheaper than it sounds: the CPU-baseline blocks in CMakeLists.txt show
#     PORTABLE only drops ggml-cpu's baseline to AVX2+FMA+F16C+BMI2, while
#     Strata's own AVX-512 expert kernels stay in the binary as per-file
#     -mavx512* objects chosen at run time either way.  Pass
#     `portable = false` to get the host-tuned build back, and lose the
#     cross-machine hash claim with it.
#
# Scope: Linux x86-64 only.  There is
# no aarch64-darwin output.  It would not cost nothing: CUDA does not exist on
# Darwin, so a Mac output is a second derivation with STRATA_ENABLE_CUDA=OFF
# and the Metal path, which is a different build we cannot test and nobody has
# asked for yet.  One platform, honestly, beats two with one of them a guess.
#
{
  description = "aegis-strata: pinned CUDA/gcc/cmake toolchain and a reproducible build";

  inputs = {
    # EXACT REVISION, never a branch and never `nixpkgs-unstable`.
    #
    # f9a75f41be3a74eab55d3a7679d34ad6cc0da977 is the nixos-unstable channel
    # head of 2026-11-05 (`curl https://channels.nixos.org/nixos-unstable/git-revision`).
    # Chosen, and verified by reading the tree at that revision, because:
    #
    #   * pkgs/top-level/cuda-packages.nix:204 defines cudaPackages_13_4 with
    #     cuda = "13.4.3".  CUDA *minor* versions matter in this family —
    #     PolyStrata's own flake carries the note "13.2.0/13.2.1 miscompile for
    #     sm_120" — and 13.4 is the minor measured working on the reference machine
    #     (nvcc 13.4.92) measured working on the reference machine.  Do not float this.
    #   * pkgs/development/compilers/gcc/versions.nix:4 gives gcc15 = 15.3.0,
    #     which is exactly the g++-15 the Phase 0 build actually used.
    #   * cmake is 4.4.4 here against 4.4.5 on the box.  Both are far past the
    #     3.24 floor (CMakeLists.txt#cmake_minimum_required(VERSION 3.24)); the
    #     difference is noted, not hidden.
    #
    # Deliberately NOT PolyStrata's rev 6510408d147d8c5e6d76043e899e9251756f6a75:
    # at that revision nixpkgs had no cudaPackages_13_4, so it forces
    # cudaPackages_13_0 — a different CUDA minor from the one that was measured
    # to work here, which is the one thing the PolyStrata note tells us not to
    # do.  nixos-unstable rather than nixpkgs-unstable because the former has
    # passed the NixOS test suite.
    #
    # The rev is immutable, but a rev alone is a git sha1, not an integrity
    # check of the fetched tree.  The SHA-256 (`narHash`) lives in flake.lock,
    # and flake.lock has not been generated — see docs/NIX-LOCK.md.  Until it
    # exists, hard rule 5 is only half satisfied, and that is a gap, not a
    # detail.
    nixpkgs.url = "github:NixOS/nixpkgs/f9a75f41be3a74eab55d3a7679d34ad6cc0da977";
  };

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";

      pkgs = import nixpkgs {
        inherit system;
        config = {
          # NVIDIA's redistributables are unfree.  Set here rather than left to
          # the user's NIXPKGS_ALLOW_UNFREE so the build does not depend on
          # ambient environment.  See the honesty note on hard rule 1 above.
          allowUnfree = true;
          # NOT config.cudaSupport.  That flag rebuilds nixpkgs packages
          # against CUDA; we consume no such package (ggml and llama come from
          # the third_party/llama.cpp submodule), so turning it on would cost
          # hours of compilation and change nothing in the output.
        };
      };

      inherit (pkgs) lib;

      cudaPackages = pkgs.cudaPackages_13_4;

      # The monolithic symlinkJoin of the split redistributables: one directory
      # with bin/nvcc, lib/ and include/ under a single root, which is what
      # `find_package(CUDAToolkit REQUIRED)` (CMakeLists.txt) and the Makefile's
      # `NVCC=` both expect, and what /opt/cuda looks like on Arch.  nixpkgs
      # prefers the split packages (cuda_nvcc, cuda_cudart, libcublas, cccl)
      # and they would give a smaller closure, but they rely on setupCudaHook
      # injecting a `;`-separated CUDAToolkit_ROOT through the cmake configure
      # hook — which this derivation bypasses, because it drives the project's
      # own Makefile.  One root is the simplest thing that can work; if it does
      # not, docs/NIX-VERIFICATION.md says what to try next.
      cudaToolkit = cudaPackages.cudatoolkit;

      # gcc 15.3.0.  nvcc 13.4.92 on the reference machine rejects its gcc 16.2.1.
      # Note that nixpkgs disagrees: its own nvcc/gcc
      # compatibility table
      # (pkgs/development/cuda-modules/_cuda/db/bootstrap/nvcc.nix) says CUDA
      # 13.4 supports gcc up to major 16, and nixpkgs' default gcc at the
      # pinned revision IS 16 (all-packages.nix:3058).  So
      # `cudaPackages_13_4.backendStdenv` would hand nvcc gcc 16 — the exact
      # compiler measured to fail here.  We do not use backendStdenv.  We pin
      # the compiler that was measured to work and say why, because a measured
      # failure outranks a vendor support matrix.
      hostStdenv = pkgs.gcc15Stdenv;
      hostCxx = "${hostStdenv.cc}/bin/g++";
      hostCc = "${hostStdenv.cc}/bin/gcc";

      # Offline Python tooling only (tools/iq_pack.py, tools/mtp_pack.py and
      # the gguf-py they import).  Never in the serving path — README hard
      # rule 3.  Taken from the pinned nixpkgs rather than from
      # tools/requirements.txt, because nixpkgs builds these from source and
      # pins them with the flake, whereas `pip --require-hashes` installs
      # prebuilt wheels: hash-verified, but still binaries nobody compiled
      # here.  The versions differ slightly from the lock (nixpkgs numpy is
      # 2.5.3 at the pinned rev against 2.5.4 in the lock).  MAIN SESSION: two
      # pinning mechanisms for four packages is one too many — pick the lock
      # for non-Nix users and this for Nix users, and say so in the README, or
      # drop one.
      pythonTooling = pkgs.python3.withPackages (
        ps: with ps; [
          numpy
          regex
          pyyaml
          tqdm
        ]
      );

      # The commit compiled into the front end.  Without this, the Nix sandbox
      # has no .git and no git binary, so llama.cpp's build-info falls back to
      # "unknown"/0: deterministic, but a binary that cannot say what it was
      # built from.  `self.shortRev` is the flake's own revision and is absent
      # for a dirty tree, which is the correct thing to record in that case.
      buildId = self.shortRev or "dirty";

      mkAegis =
        {
          # sm_86 = RTX 3080 Ti, the dev box.  The Makefile also auto-drops
          # architectures nvcc rejects; one arch builds several times faster and
          # yields a smaller binary.
          cudaArchs ? "86",
          # See the -march=native note at the top of this file.
          portable ? true,
        }:
        hostStdenv.mkDerivation {
          pname = "aegis-strata";
          version = "0.1.45";

          src = self;

          nativeBuildInputs = [
            pkgs.cmake
            pkgs.gnumake
            # Adds /run/opengl-driver/lib to the binaries' RUNPATH so libcuda.so
            # (the kernel driver's userspace half, which is NOT in the store and
            # never can be) is found at run time.  See NIX.md: on Arch this
            # directory does not exist by default and the artifact needs help.
            pkgs.autoAddDriverRunpath
          ];

          # nixpkgs' note in cuda-modules/packages/cudatoolkit.nix: cudatoolkit
          # belongs in buildInputs, not nativeBuildInputs, for splicing.  The
          # Makefile is given an absolute NVCC= so PATH does not matter.
          #
          # One thing to watch, because it only shows up at run time: the
          # redistributables ship a stub libcuda.so (lib/stubs) with no
          # implementation, so that linking resolves.  nixpkgs strips that
          # directory from RUNPATH with removeStubsFromRunpathHook, which the
          # *split* packages propagate and this symlinkJoin may not.  If
          # `patchelf --print-rpath result/bin/strata` shows a .../lib/stubs
          # entry, add cudaPackages.removeStubsFromRunpathHook to
          # nativeBuildInputs.  docs/NIX-VERIFICATION.md step 9 checks for this.
          buildInputs = [ cudaToolkit ];

          # We configure through the project's Makefile, which is the real build
          # interface (`make build`), so cmake must not run its own configure
          # hook.  Driving the Makefile rather than re-listing its cmake flags
          # here means the flag set cannot drift away from the tree.
          dontUseCmakeConfigure = true;

          # setupCudaHook (propagated by the CUDA packages) otherwise appends
          # -DCMAKE_CUDA_HOST_COMPILER=<backendStdenv's c++> to cmakeFlags and
          # exports CUDAHOSTCXX.  backendStdenv at this revision is gcc 16 (see
          # above), which is the compiler that fails.  Turn that half of the
          # hook off; setupCUDAToolkit_ROOT, the other half, still runs.
          dontSetupCUDAToolkitCompilers = true;
          env.CUDAHOSTCXX = hostCxx;

          # Appended last on the Makefile's cmake line (Makefile `configure:`),
          # so these win over anything earlier.  The three compiler variables
          # are belt as well as braces: HOSTCXX below already skips the probe,
          # and these make the choice explicit in the cache regardless.
          env.cmakeExtra = lib.concatStringsSep " " [
            "-DCMAKE_C_COMPILER=${hostCc}"
            "-DCMAKE_CXX_COMPILER=${hostCxx}"
            "-DCMAKE_CUDA_HOST_COMPILER=${hostCxx}"
            "-DCUDAToolkit_ROOT=${cudaToolkit}"
            # Pin the embedded identity instead of leaving it to whether git
            # happened to be in the sandbox.  See buildId above.
            "-DLLAMA_BUILD_COMMIT=${buildId}"
            "-DLLAMA_BUILD_NUMBER=0"
            "-DGGML_BUILD_COMMIT=${buildId}"
          ];

          preBuild = ''
            # A flake does not fetch git submodules unless asked, and
            # third_party/llama.cpp is where ggml, llama, common, mtmd and
            # seven of tools/server's files come from.  Stop and name the
            # missing dependency (README rule 5, no privilege escalation) rather than letting
            # the submodule guard in CMakeLists.txt fail further in.
            if [ ! -f third_party/llama.cpp/ggml/CMakeLists.txt ]; then
              echo "aegis-strata: third_party/llama.cpp is empty." >&2
              echo "A flake ignores git submodules unless you ask for them." >&2
              echo "Build with:  nix build '.?submodules=1'" >&2
              exit 1
            fi
          '';

          buildPhase = ''
            runHook preBuild

            make build \
              CUDA_ARCHS=${cudaArchs} \
              ${lib.optionalString portable "PORTABLE=1"} \
              NVCC=${cudaToolkit}/bin/nvcc \
              HOSTCXX=${hostCxx} \
              JOBS=$NIX_BUILD_CORES \
              CMAKE_EXTRA="$cmakeExtra"

            runHook postBuild
          '';

          # `make test` is deliberately not run here.  It configures a second,
          # CUDA-free build directory (build-frontend) and needs python3 and
          # curl, so it roughly doubles the build for a check that is better
          # run as its own CI job — and running the front-end tests needs no
          # GPU, which is the whole reason CI is possible at all (README
          # §12).  Not covered by this flake; see NIX.md.
          doCheck = false;

          installPhase = ''
            runHook preInstall

            # Everything lands in one directory on purpose: libstrata_kq.so is
            # dlopen'd by bare name from beside the engine binary
            # (include/strata/prefill/mmq_kq_abi.hpp defines STRATA_KQ_LIB_NAME, and
            # CMakeLists.txt builds strata_kq into the same output directory as the
            # engine), so splitting bin/ and lib/ would break the K-quant MMQ path.  Copying build/bin wholesale rather than
            # naming targets also survived the strata-poly deletion.
            mkdir -p $out/bin
            cp -a build/bin/. $out/bin/

            # A build that produced nothing must fail, not install an empty
            # directory.  Hard rule 5: never fail open.
            test -x $out/bin/strata || {
              echo "aegis-strata: engine binary 'strata' was not produced" >&2
              exit 1
            }
            test -x $out/bin/aegis || {
              echo "aegis-strata: front end 'aegis' was not produced" >&2
              exit 1
            }

            runHook postInstall
          '';

          meta = {
            description = "Local LLM inference engine serving an OpenAI-compatible API on localhost";
            license = lib.licenses.mit;
            platforms = [ "x86_64-linux" ];
            mainProgram = "aegis";
          };
        };
    in
    {
      packages.${system} = {
        default = mkAegis { };

        # The builder itself, exposed so the two knobs in NIX.md are reachable.  `packages.default` is a
        # derivation and has no `.override`, so documenting `default.override { ... }` named an API that does
        # not exist -- and that was the only documented way to opt out of PORTABLE=1, the knob the
        # reproducibility trade-off turns on.  Call this instead:
        #   nix build '.?submodules=1' --impure --expr \
        #     '(builtins.getFlake (toString ./.)).packages.x86_64-linux.aegis-strata-for { cudaArchs = "89"; }'
        aegis-strata-for = mkAegis;
      };

      # gcc comes from the shell's stdenv, not from `packages`: the stdenv is
      # what puts cc/c++ on PATH, so it has to be the pinned one.  mkShell
      # takes stdenv as a callPackage argument, so this is an .override and not
      # an attribute — passing `stdenv = ...` inside the attrset would set an
      # environment variable of that name in the derivation instead.
      devShells.${system}.default = (pkgs.mkShell.override { stdenv = hostStdenv; }) {
        name = "aegis-strata";

        packages = [
          pkgs.cmake
          pkgs.gnumake
          cudaToolkit
          pythonTooling
          # The project manages Python with uv; keep it available so the
          # documented `uv run` / tools/requirements.txt workflow still works
          # inside the shell for anyone who prefers it to pythonTooling above.
          pkgs.uv
          # frontend/tests/e2e.sh drives the HTTP surface with curl, and the
          # submodule needs git.
          pkgs.curl
          pkgs.git
        ];

        shellHook = ''
          # The Makefile's HOSTCXX probe (Makefile:40-42) loops
          # g++-15, g++-14, ... and finally plain g++, testing each against
          # `nvcc -ccbin`.  In this shell there is no `g++-15` — nixpkgs names
          # the binary `g++` — so the probe would fall through to `g++`, which
          # here IS gcc 15.3.0, and would succeed.  That is the right answer by
          # accident.  Setting HOSTCXX makes it the right answer on purpose:
          # the Makefile uses `HOSTCXX ?=`, which an environment variable
          # overrides, so the probe does not run at all and nothing is written
          # to /tmp.
          export HOSTCXX="${hostCxx}"
          export CUDAHOSTCXX="${hostCxx}"
          export CUDAToolkit_ROOT="${cudaToolkit}"

          echo "aegis-strata devShell (UNVERIFIED flake — see NIX.md)"
          echo "  nvcc   : $(nvcc --version 2>/dev/null | tail -1)"
          echo "  g++    : $(${hostCxx} --version | head -1)"
          echo "  cmake  : $(cmake --version | head -1)"
          echo "  python : $(python3 --version)"
          echo
          echo "  build  : make build CUDA_ARCHS=86"
          echo "  tests  : make test          (no GPU needed)"
        '';
      };
    };
}
