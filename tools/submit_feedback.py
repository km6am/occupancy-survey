"""Send one QTH survey back to sota-rf as sota-rf/qth-feedback/1.

WHAT LEAVES THIS MACHINE, AND WHY IT IS AN ALLOWLIST. A /2 prediction
carries distance_m and each emitter's coordinates. Two such rows
trilaterate the receiver as surely as its latitude does, so a denylist
would be one new field away from leaking the station. Only the fields in
PREDICTION_FIELDS and ACQUISITION_FIELDS are copied, by name, and the body
is then scanned a second time for the forbidden names anywhere at any
depth -- belt and braces, because the allowlist is only as good as the
code that applies it.

DRY RUN BY DEFAULT. Nothing is posted without --send. The body is written
to a file so it can be read in full first; reading it is the point.

THE TOKEN COMES FROM SUMMITRFI_TOKEN AND NOWHERE ELSE. Not an argument,
which lands in shell history. Not a query string, which nginx logs. It is
never written into the body and never printed.

WHAT THIS CANNOT HIDE, and no version of it could:
  - the set of channels you heard. The audible public-safety channel set
    at one point is close to unique, so it locates you more sharply than a
    4-character grid square does. Submitting occupancy at all discloses
    roughly where you are. That is the trade; it is stated rather than
    engineered away.
  - your local hour of day, which with a UTC timestamp gives a longitude
    band.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

ENDPOINT = "https://km6am.com/summitRFI/api/surveys"
SCHEMA = "sota-rf/qth-feedback/1"

# Per predicted channel. Everything else in a /2 row stays local.
PREDICTION_FIELDS = ("freq_mhz", "ref", "db", "service", "p_dbm_ref",
                     "error_db", "path", "rank")

# Refused by the server anywhere in the body, and refused here before it
# ever gets that far. Kept as its own tuple rather than derived, so a
# change in the prediction's shape cannot quietly shrink it.
FORBIDDEN_FIELDS = ("distance_m", "bearing_deg", "lat", "lon", "fspl_db",
                    "diffraction_db", "rf_lat", "rf_lon", "latitude",
                    "longitude", "operator_note")

MAX_ROWS = 100_000
MAX_GZIP_BYTES = 2_000_000


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

def grid_for_submission(grid) -> tuple[str | None, str]:
    """A 4- or 6-character locator, or withheld. Never coordinates.

    6 characters is about 5 x 8 km. 8 is about 0.5 km, which is a
    neighbourhood, so it is withheld rather than truncated: an operator who
    wrote 8 characters into their config may not have meant to share even
    the first 6, and silently shortening it would make that decision for
    them.
    """
    if not grid:
        return None, "withheld: no grid square in the config"
    text = str(grid).strip()
    if len(text) in (4, 6):
        return text[:4].upper() + text[4:].lower(), (
            f"maidenhead {len(text)}-character")
    return None, (
        f"withheld: {len(text)}-character locator. The server accepts 4 or "
        f"6; set site.grid_square to one of those if you mean to share it.")


# --------------------------------------------------------------------------- #
# The two row lists
# --------------------------------------------------------------------------- #

def prediction_rows(prediction, bands=None) -> list[dict]:
    """Allowlisted predicted channels, ranked within this submission.

    `rank` is 1..N over the rows ACTUALLY SENT, by field proxy descending.
    It is deliberately not the report's own rank, which is computed over
    the whole queried span before the band filter and so is neither 1..N
    here nor comparable between submissions with different band sets.
    """
    rows = [r for r in prediction.get("channels") or ()
            if bands is None or r.get("band") in bands]
    rows.sort(key=lambda r: (r.get("field_proxy_w_per_m2") or -1.0),
              reverse=True)

    out = []
    for i, r in enumerate(rows):
        row = {
            "freq_mhz": (None if r.get("freq_hz") is None
                         else round(r["freq_hz"] / 1e6, 6)),
            "ref": r.get("rf_ref") or None,
            "db": r.get("db") or None,
            "service": r.get("service") or None,
            "p_dbm_ref": r.get("p_dbm_ref"),
            "error_db": r.get("error_db"),
            "path": r.get("path"),
            "rank": i + 1,
        }
        out.append({k: row[k] for k in PREDICTION_FIELDS})
    return out


def _value_or_bound(pct_value, hist_spec) -> float | None:
    """None when the percentile is a histogram EDGE rather than a value.

    percentile_from_hist() returns lo_db exactly when the percentile falls
    in the underflow and the top edge exactly when it falls in the
    overflow; a genuine in-bin result is always a bin CENTRE, lo + (i+0.5)
    * bin, so it can never equal either edge. Those two returns are bounds,
    not measurements.

    They are sent as null because the server does arithmetic on these: a
    clamped 0.0 dB would enter a residual as a measured excess and bias it,
    and "the median was below the histogram floor" is not a number anyone
    downstream can use. The same rule as `duty`, for the same reason.
    """
    if pct_value is None or not hist_spec:
        return None
    try:
        lo = float(hist_spec["lo_db"])
        top = lo + float(hist_spec["bin_db"]) * int(hist_spec["n_bins"])
    except (KeyError, TypeError, ValueError):
        return float(pct_value)
    if pct_value in (lo, top):
        return None
    return float(pct_value)


def measured_rows(measured, bands=None, hist_spec=None) -> list[dict]:
    """Per-channel duty, with null where nothing was decided.

    null, never 0: a channel that was never decided is unmeasured, and a
    zero duty is a measurement. Collapsing the two would feed the shared
    model confident silence it never observed.
    """
    out = []
    for m in sorted(measured.values(), key=lambda m: m["freq_hz"]):
        if bands is not None and m.get("band") not in bands:
            continue
        n = int(m.get("n_decided") or 0)
        occ = m.get("occupancy")
        out.append({
            "freq_mhz": round(m["freq_hz"] / 1e6, 6),
            "duty": None if (n <= 0 or occ is None) else float(occ),
            "decided_n": n,
            "excess_db_p50": _value_or_bound(m.get("excess_db_p50"),
                                             hist_spec),
            "excess_db_p90": _value_or_bound(m.get("excess_db_p90"),
                                             hist_spec),
        })
    return out


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #

def acquisition_block(datasets, grid, grid_note,
                      intermod_checked: bool) -> dict:
    """The conditions without which no duty cycle means anything.

    Field names follow the dataset's own, so a reader can find each one in
    the file it came from. receiver_model is the exception: it lives under
    site in a dataset and under receiver here, because that is where the
    qth-feedback schema puts it.
    """
    acq = datasets[0]["acquisition"]
    site = acq["site"]

    bins = sorted({int(b["bins_per_channel"])
                   for d in datasets for b in d.get("bands", {}).values()
                   if b.get("bins_per_channel") is not None})

    hours = 0.0
    from datetime import datetime
    for d in datasets:
        try:
            a = datetime.fromisoformat(d["interval"]["start_utc"])
            b = datetime.fromisoformat(d["interval"]["end_utc"])
            hours += max(0.0, (b - a).total_seconds() / 3600.0)
        except (KeyError, ValueError):
            continue

    return {
        "detection": {
            "threshold_db": acq["detection"]["threshold_db"],
            "estimator": acq["detection"]["estimator"],
            "baseline_percentile": acq["detection"]["baseline_percentile"],
        },
        "receiver": {
            "gain": acq["receiver"].get("gain"),
            # Structurally false: config.py refuses an `sdr.agc_enabled`
            # key at all, and dataset.validate() refuses to write a file
            # with it true. Sent because the server checks it.
            "agc_enabled": bool(acq["receiver"].get("agc_enabled")),
            "receiver_model": site.get("receiver_model"),
        },
        "sampling": {
            "dwell_s": acq["sampling"]["dwell_s"],
            "integration_s": acq["sampling"]["integration_s"],
        },
        "histogram": {"bin_db": acq["histogram"]["bin_db"]},
        "site": {
            "site_id": site.get("site_id"),
            "antenna": site.get("antenna"),
            "antenna_height_m": site.get("antenna_height_m"),
            "grid_square": grid,
            "location_precision": grid_note,
            "preamp": site.get("preamp"),
            "feedline": site.get("feedline"),
            "front_end_filters": site.get("front_end_filters"),
        },
        "hours": round(hours, 3),
        "intervals": len(datasets),
        "bins_per_channel": bins,
        # false means NOT EVIDENCED, not clean. An unchecked front end
        # inflates occupancy on predicted-strong channels, which is the
        # direction that flatters the model this feeds.
        "intermod_checked": bool(intermod_checked),
    }


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

def forbidden_in(obj, where="$") -> list[str]:
    """Every path at which a forbidden key name appears, at any depth."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in FORBIDDEN_FIELDS:
                found.append(f"{where}.{k}")
            found.extend(forbidden_in(v, f"{where}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.extend(forbidden_in(v, f"{where}[{i}]"))
    return found


def body_size(body) -> dict:
    """Serialised and gzipped size, measured rather than estimated."""
    raw = json.dumps(body, allow_nan=False).encode("utf-8")
    gz = gzip.compress(raw, compresslevel=9)
    return {"json_bytes": len(raw), "gzip_bytes": len(gz),
            "ratio": (len(raw) / len(gz)) if gz else 0.0}


def check(body) -> list[str]:
    """Everything that would make the server refuse, checked here first."""
    problems = []

    leaks = forbidden_in(body)
    if leaks:
        problems.append(
            f"forbidden field(s) in the body at {', '.join(leaks[:6])}"
            + (f" and {len(leaks) - 6} more" if len(leaks) > 6 else "")
            + ". These locate the receiver; the server refuses them and so "
              "does this tool.")

    acq = body.get("acquisition") or {}
    if (acq.get("receiver") or {}).get("agc_enabled"):
        problems.append(
            "agc_enabled is true. An AGC moves the noise floor every "
            "threshold is relative to, so the duty cycles are not "
            "interpretable.")

    for path in (("detection", "threshold_db"), ("receiver", "gain"),
                 ("sampling", "dwell_s"), ("site", "antenna")):
        if (acq.get(path[0]) or {}).get(path[1]) in (None, ""):
            problems.append(f"acquisition.{path[0]}.{path[1]} is missing; "
                            f"the server refuses a body without it.")

    grid = (acq.get("site") or {}).get("grid_square")
    if grid and len(str(grid)) > 6:
        problems.append(f"grid square {grid!r} is longer than 6 characters.")

    for name in ("predicted", "measured"):
        rows = body.get(name) or []
        if len(rows) > MAX_ROWS:
            problems.append(
                f"{name} has {len(rows)} rows, over the {MAX_ROWS} cap. "
                f"Select with tools/make_channel_list.py --top-per-band "
                f"rather than sending everything.")

    size = body_size(body)
    if size["gzip_bytes"] > MAX_GZIP_BYTES:
        problems.append(
            f"body is {size['gzip_bytes']/1e6:.2f} MB gzipped, over the "
            f"{MAX_GZIP_BYTES/1e6:.1f} MB limit.")
    return problems


def disclosure_problems(body) -> list[str]:
    """High-severity findings from the repo's own scanner.

    A callsign-shaped site_id is the one that matters: a callsign resolves
    to a street address in the public FCC licence database. On the survey
    path that is a warning, because the operator's own files are their
    business. Here the data leaves the machine, so it is a refusal.
    """
    from occupancy_survey.disclosure import scan
    return [f"[{f.severity}] {f.where}: {f.detail} -> {f.remedy}"
            for f in scan(body) if f.severity == "high"]


# --------------------------------------------------------------------------- #
# Building and sending
# --------------------------------------------------------------------------- #

def build_body(prediction, measured, datasets, bands=None,
               intermod_checked: bool = False) -> dict:
    grid, grid_note = grid_for_submission(
        datasets[0]["acquisition"]["site"].get("grid_square"))
    model = prediction.get("model") or {}
    return {
        "schema": SCHEMA,
        "prediction_schema": prediction.get("schema"),
        # Which model built the prediction these measurements are being
        # compared with. A model change changes every prediction, so a
        # submission without this cannot be re-read later.
        "model": {"engine": model.get("engine"), "data": model.get("data"),
                  "dem": model.get("dem")},
        "acquisition": acquisition_block(datasets, grid, grid_note,
                                         intermod_checked),
        "predicted": prediction_rows(prediction, bands),
        "measured": measured_rows(
            measured, bands,
            datasets[0]["acquisition"].get("histogram")),
    }


def post(body, token: str, endpoint: str = ENDPOINT, timeout_s: float = 120.0):
    """POST the body gzipped. The token goes in a header, never a URL."""
    import urllib.error
    import urllib.request

    payload = gzip.compress(
        json.dumps(body, allow_nan=False).encode("utf-8"), compresslevel=9)
    req = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Content-Encoding": "gzip",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Send one QTH survey back to sota-rf. Dry run unless "
                    "--send is given.",
        epilog="The token is read from the SUMMITRFI_TOKEN environment "
               "variable and from nowhere else. There is deliberately no "
               "--token argument: it would land in shell history, and a "
               "query string would land in the server's logs. Get one from "
               "the maintainer (`rf_feedback.py add-token CALL`).")
    ap.add_argument("--prediction", required=True,
                    help="prediction.json from tools/qth_predict.py")
    ap.add_argument("datasets", nargs="+", help="occ_*.json.gz files")
    ap.add_argument("--bands", default=None,
                    help="comma-separated band names to submit (default: "
                         "every band in the datasets)")
    ap.add_argument("--threshold-db", type=float,
                    help="re-threshold the stored histograms instead of "
                         "using each file's default")
    ap.add_argument("--min-decided", type=int, default=100)
    ap.add_argument("--intermod-report", action="append", default=[],
                    metavar="FILE",
                    help="JSON from `--intermod-check --intermod-json`. "
                         "Without one, intermod_checked is false, which "
                         "means NOT EVIDENCED rather than clean.")
    ap.add_argument("--allow-suspect-intermod", action="store_true",
                    help="submit even though a checked band had suspect "
                         "channels. Those occupancy figures may be "
                         "inflated by the receiver's own products.")
    ap.add_argument("--out", default="./out/qth_feedback.json",
                    help="where the dry-run body is written")
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--send", action="store_true",
                    help="actually POST it, gzipped, with the token from "
                         "SUMMITRFI_TOKEN. Without this nothing leaves the "
                         "machine.")
    args = ap.parse_args(argv)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "validate_occupancy", Path(__file__).with_name(
            "validate_occupancy.py"))
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)

    from occupancy_survey.dataset import read_dataset

    # load_measured refuses synthetic datasets, which the server refuses
    # too; doing it here means the refusal names the file.
    measured, _controls, _conditions = validate.load_measured(
        args.datasets, args.threshold_db, args.min_decided)
    datasets = [read_dataset(p) for p in args.datasets]
    prediction = validate.load_prediction(args.prediction)

    intermod = validate.load_intermod_reports(args.intermod_report)
    suspect = {b: v for b, v in intermod["bands"].items()
               if v["suspect"] or v["unexplained"]}
    if suspect and not args.allow_suspect_intermod:
        print("refusing to submit: " + ", ".join(
            f"{b} has {v['suspect']} suspect and {v['unexplained']} "
            f"too-steep channel(s)" for b, v in sorted(suspect.items())),
            file=sys.stderr)
        print("Products read as carriers, so occupancy in those bands may "
              "be inflated -- the direction that flatters the model this "
              "feeds. Fit the filter, or pass --allow-suspect-intermod to "
              "send it anyway.", file=sys.stderr)
        return 3

    bands = None
    if args.bands:
        bands = {b.strip() for b in args.bands.split(",") if b.strip()}

    body = build_body(prediction, measured, datasets, bands,
                      intermod_checked=intermod["supplied"])

    problems = check(body) + disclosure_problems(body)
    size = body_size(body)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(body, indent=1, allow_nan=False),
                        encoding="utf-8")

    print(f"wrote {out_path}")
    print(f"  schema          {body['schema']}")
    print(f"  prediction      {body['prediction_schema']}  "
          f"engine {body['model']['engine']}")
    print(f"  location        "
          f"{body['acquisition']['site']['grid_square'] or 'withheld'} "
          f"({body['acquisition']['site']['location_precision']})")
    print(f"  predicted rows  {len(body['predicted'])}")
    print(f"  measured rows   {len(body['measured'])}")
    print(f"  hours           {body['acquisition']['hours']}")
    print(f"  intermod        "
          f"{'checked' if body['acquisition']['intermod_checked'] else 'NOT EVIDENCED'}")
    print(f"  size            {size['json_bytes']/1e6:.2f} MB json, "
          f"{size['gzip_bytes']/1e6:.3f} MB gzipped "
          f"(limit {MAX_GZIP_BYTES/1e6:.1f})")

    if problems:
        print("\nREFUSING TO SEND:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    if not args.send:
        print("\nDry run. Read the file above, then add --send.")
        return 0

    token = os.environ.get("SUMMITRFI_TOKEN")
    if not token:
        print("SUMMITRFI_TOKEN is not set. The token comes from the "
              "environment only -- not an argument, which lands in shell "
              "history, and not a query string, which nginx logs.",
              file=sys.stderr)
        return 4

    status, text = post(body, token, args.endpoint)
    print(f"\n{args.endpoint} -> {status}")
    print(text[:2000])
    return 0 if 200 <= status < 300 else 5


if __name__ == "__main__":
    sys.exit(main())
