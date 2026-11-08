#!/usr/bin/env python3
"""tools/check_docs.py - the project makes checkable claims; this checks them.

This project's pitch is that its security claims can be verified, so a false claim is a defect of the
same kind as a bug. One mechanism produces nearly all of them: a claim is a *copy* of a fact that
lives in the code, and two copies with no link diverge. A document written against the tree at one
commit and never re-checked after a later commit in the same range moved the code is the usual shape.

The rule this file enforces, stated once:

    A claim belongs in prose if and only if changing the code could not make it false.
    Everything else restates the code and must be deleted, derived, or bound to it mechanically.

Nine checks, each paired with the rule it enforces:

  1. `path#needle` anchors resolve: `needle` occurs literally in `path`.          (R2)
  2. `path:line` citations resolve, AND are forbidden in files this fork edited.  (R2)
  3. Backticked repository paths exist -- as files a stranger who clones gets.    (R3)
  4. Every `*.md` named anywhere resolves, absolutely or beside the citing file.  (R3)
  5. No document this project does not publish is named at all.                   (R6)
  6. No `*.md` appears inside a string literal of a file we are answerable for.   (R3)
  7. Numbers about the seccomp filter come from the built filter, not a model.     (R4)
  8. The kill-list table matches the code in BOTH directions.                      (R4)
  9. Numbered cross-references resolve and their quoted gloss matches.             (R5)

Why check 2 bans line numbers rather than merely resolving them: resolution is not the claim. Five
citations in this repository pointed at lines that had moved under them; all five were in files this
fork edited, four were correct at the import commit, and the old resolve-only check reported every one
as fine, because it asked whether the file was long enough. A line number is blind exactly where code
churns. An anchor carries the claim, so the check tests the claim -- and it is also the reader's own
`grep`, which matters because this project ships source with no `.git`.

Usage:  python3 tools/check_docs.py [--list]
Exit:   0 clean, 1 with one line per problem. `--list` prints what was checked, and what was not.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The unmodified UnbiasedStrata import. `git diff IMPORT HEAD` is this fork's complete provenance
# (PROVENANCE.md), which is also why inherited engine source is out of scope below.
IMPORT = "39cab36"   # diffed against the WORKING TREE, not HEAD: see answerable()

# Files we are answerable for when git is unavailable (a source tarball has no `.git`). Deliberately
# the front end, the build glue and our own tooling: never inherited engine source.
FALLBACK_ANSWERABLE = (
    "CMakeLists.txt", "Makefile", "flake.nix",
    "frontend/engine.hpp", "frontend/literals.hpp", "frontend/main.cpp",
    "frontend/picture_cache.hpp", "frontend/strata_context.cpp", "frontend/strata_context.hpp",
    "frontend/strata_sandbox.cpp", "frontend/strata_sandbox.h",
    "frontend/tests/engine_test.cpp", "frontend/tests/fake-engine.py",
    "frontend/tests/literals_test.cpp", "frontend/tests/picture_cache_test.cpp",
    "frontend/tests/sandbox_test.cpp", "frontend/tests/e2e.sh",
    "tools/check_docs.py",
)

# Documents this project does not publish. Naming one sends a reader somewhere they cannot go, so it
# is an error wherever it appears -- a document, a comment, a build file or a string. Either publish
# the document or restate the point in place. Bare names are listed because `*.md` matching misses
# them (`PRINCIPLES invariant 1` names no file extension).
UNPUBLISHED = ("PRINCIPLES", "ROADMAP", "STATE.md", "DESIGN.md", "TEST-PLAN.md",
               "RISKS.md", "INTEGRATION.md")

# This file, which has to spell the names above in order to forbid them. A checker naming the thing it
# bans is not the defect the ban exists to catch, and exempting it here is narrower than weakening the
# rule for everyone.
SELF = "tools/check_docs.py"

SUBMODULE = "third_party/llama.cpp"
TEXT_SUFFIXES = (".md", ".c", ".h", ".hpp", ".cpp", ".cu", ".cuh", ".py", ".sh", ".txt",
                 ".nix", ".cmake", ".jinja", ".json", ".in", ".bat", ".ps1", ".metal")
SOURCE_SUFFIXES = (".c", ".h", ".hpp", ".cpp", ".cu", ".cuh", ".py", ".sh")

# `path#needle` - an anchor that carries the claim and survives the line moving.
ANCHOR = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./+-]*\.[A-Za-z0-9_+]+)#([^`]{4,120})`")
# `path:123` or `path:123-456`, the drift-prone form.
CITATION = re.compile(r"`?([A-Za-z0-9_][A-Za-z0-9_./+-]*\.[A-Za-z0-9_+]+):(\d+)(?:-(\d+))?`?")
# A backticked token shaped like a repository path rather than a word or a flag.
BACKTICKED_PATH = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./+-]*/[A-Za-z0-9_./+-]*\.[A-Za-z0-9_+]+)`")
# Any markdown filename, wherever it is written.
MD_NAME = re.compile(r"\b([A-Za-z0-9_][A-Za-z0-9_./-]*\.md)\b")
# `seccomp (75 instructions)` wherever it is quoted.
SECCOMP_COUNT = re.compile(r"seccomp \((\d+) (?:instructions?|insns?)\)")
# A line a document presents as program output and tells the reader to look for.
QUOTED_LOG = re.compile(r"`(sandbox: [a-z][^`]{4,100})`")
# `invariant 2 ("Nothing executes that was not compiled from source in this tree")`
INVARIANT_GLOSS = re.compile(r"invariant (\d+)\s*\(\s*[\"“]([^\"”]{10,200})[\"”]\s*\)")
# A C/C++ or Python string literal, for check 6.
STRING_LITERAL = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"")

STOPWORDS = frozenset(
    "the a an and or of to in on is are was were it its this that with for from by not no "
    "nothing be been has have had can could must may might as at than then so if".split()
)


def run_git(*args: str) -> str | None:
    """Git's stdout, or None when there is no usable git here."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), *args],
                             capture_output=True, text=True, check=True)
        return out.stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def tracked_files() -> list[str]:
    """Every file a stranger who clones or unpacks this project gets.

    The walk fallback is not a nicety: this project distributes source only, so the tree a stranger
    builds is an extracted archive with no `.git`. A check that needs a repository would fail
    `make test` for exactly the people the source-only rule exists to serve.
    """
    out = run_git("ls-files")
    if out is not None:
        return out.splitlines()
    # third_party is the pinned submodule: upstream's content, not ours to police. Without this the walk
    # reported 4,261 files in a source tarball against 634 from git, so a user's `make test` could fail
    # over a markdown file inside llama.cpp.
    skip = {".git", "third_party", "build", "build-frontend", ".venv", "upstream-ubs",
            "result", "__pycache__"}
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in skip:
            continue
        found.append(rel.as_posix())
    return found


