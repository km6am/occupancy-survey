"""Load rf_feedback.clean_survey from a local sota-rf checkout, or None.

The contract for a feedback body is that function, not this repo's prose
about it: the body shape was once built from the proposal's wording and
would have been rejected on the first required field. Any test that claims
a body is acceptable has to run it through this.

It lives on sota-rf branch qth-rfi-report and is not deployed, so loading
it is best-effort. Tests skip when it is absent rather than pretending the
contract was checked -- a green run on a machine without that worktree
proves this repo agrees with itself and nothing more.
"""

import importlib.util
import os
from pathlib import Path

SKIP_REASON = ("sota-rf's rf_feedback.py not found, so the body shape was "
               "NOT checked against the server's own validator")


def server_validator():
    roots = []
    env = os.environ.get("SOTA_RF_DIR")
    if env:
        roots.append(Path(env).expanduser())
    roots.append(Path.home() / "sota-rf")
    candidates = []
    for root in roots:
        candidates.append(root / "deploy" / "droplet" / "rf_feedback.py")
        candidates.extend(sorted(
            root.glob(".claude/worktrees/*/deploy/droplet/rf_feedback.py")))
    for path in candidates:
        if not path.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "rf_feedback_under_test", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            continue
        if hasattr(mod, "clean_survey"):
            return mod
    return None


SERVER = server_validator()
