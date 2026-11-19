# aegis-strata build.  Needs CMake 3.24+, a C++20 compiler, a C compiler and a CUDA 12.x toolkit with its driver.
# Nothing is downloaded: ggml, llama, common and mtmd come from the third_party/llama.cpp submodule.
#
#   git clone <repo> && cd <repo>
#   git submodule update --init
#   make build                      # build/bin/{strata,libstrata_kq.so,aegis}
#   make test                       # the server against a fake engine; no GPU needed
#
BUILD_DIR  ?= build
# CUDA archs: GTX 10 (61), RTX 20 / GTX 16 (75), RTX 30 (86), RTX 40 (89).  Any arch below 75 turns on upstream's
# STRATA_EXPERIMENTAL_SM60, which needs a CUDA 12.x toolkit (13 dropped sm_61).  `make build CUDA_ARCHS=86` builds for
# one card and is several times faster.  An arch the installed nvcc cannot compile is dropped with a note.
CUDA_ARCHS ?= 61;75;86;89
# nvcc: on PATH, else the toolkit's usual place (RunPod images and distro packages differ).  NVCC=/path overrides.
NVCC ?= $(or $(shell command -v nvcc 2>/dev/null),$(wildcard /usr/local/cuda/bin/nvcc))
ifeq ($(NVCC),)
  ifneq ($(filter build configure,$(MAKECMDGOALS)),)
    $(error nvcc not found on PATH or in /usr/local/cuda/bin; install a CUDA 12.x toolkit or set NVCC=/path/to/nvcc)
  endif
endif
# Everything below that runs nvcc is skipped when there is none (a laptop running `make server` or `make test`).
ifneq ($(NVCC),)
NVCC_ARCHS := $(shell $(NVCC) --list-gpu-arch 2>/dev/null | sed 's/compute_//' | tr '\n' ' ')
ifneq ($(NVCC_ARCHS),)
  CUDA_ARCHS_DROPPED := $(strip $(foreach a,$(subst ;, ,$(CUDA_ARCHS)),$(if $(filter $(firstword $(subst -, ,$(a))),$(NVCC_ARCHS)),,$(a))))
  ifneq ($(CUDA_ARCHS_DROPPED),)
    $(info make: nvcc cannot compile CUDA arch(s) $(CUDA_ARCHS_DROPPED); building without them)
    CUDA_ARCHS := $(subst $(eval) ,;,$(strip $(foreach a,$(subst ;, ,$(CUDA_ARCHS)),$(if $(filter $(firstword $(subst -, ,$(a))),$(NVCC_ARCHS)),$(a),))))
  endif
endif
SM60 := $(shell echo "$(CUDA_ARCHS)" | tr ';' '\n' | sed 's/-.*//' | grep -E '^[0-9]+$$' | awk '$$1 < 75 { print "ON"; exit }')
SM60_FLAG := $(if $(SM60),-DSTRATA_EXPERIMENTAL_SM60=ON,)
# PORTABLE=1: an AVX2 baseline for the CPU expert kernels instead of -march=native (build on one box, run on another).
PORTABLE_FLAG := $(if $(PORTABLE),-DSTRATA_PORTABLE=ON,)
# One compiler for the C, C++ and CUDA host sides.  nvcc accepts GCC only up to a version (CUDA 12.4: 13); when the
# C++ files are compiled by a newer g++ and nvcc falls back to an older one, two libstdc++ generations meet at the link
# and it fails on __cxa_call_terminate (upstream #1645).  The probe tries the versioned compilers newest first and hands
# CMake absolute paths, so `g++` resolving to a version nvcc rejects cannot slip through.  HOSTCXX=/usr/bin/g++-13
# overrides it.
HOSTCXX ?= $(shell for c in g++-15 g++-14 g++-13 g++-12 g++-11 g++; do p=$$(command -v $$c 2>/dev/null) || continue; \
             t=$$(mktemp /tmp/strata-hc-XXXXXX.cu) && printf 'int main(){return 0;}\n' > $$t && \
             if $(NVCC) -ccbin $$p -c $$t -o /dev/null >/dev/null 2>&1; then rm -f $$t; echo $$p; break; fi; rm -f $$t; done)
HOSTCC   := $(subst g++,gcc,$(HOSTCXX))
endif
COMPILER_FLAGS := $(if $(HOSTCXX),-DCMAKE_C_COMPILER=$(HOSTCC) -DCMAKE_CXX_COMPILER=$(HOSTCXX) -DCMAKE_CUDA_HOST_COMPILER=$(HOSTCXX),)
# capped: inside a container nproc reports the host's CPUs, not the container's quota
JOBS       ?= $(shell n=$$(nproc 2>/dev/null || echo 4); [ $$n -gt 16 ] && n=16; echo $$n)
# what `make build` produces; upstream's `all` also builds about 70 parity and bench programs
TARGETS    := strata strata_kq aegis

