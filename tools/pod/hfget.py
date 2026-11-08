#!/usr/bin/env python3
"""hfget.py REPO REVISION LOCAL_DIR PATTERN [PATTERN...]: snapshot_download of the matching files at the pinned
revision into LOCAL_DIR (the repo's own paths under it). Fresh from Hugging Face every time, onto the local disk."""
import sys
from huggingface_hub import snapshot_download
repo, rev, out, pats = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
p = snapshot_download(repo_id=repo, revision=rev, local_dir=out, allow_patterns=pats, max_workers=8)
print("downloaded", repo, rev, "->", p, pats, flush=True)
