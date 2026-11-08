# Security policy

## Reporting a vulnerability

There is no security email address for this project, and no PGP key. Use GitHub's private
vulnerability reporting instead:

> <https://github.com/Lutmak/aegis-strata> → **Security** → **Report a vulnerability**

That creates a private advisory visible only to you and the maintainers. Please use it rather than a
public issue for anything you think is exploitable. If you are unsure whether something counts, use
it anyway — a false alarm costs nothing.

Useful in a report: the commit you built, the exact invocation, what you observed, and the smallest
input that reproduces it. A description of the mechanism is worth more than a working exploit;
please do not attach payloads.

**What to expect.** This is a one-person project. There is no response-time guarantee, no SLA and no
bug bounty. Reports will be read and answered, and anything confirmed will be written down in public
once it is fixed, or written down in `THREAT_MODEL.md` as a known gap if it is not going to be fixed
soon.

**Supported versions.** Only the current `main`. There are no releases and nothing is backported.

## Scope

In scope:

- The front end (`frontend/`): the HTTP surface, request parsing, the engine adapter and the
  child-process boundary.
- The build glue in this repository: `CMakeLists.txt`, `cmake/`, `Makefile`.
- The engine source as this tree ships it, including how it parses GGUF and pack files.
- The pinned `third_party/llama.cpp` submodule *as configured here*.
- The offline tools in `tools/`, including whether their downloads are pinned and verified.
- The documentation, where following it would leave a reader less safe than the docs imply.

Things we particularly want to hear about:

- Memory corruption reachable from an HTTP request.
- A malformed model, pack or MTP file causing memory corruption rather than a clean exit.
- Anything that makes the build or the server open a network connection.
- Anything that causes execution of code that was not compiled from this tree.
- A path traversal, or a write outside the build directory and the temporary files the server owns.
- A prompt or a completion reaching a log or a file on disk.
- A build step that reads an unverified download, or a verification that fails open.

Out of scope:

- Anything that requires already running code as the user who runs the server. The server trusts its
  operator completely; it reads the model files that operator points it at and serves whoever can
  reach its port.
- Resource exhaustion from a large or slow request. The server handles one request at a time by
  design; a long request blocks the next one. That is documented, not a flaw.
- Binding the server to a non-loopback address without `--api-key`. It defaults to `127.0.0.1`, and
  overriding that is a configuration you chose. We would like it to refuse that combination — see
  invariant 3 in `THREAT_MODEL.md` — but today it does not, and that gap is already recorded.
- Windows code paths. Upstream's Windows branches are still in the tree but are never compiled or
  run here.
- The model's own behaviour: jailbreaks, bad output, and prompt injection of the agent that calls
  us. The engine's job is to run the weights it is given.
- Upstream projects' installers, release binaries and update feeds. Report those to Strata,
  PolyStrata or UnbiasedStrata. `THREAT_MODEL.md` records why this fork does not use them.

## The kernels are unaudited

Stated plainly, because the rest of this document means less without it:

**The CUDA kernels in this tree have not been audited, by us or by anyone we know of.** They are the
bulk of the engine's code. Building them from source proves their provenance — they are the code in
this repository, which is upstream's code, and `git diff` shows every change — but provenance is not
correctness. A memory-safety bug or a numerical error in a kernel would not be caught by anything
this project currently does.

Nor can it be, cheaply: checking a kernel's numerical correctness needs GPU runs against real
weights, compared against a reference implementation. Upstream has done some of that work; this fork
has not reproduced it.

There has been **no external security audit** of this project, and none is planned.

## Other known gaps

These are not secrets; they are written up with their evidence in `THREAT_MODEL.md`:

- "The server makes no outbound connection" is now enforced for the **engine** child — a network
  namespace, a seccomp filter and a Landlock allowlist, installed before `exec` and covered by a
  GPU-free test. It is still only inspection for the **front-end** process, which serves HTTP and
  imports `connect`; a filter for it is designed and not built.
- A container is a soft boundary for the GPU, because the NVIDIA kernel driver's ioctl interface is
  not namespaced.
- Builds are not reproducible yet, so a published hash cannot yet be independently recreated.
- The llama.cpp submodule contains a web-UI asset fetcher that checks a downloaded archive against a
  checksum fetched from the same server. It is forced off here and the sandboxed build proves it is
  unreachable, but it should stay off.

## Distribution

Source only. There are no release binaries and there never will be — that is hard rule 1 in this
project, not a current state of affairs. Verification hashes for builds of a given commit are
intended to be published once builds are reproducible; until then there is nothing to check a binary
against, which is another reason not to trust one.

If you ever see a binary release claiming to be aegis-strata, it did not come from this project.
