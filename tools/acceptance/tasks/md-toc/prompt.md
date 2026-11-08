Write a command-line tool `mdtoc.py` (Python 3, standard library only) that prints a table of contents for a Markdown file.

Usage: `python3 mdtoc.py FILE [--max-depth N] [--min-depth N]`. It reads ATX headings (`#` to `######`), ignores headings inside fenced code blocks (``` or ~~~), and prints one line per heading, indented two spaces per level below the shallowest level that appears, as a Markdown list item linking to the heading's GitHub-style anchor: lowercase, spaces to hyphens, punctuation other than hyphens and underscores removed, and a `-1`, `-2`, ... suffix on repeated anchors. Setext headings (underlined with `===` or `---`) count as levels 1 and 2. `--max-depth` and `--min-depth` filter by heading level. Exit 0 on success, 1 with a message on stderr if the file cannot be read.

Example: for the lines `# Intro`, `## Setup`, `### Linux`, `## Setup` the output is
```
- [Intro](#intro)
  - [Setup](#setup)
    - [Linux](#linux)
  - [Setup](#setup-1)
```

Write `test_mdtoc.py` with pytest tests covering fenced code blocks, repeated anchors, setext headings, the depth filters and punctuation in headings, and run `python3 -m pytest -q` until it passes. Finish with `NOTES.md`: what you built, how you tested it, and anything you left out.