.PHONY: build configure clean test

configure:
	@test -f third_party/llama.cpp/ggml/CMakeLists.txt || \
	  { echo "third_party/llama.cpp is empty: run 'git submodule update --init' first"; exit 1; }
	cmake -S . -B $(BUILD_DIR) -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON -DSTRATA_NATIVE_EXPERTS=ON \
	      -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_BUILD_TESTS=OFF \
	      "-DCMAKE_CUDA_ARCHITECTURES=$(CUDA_ARCHS)" $(SM60_FLAG) $(PORTABLE_FLAG) $(COMPILER_FLAGS) -DCMAKE_CUDA_COMPILER=$(NVCC) \
	      -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))/bin \
	      -DCMAKE_LIBRARY_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))/bin $(CMAKE_EXTRA)

build: configure
	cmake --build $(BUILD_DIR) -j $(JOBS) --target $(TARGETS)
	@echo "built:"; ls -1 $(BUILD_DIR)/bin/strata $(BUILD_DIR)/bin/libstrata_kq.so $(BUILD_DIR)/bin/aegis

# The `aegis` server alone, without CUDA: for a laptop or a CI box. Needs only a C++20 compiler and CMake.
.PHONY: server
server:
	cmake -S . -B $(BUILD_DIR)-server -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=OFF -DSTRATA_BUILD_TESTS=OFF \
	      -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))-server/bin $(CMAKE_EXTRA)
	cmake --build $(BUILD_DIR)-server -j $(JOBS) --target aegis

# The server's tests, against a fake engine (server/tests/fake-engine.py): the adapter's cases (cancel, drain,
# backpressure, child exit, ERR) and the HTTP surface a coding agent touches (server/tests/e2e.sh).  Needs python3
# and curl; no GPU and no model.  Runs in the CUDA-free build directory, so it works on the build box and on a laptop.
test: server
	cmake --build $(BUILD_DIR)-server -j $(JOBS) --target engine_test literals_test picture_cache_test nv_defaults_test tail_skip_test mmq_ids_margin_test sandbox_test
	$(BUILD_DIR)-server/bin/engine_test server/tests/fake-engine.py
	$(BUILD_DIR)-server/bin/literals_test
	$(BUILD_DIR)-server/bin/picture_cache_test
# The sm_86 switches the engine turns on by itself (docs/speed.md): that they apply to sm_86 and nothing else,
# that an operator's own setting always wins, and that the two ADAPT variables stay paired.
	$(BUILD_DIR)-server/bin/nv_defaults_test
# The route tail skip: that nothing is skipped unless --tail-skip asked for it, that the two configurations the
# dispatch cannot honour say so instead of skipping, and that every outcome prints one line.  This is the setting
# that computes less of the model rather than reordering the same arithmetic, so its default is pinned by a test.
	$(BUILD_DIR)-server/bin/tail_skip_test
# The MMQ row table's over-read margin: llama.cpp's stream-k fixup loads a whole J-column tile of `ids` from each
# expert's first row, so an exact-sized table is read past its end (and faults when it ends a page).
	$(BUILD_DIR)-server/bin/mmq_ids_margin_test
	server/tests/e2e.sh $(BUILD_DIR)-server/bin/aegis
# The documentation makes checkable claims, so they get checked: file:line citations that resolve,
# backticked paths that exist, the filter's instruction count and kill list against the code, and quoted
# log lines that a reader could actually grep for.  No GPU, no model, under a second.
	python3 tools/check_docs.py
# The benchmark harness's own logic: which arms exist, what each one sets, and the guards that refuse a
# comparison that could not mean anything.  A measuring instrument that quietly measures the wrong
# configuration reports a number rather than an error, so the parts that decide what runs get tests.  No GPU.
	@out=$$(python3 -m unittest discover -s tools -p 'test_bench_*.py' 2>&1); status=$$?; \
	 echo "$$out" | tail -3; \
	 [ $$status -eq 0 ] || exit $$status; \
	 echo "$$out" | grep -qE '^Ran [1-9][0-9]* tests?' || \
	   { echo 'harness tests: unittest discover registered NOTHING and still exited 0; the pattern'; \
	     echo "             test_bench_*.py matched no test, so these assertions are not in the gate."; \
	     exit 1; }
# Last on purpose.  sandbox_test exits 77 when the kernel cannot enforce the sandbox, and make stops at the first
# failure -- run earlier, a skip would silently take the rest of the suite with it, which is how a kernel without
# Landlock used to turn into "no tests ran" rather than "one test skipped".  77 is still a failure and not a pass:
# a runner that cannot enforce the sandbox must not report green on an untested security control.
	$(BUILD_DIR)-server/bin/sandbox_test

