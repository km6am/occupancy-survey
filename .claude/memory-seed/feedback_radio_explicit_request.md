---
name: feedback-radio-explicit-request
description: Every single command that triggers the SDR needs its own explicit request to the user first
metadata:
  type: feedback
---

Never run a command that opens or transmits on the radio. For **each
individual prompt that will trigger the radio**, make an explicit request
and let the user run it or approve it. Approval for one radio command never
carries over to the next one, even an identical re-run.

Asking for a *session* to test the radio is acceptable; what is not
acceptable is a session or a loop that then exercises the radio repeatedly
off one approval.

**Why:** The user asked for this explicitly, beyond the project's existing
"do not open any serial port or SDR device unprompted" constraint. The SDR
is shared physical hardware that other software may be claiming, and a
transmit-capable or antenna-switching command has effects outside this
process that I cannot see or undo.

**How to apply:** Put the command in its own fenced `bash` block and say
what it will do to the hardware. One block per radio action. Re-running the
same check after a config change is a new action and needs a new request.
Non-radio work — tests, analysis, edits — needs none of this. Related:
[[feedback-abort-phrase]], [[feedback-use-radioconda]].
