# Memory seed

These are copies of Claude Code memory files, vendored so they travel with
the repository. They are a **seed, not live memory** — nothing reads them
from here.

## Why they are here

Claude Code keeps memory per *project directory*, under

    ~/.claude/projects/<mangled-working-directory>/memory/

That path is derived from the session's working directory, so the files do
not travel with a `git push` and the directory name does not survive a move
to a machine with a different user or layout.

There is a second problem, found 2026-11-01: these three were written by a
session whose working directory was a **morse_code_recorder worktree**, not
this repository. They are filed under that project key, so a session opened
directly in this repo never loaded them — on the original machine either.

## What actually protects a session in this repo

`CLAUDE.md` at the repository root. Both safety rules — the per-prompt radio
request and the "my airplane" abort — are stated there in full, and that file
is tracked. Treat it as authoritative. These seeds carry the *reasoning*
behind the rules, which is worth having but is not what enforces them.

## Installing them as live memory (optional)

Open a Claude Code session in this repository and ask it to install the seed
into memory. Or by hand, with the session's own project directory:

    ls ~/.claude/projects/            # find the key for this repo
    cp .claude/memory-seed/feedback_*.md ~/.claude/projects/<key>/memory/

then add one line per file to that directory's `MEMORY.md`, which is the
index Claude loads. Copying the files without indexing them does nothing.