# The engine's own test suite: ~135 ctest cases over the CUDA kernels, the GGUF reader, the expert cache and
# the split/share rules.  It is NOT part of `make build`, which ships three binaries and nothing else -- so
# without this target ctest registers tests whose executables were never built and reports "Unable to find
# executable" for every one.  Needs the CUDA toolkit and a GPU; some cases need model fixtures upstream never
# published and fail honestly rather than skipping.
.PHONY: test-engine
test-engine:
	cmake -S . -B $(BUILD_DIR)-tests -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON \
	      -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_BUILD_TESTS=ON \
	      "-DCMAKE_CUDA_ARCHITECTURES=$(CUDA_ARCHS)" $(SM60_FLAG) $(PORTABLE_FLAG) $(COMPILER_FLAGS) \
	      -DCMAKE_CUDA_COMPILER=$(NVCC) \
	      -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))-tests/bin \
	      -DCMAKE_LIBRARY_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))-tests/bin $(CMAKE_EXTRA)
	cmake --build $(BUILD_DIR)-tests -j $(JOBS)
# --no-tests=error: ctest exits 0 when it finds NO tests, so a configure change that dropped a whole block of
# registrations would leave this target green and silent. Proven: an empty CTestTestfile.cmake gives
# "No tests were found!!!" and exit 0. The flag turns that into the failure it is.
	cd $(BUILD_DIR)-tests && ctest --output-on-failure --no-tests=error

# The checks that need a GPU, kept out of `make test` because that target runs on a laptop with no CUDA.
#
# This exists because nothing in `make test` can see the IQ4 W4A8 kernel or any of the row-table call sites:
# `make test` builds in $(BUILD_DIR)-server with CUDA off. `make test-engine` does build them and is now a usable
# gate (it exits 0, with the cases that need unshipped fixtures reporting themselves as skips), but it does not
# cover these two: the IQ4 parity test is registered there, yet nothing runs it under compute-sanitizer, and the
# tile widths need driving one at a time. So the two checks that do bite live here:
#
#   - prefill_iq4_w4a8_test: the kernel against MMQ, bit for bit. Run at all four tile widths, because
#     STRATA_W4A8_CFG selects between them in production and three of them had no coverage.
#   - the same test under compute-sanitizer: the ONLY check in this tree that looks at the allocations rather than
#     the arithmetic. It is what found an exact-sized row table in the test itself -- 281 invalid reads, with the
#     plain run passing on whatever slack the allocator left. ~4 minutes at 97 tokens.
#
# Exit 77 from the test means "skipped": no CUDA device, or a card mmq::w4a8_arch_ok() has not been measured on.
# That is reported and not treated as a pass.
.PHONY: test-gpu
test-gpu:
	cmake -S . -B $(BUILD_DIR)-gpu -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON \
	      -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_Q6K_EXPERTS=ON -DSTRATA_BUILD_TESTS=ON \
	      "-DCMAKE_CUDA_ARCHITECTURES=$(CUDA_ARCHS)" $(SM60_FLAG) $(PORTABLE_FLAG) $(COMPILER_FLAGS) \
	      -DCMAKE_CUDA_COMPILER=$(NVCC) \
	      -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))-gpu/bin \
	      -DCMAKE_LIBRARY_OUTPUT_DIRECTORY=$(abspath $(BUILD_DIR))-gpu/bin $(CMAKE_EXTRA)
	cmake --build $(BUILD_DIR)-gpu -j $(JOBS) --target prefill_iq4_w4a8_test
	@set -e; B=$(BUILD_DIR)-gpu/bin/prefill_iq4_w4a8_test; \
	  $$B --tokens 97 >/dev/null 2>&1; rc=$$?; \
	  if [ $$rc = 77 ]; then $$B --tokens 97; echo "test-gpu: SKIPPED (no card this kernel has been measured on)"; exit 77; fi; \
	  for cfg in 0 1 2 3; do echo "--- tile width $$cfg"; $$B --cfg $$cfg --tokens 97; $$B --cfg $$cfg --tokens 4096; done; \
	  if command -v compute-sanitizer >/dev/null 2>&1; then \
	    echo "--- compute-sanitizer memcheck (the allocations, not the arithmetic)"; \
	    compute-sanitizer --tool memcheck --error-exitcode 1 $$B --tokens 97 | tail -3; \
	  else \
	    echo "test-gpu: compute-sanitizer not on PATH -- the allocation check did NOT run"; exit 1; \
	  fi; \
	  echo "test-gpu: all checks passed"

clean:
	# -frontend as well: that was this directory's name before the rename, so anyone who ran the suite
	# before it kept a few hundred MB that `make clean` silently walked past.
	trash $(BUILD_DIR) $(BUILD_DIR)-server $(BUILD_DIR)-tests $(BUILD_DIR)-gpu $(BUILD_DIR)-frontend 2>/dev/null || true