def authored(tracked: set[str]) -> set[str]:
    """Files this fork wrote itself, plus every document and the build glue it rewrote.

    This is the set whose *comments* we are answerable for. `answerable()` is wider and governs what the
    program prints: a string literal naming a deleted document misleads the operator whoever first typed
    it, but a comment inside 9,000 lines of inherited engine source is upstream's note to itself, and
    `src/program/generate.cpp` counts as "edited" only because this fork deleted code from it.
    """
    docs = {p for p in tracked if p.endswith(".md")}
    # The front end and this fork's own tooling, whatever git says about who first created the path.
    # `--diff-filter=A` alone missed frontend/engine.hpp, because upstream had a file by that name --
    # and the comment at the top of it sent readers to a document this fork deleted.
    ours = {p for p in tracked if p.startswith("frontend/")}
    glue = {p for p in ("CMakeLists.txt", "Makefile", "flake.nix", "tools/check_docs.py") if p in tracked}
    out = run_git("diff", "--name-only", "--diff-filter=A", IMPORT)
    if out is None:
        return docs | glue | ours | {p for p in FALLBACK_ANSWERABLE if p in tracked}
    return docs | glue | ours | (set(out.split()) & tracked)


def answerable(tracked: set[str]) -> set[str]:
    """Files this fork wrote or changed, plus every document it ships.

    Derived from git rather than listed, because a hand-maintained list is itself a copy of a fact
    and drifts: the list this replaced named four documents that no longer existed.

    Inherited engine source is excluded on purpose. Its comments cite upstream's private design
    notes, which this distribution does not contain, and rewriting them to fix that would change
    `git diff IMPORT HEAD` -- the provenance claim the repository's shape exists to protect.
    PROVENANCE.md discloses it instead.
    """
    docs = {p for p in tracked if p.endswith(".md")}
    out = run_git("diff", "--name-only", IMPORT)
    if out is None:
        return docs | {p for p in FALLBACK_ANSWERABLE if p in tracked}
    return docs | (set(out.split()) & tracked)


