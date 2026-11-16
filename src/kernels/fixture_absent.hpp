// src/kernels/fixture_absent.hpp - "is this fixture absent?", the only question a test may skip itself on.
//
// A skip condition must be a positive assertion about what is missing, never the negation of a success test.
// `fopen(p, "rb") != nullptr` answers "can I open this?", which is also false for EACCES, EMFILE, ENFILE, ELOOP
// and a broken symlink. Three test programs here used it as "is it here?", so a fixture that was present but
// unreadable was reported as absent: a green skip, with a message naming a file sitting right there. On this
// project's own workflow that is reachable -- a 79 GB pack or shard copied into place with sudo keeps root:600,
// and a parent directory missing its x bit gives the same errno.
//
//   $ head -c 4096 /dev/urandom > pack/full/experts.bin && chmod 000 pack/full/experts.bin
//   $ expert_parity --selftest
//   expert_parity: SKIP - no expert pack at pack/full/experts.bin.      exit 77   <- before
//   $ chmod 644 pack/full/experts.bin     # the same file, the same bytes
//                                                                      exit 2    <- the honest answer
//
// `stat` plus ENOENT is the positive form: it says the thing is not there, rather than that we failed to open it.
#pragma once

#include <cerrno>
#include <sys/stat.h>

namespace strata::testing {

/// True only when `path` demonstrably does not exist. ENOTDIR counts: a non-directory component means no such
/// file can exist. Every other failure -- no permission here or on a parent, a descriptor limit, a symlink loop --
/// is NOT absence: we could not find out, so the caller must treat it as a hard failure, which is this project's
/// rule that a verification which cannot run is a failure and never a warning.
inline bool fixture_absent(const char* path) {
    struct stat st {};
    if (path == nullptr) return true;
    if (stat(path, &st) == 0) return false;
    return errno == ENOENT || errno == ENOTDIR;
}

}   // namespace strata::testing
