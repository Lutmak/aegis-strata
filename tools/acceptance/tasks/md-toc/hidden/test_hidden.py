import os, subprocess, sys, pathlib
HERE = pathlib.Path(__file__).parent
WORK = pathlib.Path(os.environ.get("WORK", HERE.parent))
def run(md, *args):
    f = WORK / "_in.md"; f.write_text(md)
    r = subprocess.run([sys.executable, str(WORK / "mdtoc.py"), str(f), *args], capture_output=True, text=True)
    return r.returncode, r.stdout
def test_example():
    rc, out = run("# Intro\n## Setup\n### Linux\n## Setup\n")
    assert rc == 0
    assert out.rstrip("\n") == "- [Intro](#intro)\n  - [Setup](#setup)\n    - [Linux](#linux)\n  - [Setup](#setup-1)"
def test_fence_and_punctuation():
    md = "## What's new, in 2.0?\n```\n# not a heading\n```\n~~~\n## nor this\n~~~\n## Done\n"
    rc, out = run(md)
    assert rc == 0
    assert out.rstrip("\n") == "- [What's new, in 2.0?](#whats-new-in-20)\n- [Done](#done)"
def test_setext_and_depth():
    md = "Title\n=====\nPart\n----\n### Deep\n#### Deeper\n"
    rc, out = run(md, "--max-depth", "3")
    assert rc == 0
    assert out.rstrip("\n") == "- [Title](#title)\n  - [Part](#part)\n    - [Deep](#deep)"
    rc, out = run(md, "--min-depth", "2")
    assert out.rstrip("\n") == "- [Part](#part)\n  - [Deep](#deep)\n    - [Deeper](#deeper)"
def test_missing_file():
    r = subprocess.run([sys.executable, str(WORK / "mdtoc.py"), str(WORK / "nope.md")], capture_output=True, text=True)
    assert r.returncode == 1 and r.stderr.strip()