def resolve_md(name: str, citing: str, tracked: set[str]) -> bool:
    """A markdown name resolves if it is tracked, outright or beside the file that names it."""
    if name in tracked:
        return True
    sibling = (Path(citing).parent / name).as_posix()
    return sibling in tracked


def strip_strings(text: str) -> str:
    """The text with string literals blanked, so a comment check cannot see into a literal."""
    return STRING_LITERAL.sub('""', text)


def kill_list(src: str) -> set[str]:
    m = re.search(r"const int kKilled\[\] = \{(.*?)\n\};", src, re.S)
    return set(re.findall(r"__NR_([a-z0-9_]+)", m.group(1))) if m else set()


def documented_kills(table: str) -> set[str]:
    """The syscalls docs/SANDBOX.md's kill-list table names.

    Three columns exactly. The table above it documents the two syscalls filtered by ARGUMENT rather than
    killed outright (`socket` and `socketpair`, allowed for AF_UNIX), and reading both as one list made the
    reverse check report that the document claims protections the code does not provide -- when the code
    provides them by a different mechanism the other table exists to describe.
    """
    found: set[str] = set()
    for raw in table.splitlines():
        line = raw.replace(r"\|", "")   # an escaped pipe is cell content, not a separator
        if line.startswith("| `") and line.count("|") == 4 and re.search(r"\| \d+", line):
            found |= set(re.findall(r"`([a-z0-9_]+)`", line.split("|")[1]))
    return found


def filter_facts() -> tuple[int | None, int | None, str]:
    """The filter's real size, from the binary that builds it.

    `sandbox_test --facts` constructs the actual plan and prints what it contains. The number is not
    re-derived here on purpose: the formula this replaced was `17 + 2*n`, a Python model of
    `build_filter()`. Change the filter's fixed part -- add a socket family, reload a register -- and
    the model still returns the old number while the real filter has a new one, so a document stating
    it would pass while being false. That is this project's own failure mode inside its own guard.
    """
    for candidate in ("build-frontend/bin/sandbox_test", "build/bin/sandbox_test"):
        exe = ROOT / candidate
        if not exe.is_file():
            continue
        try:
            out = subprocess.run([str(exe), "--facts"], capture_output=True, text=True,
                                 timeout=30, check=True).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return None, None, f"{candidate} --facts did not run ({exc.__class__.__name__})"
        insns = re.search(r"filter_instructions=(\d+)", out)
        killed = re.search(r"killed=(\d+)", out)
        if not insns or not killed:
            return None, None, f"{candidate} --facts printed no filter_instructions/killed"
        return int(insns.group(1)), int(killed.group(1)), candidate
    return None, None, "sandbox_test is not built"


