---
name: feedback-abort-phrase
description: "my airplane" / "my controls" / "my radio" means stop everything immediately and hand back control
metadata:
  type: feedback
---

If the user says **"my airplane"**, **"my controls"**, or **"my radio"**, that
is an abort. Immediately:

1. Relinquish the radio — stop any process holding the SDR, start nothing new.
2. Stop code tasks. Do not finish the current edit, test run, or commit
   sequence just because it is nearly done.
3. Reach the closest clean state as fast as possible: no half-applied edits,
   no broken tree, no running background jobs.
4. Say in one or two lines where things stand and what is uncommitted.
5. End the turn. Do not propose next steps or ask permission to continue.

**Why:** It is the aviation positive-exchange-of-control convention, and the
user has said it will be used when I am going in an incorrect direction. The
value is entirely in the speed of the handover — a tidy wind-down that takes
four more tool calls defeats the purpose. Treating it as a suggestion to
wrap up gracefully would be the specific failure it exists to prevent.

**How to apply:** Act on the phrase the moment it appears, including
mid-turn. "Closest clean state" means fastest safe stop, not "finished".
Prefer leaving work uncommitted and saying so over spending calls to tidy
it. Related: [[feedback-radio-explicit-request]].
