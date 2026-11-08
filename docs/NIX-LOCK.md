# No `flake.lock` was produced — and it was not faked

There is no `flake.lock` in this directory. One could not be generated
correctly, and writing a plausible-looking one would be worse than writing
none: a wrong `narHash` makes `nix build` fail with an integrity error that
looks like tampering, and a *guessed* `narHash` that happens to be right is
still an unverified hash presented as a verified one. The README's rule 6
— "a verification that cannot run is a failure, never a warning" — means this
file, not a fabricated lock.

## Why it cannot be produced here

A `flake.lock` entry carries a `narHash`: the SHA-256 of the *unpacked* source
tree in Nix archive form. Computing it requires fetching the nixpkgs tarball
and hashing it with Nix's own NAR serialisation. On the reference machine:

- Nix is not installed (`command -v nix` finds nothing), so `nix flake lock`
  cannot run.
- Installing Nix needs root, and on a machine whose `/` cannot hold the closure it
  also needs `/nix` bind-mounted somewhere with room first (`NIX.md`, step 1).
- Hashing the nixpkgs archive by hand would not help: `nix-hash --type sha256
  --sri` needs Nix.

## Why a `narHash` copied off the internet is not acceptable either

Several third-party indexes publish nixpkgs `narHash` values. Taking one would
reproduce exactly the flaw this project faults its parents for: a checksum
obtained from the same trust domain as the artifact it is supposed to check.
The hash has to be computed locally, once, by the person who will rely on it.

## The command that generates it

Run this once, from the repository root, after Nix is installed (NIX.md has
the prerequisites, including putting `/nix` on `/home`):

```bash
cd path/to/aegis-strata
nix flake lock          # writes flake.lock next to flake.nix
git add flake.lock      # it is part of the pin; it must be committed
```

`nix flake lock` fetches each input, records its resolved identity and hash,
and writes nothing else. It does not build anything and needs no GPU.

To confirm afterwards that the lock says what this flake intends:

```bash
nix flake metadata --json | jq '.locks.nodes.nixpkgs.locked'
```

## What it will pin

Exactly one input. The flake has no others — no `flake-utils`, no
`flake-parts`, no `nixpkgs-unstable` fallback — so the lock will contain one
node plus the root:

| field | expected value |
|---|---|
| `type` | `github` |
| `owner` | `NixOS` |
| `repo` | `nixpkgs` |
| `rev` | `f9a75f41be3a74eab55d3a7679d34ad6cc0da977` |
| `narHash` | **unknown until the command above runs** — this is the whole point |
| `lastModified` | `1793876309` (2026-11-05T10:58:29Z, the commit date per the GitHub API) |

`rev` and the three identity fields are already fixed by `inputs.nixpkgs.url`
in `flake.nix`, so the lock cannot silently change them; it can only *add* the
`narHash`. That is the missing half of hard rule 5. Until it is committed, the
pin is "this git sha1" (immutable, but a sha1, and verified by GitHub rather
than by us) instead of "this exact tree, checked against a SHA-256 we computed".

## Checking that the lock did not drift

`nix flake update` would re-resolve inputs and move the pin. It must never be
run casually on this repo. To verify the committed lock still matches the
declared revision:

```bash
nix flake metadata --json \
  | jq -e '.locks.nodes.nixpkgs.locked.rev == "f9a75f41be3a74eab55d3a7679d34ad6cc0da977"' \
  && echo "pin intact"
```

Exit status 0 and `pin intact` is the pass. Anything else means the lock and
the flake disagree and the build must not be trusted.