def main() -> int:
    listing = "--list" in sys.argv
    tracked = set(tracked_files())
    mine = answerable(tracked)
    ours = authored(tracked)
    problems: list[str] = []
    counts = {"anchors": 0, "citations": 0, "paths": 0, "md": 0, "unpublished": 0,
              "literals": 0, "numbers": 0, "logs": 0, "kills": 0, "invariants": 0}
    skipped_citations = 0

    sandbox_src = (ROOT / "frontend/strata_sandbox.cpp").read_text()
    killed = kill_list(sandbox_src)
    want_insns, want_kills, facts_from = filter_facts()
    if want_insns is None:
        problems.append(f"the filter's real size could not be read ({facts_from}); build the front "
                        f"end first -- a size claim must not be checked against a model of the code")

    # Every text file a stranger gets, for the markdown and unpublished-document checks. The
    # submodule is upstream's and not ours to police.
    texts: dict[str, str] = {}
    for rel in sorted(tracked):
        if rel.startswith(f"{SUBMODULE}/") or not rel.endswith(TEXT_SUFFIXES):
            continue
        try:
            texts[rel] = (ROOT / rel).read_text(errors="replace")
        except OSError:
            continue
    all_source = "\n".join(t for r, t in texts.items() if r.endswith(SOURCE_SUFFIXES))

    # ---- 4 and 5: tree-wide, because a dead reference misleads wherever it is written ----
    for rel, text in texts.items():
        for m in MD_NAME.finditer(text):
            name = m.group(1)
            counts["md"] += 1
            if resolve_md(name, rel, tracked):
                continue
            # A name on the unpublished list is reported once, by check 5, with the actionable message.
            if any(name == u or name.removesuffix(".md") == u for u in UNPUBLISHED):
                continue
            line = text[:m.start()].count("\n") + 1
            if rel in ours:
                problems.append(f"{rel}:{line}: names `{name}`, which is not a file in this repository")
            # A reference inside inherited engine source is upstream's; PROVENANCE.md discloses it.

        if rel not in ours or rel == SELF:
            continue
        for name in UNPUBLISHED:
            # The lookbehind keeps `RISKS.md` from matching inside `docs/SANDBOX-RISKS.md`, which is a
            # real file: a hyphen counts as a word boundary, so \b alone reported the shipped
            # document as the unpublished one.
            for m in re.finditer(rf"(?<![A-Za-z0-9_./-]){re.escape(name)}\b", text):
                counts["unpublished"] += 1
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{rel}:{line}: names `{name}`, which this project does not publish; "
                                f"publish it or restate the point here")

    # ---- the rest apply to what we are answerable for ----
    for rel in sorted(mine):
        text = texts.get(rel)
        if text is None:
            continue
        is_doc = rel.endswith(".md")

        # ---- 1: anchors ----
        for m in ANCHOR.finditer(text):
            path, needle = m.group(1), m.group(2)
            if path not in tracked:
                continue
            counts["anchors"] += 1
            try:
                body = (ROOT / path).read_text(errors="replace")
            except OSError:
                problems.append(f"{rel}: anchors `{path}#{needle}` but that file cannot be read")
                continue
            if needle not in body:
                problems.append(f"{rel}: anchors `{path}#{needle}` but that text does not occur in {path}")

        # ---- 2: line citations ----
        for m in CITATION.finditer(text):
            path, start = m.group(1), int(m.group(2))
            if path not in tracked:
                skipped_citations += 1
                continue
            counts["citations"] += 1
            if path in mine:
                problems.append(f"{rel}: cites `{path}:{m.group(2)}` by line number, and {path} is a "
                                f"file this fork edits; cite `{path}#<text that occurs there>` instead")
                continue
            try:
                n_lines = len((ROOT / path).read_text(errors="replace").splitlines())
            except OSError:
                problems.append(f"{rel}: cites {path}:{start} but that file cannot be read")
                continue
            end = int(m.group(3)) if m.group(3) else start
            if start < 1 or end > n_lines:
                problems.append(f"{rel}: cites {path}:{m.group(2)}"
                                f"{'-' + m.group(3) if m.group(3) else ''} but {path} has {n_lines} lines")

        # ---- 3: backticked paths ----
        for m in (BACKTICKED_PATH.finditer(text) if rel in ours else ()):
            path = m.group(1)
            if path in tracked:
                counts["paths"] += 1
                continue
            # A path inside the pinned submodule, written relative to it as the documents do. `git ls-files`
            # of this repository cannot list a submodule's contents, so existence on disk is the only
            # available test -- and it is the right one: the submodule is part of what a cloner gets.
            if (ROOT / SUBMODULE / path).exists():
                counts["paths"] += 1
                continue
            if path.startswith(("http", "/", "~", "..")) or "third_party/" in path or "*" in path:
                continue
            if any(path.startswith(p) for p in ("build/", "build-frontend/", "result/",
                                                "upstream-ubs/", "nix/store", "models/")):
                continue
            # An owner/repo slug (`robertkarl/llama.cpp`) is not a path: two components, and the
            # first is not a directory here.
            head = path.split("/")[0]
            if path.count("/") == 1 and not (ROOT / head).exists():
                continue
            problems.append(f"{rel}: mentions `{path}`, which is not a file in this repository")

        # ---- 6: a markdown name inside a string literal reaches the user, not a source reader ----
        if not is_doc and rel != SELF:
            for m in STRING_LITERAL.finditer(text):
                for md in MD_NAME.finditer(m.group(1)):
                    counts["literals"] += 1
                    if not resolve_md(md.group(1), rel, tracked):
                        line = text[:m.start()].count("\n") + 1
                        problems.append(f"{rel}:{line}: prints `{md.group(1)}` to the user, and that "
                                        f"file is not in this repository")

        if not is_doc:
            continue

        # ---- 7: the filter's size ----
        if want_insns is not None:
            for m in SECCOMP_COUNT.finditer(text):
                counts["numbers"] += 1
                if int(m.group(1)) != want_insns:
                    problems.append(f"{rel}: says the filter is {m.group(1)} instructions; the built "
                                    f"filter has {want_insns} ({want_kills} kill-list entries)")

        # ---- quoted log lines ----
        for m in QUOTED_LOG.finditer(text):
            line = m.group(1)
            counts["logs"] += 1
            needle = line.split("sandbox: ", 1)[1]
            head = re.split(r"[<%]", needle)[0].rstrip(" (;:,")
            if len(head) >= 8 and head not in all_source:
                problems.append(f"{rel}: quotes the log line `{line}` but no source file contains "
                                f"\"{head}\"")

        # ---- 9: numbered cross-references carry their own words ----
        invariants = {}
        try:
            tm = (ROOT / "THREAT_MODEL.md").read_text()
            invariants = {int(n): h for n, h in re.findall(r"^#{2,4} (\d+)\.\s+(.+)$", tm, re.M)}
        except OSError:
            pass
        for m in INVARIANT_GLOSS.finditer(text):
            number, gloss = int(m.group(1)), m.group(2)
            counts["invariants"] += 1
            heading = invariants.get(number)
            if heading is None:
                problems.append(f"{rel}: cites invariant {number}, which THREAT_MODEL.md does not define")
                continue
            words = {w for w in re.findall(r"[a-z]{3,}", gloss.lower()) if w not in STOPWORDS}
            have = {w for w in re.findall(r"[a-z]{3,}", heading.lower()) if w not in STOPWORDS}
            if len(words & have) < 2:
                problems.append(f"{rel}: glosses invariant {number} as \"{gloss}\" but "
                                f"THREAT_MODEL.md calls it \"{heading}\"")

    # ---- 8: the kill list against the table that documents it, both directions ----
    counts["kills"] = len(killed)
    try:
        table = (ROOT / "docs/SANDBOX.md").read_text()
    except OSError:
        problems.append("docs/SANDBOX.md cannot be read, so the kill-list table cannot be checked")
    else:
        documented = documented_kills(table)
        for name in sorted(killed - documented):
            problems.append(f"docs/SANDBOX.md: the filter kills {name} but its table does not list it")
        for name in sorted(documented - killed):
            problems.append(f"docs/SANDBOX.md: its table lists {name} but the filter does not kill it, "
                            f"so the document claims a protection the code does not provide")
        if killed and not documented:
            problems.append("docs/SANDBOX.md: the kill-list table could not be parsed at all")
        if want_kills is not None and len(killed) != want_kills:
            problems.append(f"frontend/strata_sandbox.cpp: kKilled[] has {len(killed)} entries but the "
                            f"built filter reports {want_kills}")

    if listing:
        print(f"check_docs: {len(ours)} files authored here, {len(mine)} edited here, "
              f"of {len(tracked)} tracked "
              f"({'git' if run_git('rev-parse', 'HEAD') else 'no git: fallback list'})")
        print(f"check_docs: {counts['anchors']} anchors, {counts['citations']} line citations, "
              f"{counts['paths']} backticked paths, {counts['md']} markdown names, "
              f"{counts['literals']} names in string literals, {counts['numbers']} filter-size claims, "
              f"{counts['logs']} quoted log lines, {counts['kills']} kill-list entries, "
              f"{counts['invariants']} invariant glosses")
        print(f"check_docs: filter facts from {facts_from}: "
              f"{want_insns} instructions, {want_kills} kill-list entries")
        if skipped_citations:
            print(f"check_docs: {skipped_citations} citations skipped (not a repository path)")

    for p in problems:
        print(f"check_docs: {p}", file=sys.stderr)
    if problems:
        print(f"check_docs: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    checked = sum(counts.values())
    print(f"check_docs: {checked} claims checked, 0 problems "
          f"(this is not every claim in the repository)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
