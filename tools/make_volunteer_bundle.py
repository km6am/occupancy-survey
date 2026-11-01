"""Build the smallest thing you can hand a stranger and ask them to run.

The goal is minimum disclosure in BOTH directions, which are different
problems:

  WHAT THEY RECEIVE should contain no trace of you. Anything generated from
  an FCC query is centred on your coordinates, and an earlier channel list
  carried your exact latitude, longitude and account name. So every file
  going into the bundle is scanned with occupancy_survey.disclosure, and a
  high-severity finding REFUSES the build rather than warning about it.

  WHAT THEY SEND BACK should be the numbers and nothing more. The bundle
  therefore ships a config whose location field is optional and deletable,
  and a DISCLOSURE.md stating what a returned dataset reveals anyway --
  including the parts that cannot be hidden. A volunteer who is told "your
  location is protected" and later works out that their channel list
  identifies their county has been misled, and will not send a second file.

MINIMUM MEASURED, NOT GUESSED. The module list is what the rtl_sdr path
actually imports, checked by importing it; the analysis tools, the GNU
Radio backends, the tests and the docs are all left out.

    python tools/make_volunteer_bundle.py --out /tmp/volunteer-kit.zip
    python tools/make_volunteer_bundle.py --out kit.zip --channel-list cl.json
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from occupancy_survey import disclosure  # noqa: E402

# Measured by importing the rtl_sdr path and listing occupancy_survey
# modules that loaded; plus reference/, which --check reads, and
# disclosure.py, so the volunteer can audit their own file before sending.
MODULES = (
    "occupancy_survey/__init__.py",
    "occupancy_survey/__main__.py",
    "occupancy_survey/backends/__init__.py",
    "occupancy_survey/backends/base.py",
    "occupancy_survey/backends/rtl_sdr_pipe.py",
    "occupancy_survey/backends/rtl_power_csv.py",
    "occupancy_survey/channels.py",
    "occupancy_survey/config.py",
    "occupancy_survey/dataset.py",
    "occupancy_survey/detector.py",
    "occupancy_survey/disclosure.py",
    "occupancy_survey/occupancy.py",
    "occupancy_survey/preflight.py",
    "occupancy_survey/presence.py",
    "occupancy_survey/reference/__init__.py",
    "occupancy_survey/reference/duty_cycles_section3.json",
    "occupancy_survey/schedule.py",
    "occupancy_survey/site.py",
    "occupancy_survey/survey.py",
    # Imported lazily by survey.py when a status interval is set, which the
    # default run does -- excluding it would fail at run time, not at build
    # time, on the volunteer's machine.
    "occupancy_survey/display.py",
)

# Deliberately NOT bundled: intermod.py and overload_watch.py (front-end
# diagnostics for a gain-controllable SDR), the GNU Radio backends, every
# analysis tool, the tests and the docs. Verified by extracting the bundle
# and running --check from it with only numpy and PyYAML present.

EXTRAS = ("config.volunteer.yaml", "LICENSE",
          "docs/volunteer-quickstart.md")


def _readme() -> str:
    return """# Occupancy survey: volunteer kit

You are being asked to log, per radio channel, **what fraction of the time a
carrier is present**. Nothing is demodulated and no audio or IQ is ever
recorded -- the receiver interface in this kit yields power spectra only,
which is structural rather than a policy you have to trust.

## What you need

* Python 3.10 or newer, with `numpy` and `PyYAML`
* an RTL-SDR dongle and the `rtl_sdr` binary on your PATH
* an antenna, indoors is fine

## Running it

    pip install numpy PyYAML
    cp config.volunteer.yaml config.yaml

Edit the three lines marked `EDIT ME`, then:

    python -m occupancy_survey --config config.yaml --check
    python -m occupancy_survey --config config.yaml

`--check` touches no hardware. Leave the survey running as long as you like;
it writes one file per interval into `./data`.

## Before you send anything back

    python -m occupancy_survey.disclosure data/<file>.json

Read `DISCLOSURE.md`. It says what a returned file reveals, including the
parts that **cannot** be hidden. Send files only if you are content with
that; a partial contribution with the location line deleted is genuinely
useful.

