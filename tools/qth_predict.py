"""Ask sota-rf what should be audible at a fixed location.

sota-rf has no arbitrary-location mode: it takes --association or --us and
joins against the SOTA summit list. But its `load_summits()` only needs a
CSV whose header row starts with "SummitCode", and it filters on the prefix
before the "/". So a one-row pseudo-summit file plus --summits-file and
--association points the whole pipeline at a QTH.

READ-ONLY BY CONSTRUCTION, as the task requires. This runs sota-rf with:
  --summits-file   a pseudo-summit written into a scratch directory
  --data-dir       the EXISTING FCC cache, with --no-download so nothing is
                   fetched or rewritten
  --out-dir        a scratch directory, never the sota-rf tree
and it does not import or edit anything in that repo.

WHAT THIS DOES AND DOES NOT PRODUCE. sota-rf gives ERP and distance per
emitter, so the prediction is a received-power proxy, ERP/d^2. That is
*uncalibrated* -- it has no antenna pattern, no terrain, no feedline loss
and no receiver sensitivity in it -- so it supports an ORDERING of emitters
and not an absolute level. Nothing downstream may convert it to dBm, and
tools/validate_occupancy.py compares ranks rather than magnitudes.

A QTH IS NOT A SUMMIT. This is the prediction for one fixed location with
one antenna. It is not a summit field strength and must not be presented as
one: the propagation path and the antenna are both different.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from occupancy_survey import reference as _reference

# Overridable, because the machine that ANALYSES is not necessarily the
# machine that SURVEYS. The survey host needs numpy and a backend; this
# tool needs sota-rf and its FCC cache, and the two rarely coincide.
SOTA_RF_DIR = Path(os.environ.get("SOTA_RF_DIR",
                                  Path.home() / "sota-rf")).expanduser()
SOTA_RF_SCRIPT = SOTA_RF_DIR / "sota_rf_sources.py"
FCC_CACHE = SOTA_RF_DIR / "fccdata"

PSEUDO_ASSOCIATION = "QTH"
PSEUDO_CODE = f"{PSEUDO_ASSOCIATION}/SITE-001"

# Both contracts live in occupancy_survey/reference/ now, with the columns
# sota-rf actually emitted recorded beside them, so a change on its side
# surfaces here as a named failure instead of a column of None.
SUMMIT_HEADER = list(_reference.SUMMIT_CSV_HEADER)


def write_pseudo_summit(path: Path, lat: float, lon: float,
                        alt_m: float = 0.0, name: str = "QTH") -> Path:
    """One-row summit list for sota-rf's --summits-file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="ascii", errors="replace") as f:
        w = csv.writer(f)
        w.writerow(SUMMIT_HEADER)
        row = {k: "" for k in SUMMIT_HEADER}
        row.update({"SummitCode": PSEUDO_CODE, "SummitName": name,
                    "RegionName": "QTH", "AssociationName": "QTH",
                    "AltM": f"{alt_m:g}", "Longitude": f"{lon:.6f}",
                    "Latitude": f"{lat:.6f}", "Points": "0",
                    "ActivationCount": "0"})
        w.writerow([row[k] for k in SUMMIT_HEADER])
    return path


def run_sota_rf(summits_file: Path, out_dir: Path, radius_m: float,
                extra_args=(), timeout_s: float = 3600.0) -> Path:
    """Run sota-rf read-only and return its per-emitter CSV."""
    if not SOTA_RF_SCRIPT.exists():
        raise FileNotFoundError(
            f"{SOTA_RF_SCRIPT} not found. This tool reads sota-rf; it does "
            f"not vendor it.")
    if not FCC_CACHE.is_dir():
        raise FileNotFoundError(
            f"{FCC_CACHE} not found. Run sota-rf once with downloads enabled "
            f"to populate the FCC cache; this tool will not fetch anything.")

    out_dir.mkdir(parents=True, exist_ok=True)
    # Absolute paths throughout: cwd is the scratch directory, so a relative
    # --summits-file would be resolved against it and not found.
    out_dir = out_dir.resolve()
    summits_file = Path(summits_file).resolve()
    cmd = [sys.executable, str(SOTA_RF_SCRIPT),
           "--association", PSEUDO_ASSOCIATION,
           "--summits-file", str(summits_file),
           "--data-dir", str(FCC_CACHE.resolve()),
           "--out-dir", str(out_dir),
           "--no-download",
           "--radius", str(radius_m),
           *extra_args]
    print("running:", " ".join(cmd), file=sys.stderr)
    # cwd is the scratch out_dir, not the sota-rf tree, so any relative
    # path the script writes lands in scratch.
    proc = subprocess.run(cmd, cwd=str(out_dir), timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"sota-rf exited {proc.returncode}")

    csv_path = out_dir / f"{PSEUDO_ASSOCIATION}_rf_sources.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"sota-rf produced no {csv_path.name}. With one pseudo-summit "
            f"this usually means nothing was found within the radius or the "
            f"field-strength floor -- try a larger --radius.")
    return csv_path


# --------------------------------------------------------------------------- #
# Turning the emitter list into per-channel predictions
# --------------------------------------------------------------------------- #

