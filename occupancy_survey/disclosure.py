"""What a file would tell a stranger, and how to stop it telling them that.

This survey asks volunteers to run a receiver and send back numbers, and it
hands them a config and a channel list to do it with. Both directions carry
disclosure risk and the risks are not symmetric:

  OUTBOUND, to the volunteer. Files generated here are built from an FCC
  query centred on the generator's own coordinates. tools/make_channel_list
  used to copy the prediction's `query` block straight through, so the file
  designed to be handed to strangers carried the sender's EXACT LATITUDE AND
  LONGITUDE, plus absolute paths containing their account name. Measured
  2026-11-01 against a synthetic prediction, which still leaked the username
  through a default path.

  INBOUND, from the volunteer. site.py already keeps location coarse and
  opt-in. The gap it cannot close is `site_id`, which the volunteer config
  describes as "any short name" -- and a radio operator's reflex short name
  is their callsign, which resolves to a full street address in the public
  FCC licence database. Six characters of grid square is refused as too
  precise while a callsign walks straight through.

WHAT CANNOT BE HIDDEN, and must therefore be said rather than engineered
around:

  - Local hour of day is load-bearing. Land-mobile traffic follows the local
    working day, so time-of-day analysis needs it; combined with the UTC
    timestamp it reveals the UTC offset, hence a longitude band. There is no
    version of this that keeps the analysis and hides the timezone.
  - The occupancy data IS a location fingerprint. Which public-safety
    channels a receiver hears identifies a region more sharply than a 4
    character grid square does. Nothing can be done about that without
    destroying the measurement, so a volunteer should be told it plainly
    instead of being reassured by the grid-square rule.

This module is a scanner, not a guarantee. It finds the patterns listed
below; it cannot find a disclosure nobody thought of, so a clean result
means "none of these" and never "safe".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A US/Canada amateur callsign, which is what a radio operator reaches for
# when asked for a short site name. Deliberately narrow: this exists to
# catch the reflex, not to police every string.
CALLSIGN_RE = re.compile(r"^[AKNW][A-Z]?[0-9][A-Z]{1,3}$", re.IGNORECASE)

# An absolute path under a user's home directory, which carries the account
# name whatever else it carries.
HOME_PATH_RE = re.compile(
    r"(?:/(?:Users|home)/(?!shared\b)[^/\s\"']+|C:\\Users\\[^\\\s\"']+)",
    re.IGNORECASE)

# Coordinates precise enough to locate a property. Anything with 3+ decimal
# places is ~100 m or better.
_PRECISE_DECIMALS = 3

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Finding:
    """One thing a file would disclose."""
    where: str
    kind: str
    severity: str
    detail: str
    remedy: str

    def __str__(self) -> str:
        return f"[{self.severity:<6}] {self.where}: {self.detail}"


def grid_square(lat: float, lon: float, chars: int = 4) -> str:
    """Maidenhead locator at 4 or 6 characters.

    Used to replace exact coordinates with something that still supports a
    propagation sanity check. 4 characters is about 100 x 160 km.
    """
    if chars not in (4, 6):
        raise ValueError("chars must be 4 or 6")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("lat/lon out of range")
    lon_adj, lat_adj = lon + 180.0, lat + 90.0
    field_lon, field_lat = int(lon_adj // 20), int(lat_adj // 10)
    sq_lon, sq_lat = int((lon_adj % 20) // 2), int((lat_adj % 10) // 1)
    out = (chr(ord("A") + field_lon) + chr(ord("A") + field_lat)
           + str(sq_lon) + str(sq_lat))
    if chars == 6:
        sub_lon = int(((lon_adj % 20) % 2) / (2 / 24))
        sub_lat = int(((lat_adj % 10) % 1) / (1 / 24))
        out += chr(ord("a") + sub_lon) + chr(ord("a") + sub_lat)
    return out


def _looks_like_coordinate(key: str) -> str | None:
    k = key.lower()
    if k in ("lat", "latitude", "rf_lat", "summit_lat", "site_lat"):
        return "latitude"
    if k in ("lon", "lng", "longitude", "rf_lon", "summit_lon", "site_lon"):
        return "longitude"
    return None


def _decimals(value: float) -> int:
    s = f"{value!r}"
    return len(s.split(".", 1)[1]) if "." in s else 0


def scan(obj, where: str = "$") -> list[Finding]:
    """Walk a JSON-like structure and report what it would disclose.

    Returns findings sorted most severe first. An empty list means none of
    the patterns this module knows about were present -- see the module
    docstring for what it cannot see.
    """
    out: list[Finding] = []

    def walk(node, path: str):
        if isinstance(node, dict):
            for k, v in node.items():
                child = f"{path}.{k}"
                axis = _looks_like_coordinate(str(k))
                if axis and isinstance(v, (int, float)) and \
                        not isinstance(v, bool):
                    if float(v) == 0.0:
                        pass  # a fixture placeholder, not a location
                    elif _decimals(float(v)) >= _PRECISE_DECIMALS:
                        out.append(Finding(
                            child, "exact_coordinate", "high",
                            f"{axis} {v} to {_decimals(float(v))} decimals "
                            f"locates a property",
                            "replace with disclosure.grid_square(lat, lon) "
                            "or drop the field"))
                    else:
                        out.append(Finding(
                            child, "coarse_coordinate", "medium",
                            f"{axis} {v} is still a point location",
                            "prefer a 4-character grid square"))
                walk(v, child)
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for m in HOME_PATH_RE.finditer(node):
                out.append(Finding(
                    path, "home_path", "high",
                    f"absolute home path {m.group(0)!r} carries the account "
                    f"name",
                    "store a basename, not a path"))
            if CALLSIGN_RE.match(node.strip()):
                out.append(Finding(
                    path, "callsign", "high",
                    f"{node.strip()!r} is callsign-shaped; a callsign "
                    f"resolves to a street address in the public FCC "
                    f"licence database",
                    "use an arbitrary site label with no personal meaning"))
            if re.fullmatch(r"[A-R]{2}[0-9]{2}[A-X]{2}[0-9]{2}", node.strip(),
                            re.IGNORECASE):
                out.append(Finding(
                    path, "grid_too_precise", "high",
                    f"{node.strip()!r} is an 8-character grid square, about "
                    f"0.5 km",
                    "truncate to 4 characters"))

    walk(obj, where)
    out.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return out


def worst_severity(findings) -> str | None:
    """Highest severity present, or None for an empty list."""
    if not findings:
        return None
    return min((f.severity for f in findings),
               key=lambda s: SEVERITY_ORDER.get(s, 9))


# What a dataset discloses no matter what, stated for informed consent
# rather than engineered around. Written into the volunteer bundle.
UNAVOIDABLE_DISCLOSURES = (
    ("local hour of day",
     "Needed for the measurement: land-mobile traffic follows the local "
     "working day. With the UTC timestamp it reveals your UTC offset, so a "
     "longitude band. There is no version that keeps the analysis and hides "
     "this."),
    ("which channels are active",
     "The occupancy numbers themselves identify a region more sharply than "
     "a 4-character grid square does, because the set of audible "
     "public-safety channels is close to unique. This cannot be removed "
     "without destroying the measurement."),
    ("your receive path",
     "Antenna and filter descriptions are required, because a quiet channel "
     "and a deaf receiver produce the same number. Describe the equipment, "
     "not where it is mounted."),
)


def _load(path: str):
    """Read a dataset or channel list, gzipped or not."""
    import gzip
    import json
    p = __import__("pathlib").Path(path)
    raw = (gzip.decompress(p.read_bytes()) if p.suffix == ".gz"
           else p.read_bytes())
    return json.loads(raw.decode("utf-8"))


def main(argv=None) -> int:
    """`python -m occupancy_survey.disclosure FILE...`

    Shipped in the volunteer bundle so an operator can audit a file before
    sending it, without taking anyone's word for what is in it.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m occupancy_survey.disclosure",
        description="Report what a dataset or channel list would disclose.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--quiet", action="store_true",
                    help="print only files with findings")
    args = ap.parse_args(argv)

    worst = None
    for path in args.files:
        try:
            data = _load(path)
        except Exception as e:
            print(f"{path}: cannot read ({e})", file=sys.stderr)
            worst = "high"
            continue
        found = scan(data, path)
        if found:
            print(f"\n{path}: {len(found)} finding(s)")
            for f in found:
                print(f"  {f}")
                print(f"      -> {f.remedy}")
            sev = worst_severity(found)
            if worst is None or SEVERITY_ORDER.get(sev, 9) < \
                    SEVERITY_ORDER.get(worst, 9):
                worst = sev
        elif not args.quiet:
            print(f"{path}: nothing found")

    print("\nWhat this cannot tell you:")
    for name, why in UNAVOIDABLE_DISCLOSURES:
        print(f"  - {name}: {why.splitlines()[0]}")
    print("\nA clean result means none of the patterns above were present. "
          "It does not mean the file is safe to publish.")
    return 1 if worst == "high" else 0


if __name__ == "__main__":
    raise SystemExit(main())