## What is not in this kit

The analysis tools, the GNU Radio backends, the test suite and the FCC
prediction pipeline. You do not need them to contribute, and leaving them
out keeps this small enough to read before you run it -- which, given a
stranger asked you to run it, is the right amount of trust to extend.
"""


def _disclosure_md() -> str:
    lines = [
        "# What a returned dataset tells us\n",
        "Written for informed consent. The aim is that nothing here is a",
        "surprise later.\n",
        "## Kept coarse or optional\n",
        "* **Location is opt-in.** `grid_square` can be deleted outright and",
        "  the dataset stays valid and mergeable. If you keep it, 4",
        "  characters is about 100 x 160 km. Anything finer than 6 is",
        "  refused rather than silently truncated.",
        "* **No audio, no IQ, no content.** The receiver interface yields",
        "  power spectra. There is no code path in this kit that could",
        "  record a conversation.",
        "* **No host fingerprint.** The dataset records your Python version",
        "  and OS family, not your kernel build, hostname or username.",
        "* **`site_id` is yours to choose.** Please do NOT use your",
        "  callsign: it resolves to a full street address in the public FCC",
        "  licence database, which gives away far more than the grid square",
        "  rule above protects. Any arbitrary label works.\n",
        "## Cannot be hidden, and we would rather say so\n",
    ]
    for name, why in disclosure.UNAVOIDABLE_DISCLOSURES:
        lines.append(f"* **{name}.** {why}")
    lines += [
        "\n## Checking for yourself\n",
        "    python -m occupancy_survey.disclosure data/<file>.json\n",
        "That scanner reports exact coordinates, callsign-shaped names,",
        "over-precise grid squares and absolute paths holding an account",
        "name. A clean result means none of those were found -- not that",
        "the file is safe, because it cannot find a disclosure nobody",
        "thought of.\n",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="volunteer-kit.zip")
    ap.add_argument("--channel-list", default=None,
                    help="targeted channel list JSON to include")
    ap.add_argument("--allow-disclosure", action="store_true",
                    help="build even if a file trips the scanner (it will "
                         "still be reported)")
    args = ap.parse_args(argv)

    members: list[tuple[str, bytes]] = []
    for rel in MODULES + EXTRAS:
        src = ROOT / rel
        if not src.is_file():
            if rel in EXTRAS:
                print(f"note: {rel} absent, skipped", file=sys.stderr)
                continue
            raise SystemExit(f"missing required file {src}")
        members.append((rel, src.read_bytes()))

    findings = []
    if args.channel_list:
        cl = Path(args.channel_list)
        data = json.loads(cl.read_text(encoding="utf-8"))
        findings = disclosure.scan(data, cl.name)
        members.append(("channel_list.json", cl.read_bytes()))

    # Also scan the text files, which is where a path or a callsign would
    # hide if one were pasted into a config comment.
    for rel, blob in members:
        if rel.endswith((".yaml", ".md", ".json")):
            try:
                findings += disclosure.scan(
                    blob.decode("utf-8", "replace"), rel)
            except Exception:                             # pragma: no cover
                pass

    if findings:
        print(f"{len(findings)} disclosure finding(s):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
            print(f"      -> {f.remedy}", file=sys.stderr)
        if disclosure.worst_severity(findings) == "high" and \
                not args.allow_disclosure:
            raise SystemExit(
                "refusing to build: a file going to a stranger would "
                "disclose the above. Fix it, or pass --allow-disclosure if "
                "you have decided it is acceptable.")
    else:
        print("disclosure scan: nothing found "
              "(see occupancy_survey/disclosure.py for what that misses)")

    members.append(("README.md", _readme().encode("utf-8")))
    members.append(("DISCLOSURE.md", _disclosure_md().encode("utf-8")))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, blob in members:
            z.writestr(rel, blob)

    size = out.stat().st_size
    print(f"\nwrote {out}  ({size/1024:.0f} KiB, {len(members)} files)")
    print("contents:")
    for rel, blob in sorted(members):
        print(f"  {len(blob):>8}  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