def _floats(text: str):
    out = []
    for token in str(text or "").replace(";", " ").replace(",", " ").split():
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def parse_emitters(csv_path: Path, bands) -> list[dict]:
    """Per-emitter predictions, restricted to the surveyed bands.

    One licence record can list several frequencies; each becomes its own
    predicted channel, because occupancy is measured per channel.
    """
    from occupancy_survey.channels import continuity_for

    spans = [(b.name, b.freq_min_hz, b.freq_max_hz, b.role) for b in bands]
    rows = []
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        missing = _reference.missing_sota_rf_columns(reader.fieldnames)
        if missing:
            # Every one of these is read below. Without them the rows still
            # parse and the prediction still writes -- with None where the
            # ERP, the distance or the service class should be, which is a
            # confidently empty answer rather than an error.
            raise SystemExit(
                f"{csv_path}: sota-rf output is missing required column(s) "
                f"{', '.join(missing)}. Expected the 22-column header "
                f"recorded in occupancy_survey/reference/; if sota-rf has "
                f"changed its output, update SOTA_RF_SOURCES_COLUMNS there "
                f"rather than reading around the gap.")
        for rec in reader:
            dist_m = None
            try:
                dist_m = float(rec.get("distance_m") or "nan")
            except ValueError:
                pass
            erp_w = None
            try:
                erp_w = float(rec.get("rf_max_power_w") or "nan")
            except ValueError:
                pass

            service = (rec.get("rf_services") or "").strip()
            cont = continuity_for(service)

            for mhz in _floats(rec.get("rf_freqs_mhz")):
                hz = mhz * 1e6
                band = next(((n, role) for n, lo, hi, role in spans
                             if lo <= hz <= hi), None)
                if band is None:
                    continue
                proxy = None
                if erp_w and dist_m and dist_m > 0 and math.isfinite(erp_w) \
                        and math.isfinite(dist_m):
                    proxy = erp_w / (dist_m ** 2)
                rows.append({
                    "freq_hz": hz,
                    "band": band[0],
                    "band_role": band[1],
                    # Carried so the summit tag is a join rather than a
                    # second expensive sota-rf run: rf_ref identifies the
                    # same emitter in that tool's per-summit output.
                    "rf_ref": (rec.get("rf_ref") or "").strip(),
                    "rf_lat": rec.get("rf_lat"),
                    "rf_lon": rec.get("rf_lon"),
                    "owner": (rec.get("rf_owner") or "").strip()[:60],
                    "service": service,
                    "continuity": cont.continuity,
                    "expected_duty": cont.expected_duty,
                    "continuity_basis": cont.basis,
                    "distance_m": dist_m,
                    "erp_w": erp_w,
                    "field_proxy_w_per_m2": proxy,
                })
    rows.sort(key=lambda r: (r["field_proxy_w_per_m2"] or -1.0), reverse=True)
    return rows


def write_prediction(rows, path: Path, meta: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "occupancy-survey/qth-prediction/1",
        "source": "sota-rf (read-only)",
        "caveats": {
            "field_proxy": "ERP / distance^2, UNCALIBRATED. No antenna "
                           "pattern, terrain, feedline loss or receiver "
                           "sensitivity. Supports an ordering of emitters, "
                           "not an absolute level, and must not be "
                           "converted to dBm.",
            "not_a_summit": "This is the prediction for one fixed location "
                            "with one antenna. It is not a summit field "
                            "strength and the two must not be conflated.",
            "continuity": "Continuity classes are architectural, from "
                          "duty_cycles.md section 2. Nothing in them has "
                          "been measured; that is what the survey is for.",
        },
        "query": meta,
        "n_predicted_channels": len(rows),
        "channels": rows,
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Predict what should be audible at a fixed location, "
                    "using sota-rf read-only.")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--alt-m", type=float, default=0.0)
    ap.add_argument("--radius", type=float, default=30000.0,
                    help="base inclusion radius in metres (default 30 km). "
                         "sota-rf also keeps anything clearing its "
                         "field-strength floor beyond this.")
    ap.add_argument("--bands", default="vhf_lmr_low,vhf_lmr_high,uhf_lmr,"
                                       "nwr,bcast_fm")
    ap.add_argument("--work-dir", default="./out/qth_prediction")
    ap.add_argument("--out", default="./out/qth_prediction/prediction.json")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing sota-rf CSV in --work-dir "
                         "instead of rerunning it")
    args = ap.parse_args(argv)

    from occupancy_survey.channels import resolve_bands
    bands = resolve_bands(b.strip() for b in args.bands.split(",") if b.strip())

    work = Path(args.work_dir)
    csv_path = work / f"{PSEUDO_ASSOCIATION}_rf_sources.csv"
    if not (args.reuse and csv_path.exists()):
        summits = write_pseudo_summit(work / "qth_summits.csv", args.lat,
                                      args.lon, args.alt_m)
        csv_path = run_sota_rf(summits, work, args.radius)

    rows = parse_emitters(csv_path, bands)
    out = write_prediction(rows, Path(args.out), {
        "lat": args.lat, "lon": args.lon, "alt_m": args.alt_m,
        "radius_m": args.radius,
        "bands": [b.name for b in bands],
        "sota_rf_csv": str(csv_path),
    })

    print(f"{len(rows)} predicted channels in the surveyed bands -> {out}")
    by_cont: dict[str, int] = {}
    for r in rows:
        by_cont[r["continuity"]] = by_cont.get(r["continuity"], 0) + 1
    for k, v in sorted(by_cont.items()):
        print(f"  {k:<13} {v}")
    print("\nstrongest predicted (uncalibrated ERP/d^2 ordering only):")
    for r in rows[:12]:
        proxy = r["field_proxy_w_per_m2"]
        print(f"  {r['freq_hz']/1e6:10.4f} MHz  {r['continuity']:<12} "
              f"{'' if proxy is None else f'{proxy:.2e}':>10}  "
              f"{r['service']:<6} {r['owner'][:34]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
