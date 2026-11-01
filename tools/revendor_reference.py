"""Re-extract occupancy_survey/reference/ from the upstream repositories.

READ-ONLY on the sources, like everything else here: it hashes and reads,
and writes only inside this repository.

Run it when check_duty_cycles_drift() reports the source has changed. It
does not resolve the change for you -- it rewrites the vendored copy and
prints what moved, and the rows that moved still need reading, because a
continuity class changing upstream may invalidate a comparison already
made against the old one.

    python tools/revendor_reference.py --dry-run
    python tools/revendor_reference.py
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_SRC = Path.home() / "Code/hp8935_sinad_serial"
REL = "docs/reference/duty_cycles.md"
OUT = Path(__file__).resolve().parent.parent / \
    "occupancy_survey/reference/duty_cycles_section3.json"


def git(src: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(src), *args],
                       capture_output=True, text=True)
    return r.stdout.strip()


def pick_branch(src: Path, requested: str | None) -> str:
    """Which branch to read duty_cycles.md from.

    It is absent on the source repo's `main` and differs between feature
    branches, so there is no "latest" to default to. Refuse to guess.
    """
    if requested:
        if not git(src, "rev-parse", "--verify", f"{requested}:{REL}"):
            raise SystemExit(f"{REL} not found on branch {requested}")
        return requested
    carriers = []
    for ref in git(src, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads/").splitlines():
        if git(src, "rev-parse", "--verify", f"{ref.strip()}:{REL}"):
            carriers.append(ref.strip())
    if not carriers:
        raise SystemExit(f"no branch in {src} carries {REL}")
    blobs = {git(src, "rev-parse", f"{b}:{REL}") for b in carriers}
    if len(blobs) > 1:
        raise SystemExit(
            f"{REL} differs between branches that carry it ({', '.join(carriers)}). "
            f"Pass --branch to say which one is authoritative; this tool "
            f"will not pick for you.")
    return carriers[0]


def extract(src: Path, branch: str) -> dict:
    text = (src / REL).read_text(encoding="utf-8")
    if "## 3. The table" not in text:
        raise SystemExit(f"{src / REL}: no '## 3. The table' heading -- the "
                         f"document has been restructured; re-read it before "
                         f"re-vendoring.")
    sec = text.split("## 3. The table", 1)[1].split("### 3.1", 1)[0]
    rows = [ln.strip() for ln in sec.splitlines()
            if ln.strip().startswith("|")
            and not re.match(r"^\|[\s\-|]+\|$", ln.strip())]
    if len(rows) < 2:
        raise SystemExit("section 3 holds no table rows")
    header, body = rows[0], rows[1:]

    parsed = []
    for ln in body:
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) < 4:
            raise SystemExit(f"unexpected row shape: {ln}")
        parsed.append({"service": c[0], "duty_cycle": c[1], "class": c[2],
                       "basis": c[3], "verbatim": ln})

    prior = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    return {
        "schema": "occupancy-survey/vendored-duty-cycles/1",
        "what": prior.get("what", "Section 3 of duty_cycles.md."),
        "authority": prior.get(
            "authority", "NOT authoritative; the source file below is."),
        "source": {
            "repo": "https://github.com/km6am/hp8935_sinad_serial",
            "path": REL,
            "section": "3. The table",
            "branch_read": branch,
            "branch_commit": git(src, "rev-parse", branch),
            "blob_sha1": git(src, "rev-parse", f"{branch}:{REL}"),
            "file_sha256": hashlib.sha256(
                (src / REL).read_bytes()).hexdigest(),
            "file_last_modified_commit": git(
                src, "log", "-1", "--format=%H", branch, "--", REL),
            "file_last_modified_date": git(
                src, "log", "-1", "--format=%ad", "--date=short", branch,
                "--", REL),
            "captured": __import__("datetime").date.today().isoformat(),
            "caution": prior.get("source", {}).get("caution", ""),
        },
        "evidential_status": prior.get(
            "evidential_status",
            "Nothing in this table is measured."),
        "table_header": header,
        "rows": parsed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-repo", default=str(DEFAULT_SRC))
    ap.add_argument("--branch", default=None,
                    help="branch to read; required when branches disagree")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.source_repo).expanduser()
    if not (src / REL).is_file():
        raise SystemExit(f"{src / REL} not found")

    branch = pick_branch(src, args.branch)
    fresh = extract(src, branch)

    old_rows = (json.loads(OUT.read_text(encoding="utf-8"))["rows"]
                if OUT.exists() else [])
    a = [r["verbatim"] for r in old_rows]
    b = [r["verbatim"] for r in fresh["rows"]]
    if a == b:
        print(f"table unchanged ({len(b)} rows), branch {branch}")
    else:
        print(f"TABLE CHANGED, branch {branch}:")
        for line in difflib.unified_diff(a, b, "vendored", "upstream",
                                         lineterm="", n=0):
            print("  " + line)
        print("\nRead the changed rows before trusting any comparison made "
              "against the old ones.")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    OUT.write_text(json.dumps(fresh, indent=1) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
