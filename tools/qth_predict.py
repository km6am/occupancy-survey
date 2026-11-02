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

THREE ENGINES, and the one a volunteer uses needs none of the above.

  --engine report   reads a location report the operator DOWNLOADED FROM THE
                    MAP UI ("Report for a location" -> "Download JSON"). No
                    sota-rf checkout, no Node, no FCC cache: the report is
                    computed in the browser and the file is the whole input.
                    This is the documented volunteer path (sota-rf
                    docs/volunteer/location-report-howto.html) and the only
                    one available on a machine without sota-rf.
  --engine qth-cli  runs web/map/qth_cli.mjs locally. The same module as the
                    page, so the same object -- but it lives on sota-rf's
                    unmerged qth-rfi-report branch and is not served.
  --engine pipeline the above pseudo-summit run: sota_rf_sources.py, no
                    terrain, ERP/d^2, the lowest 40 channels per site.

report and qth-cli both write occupancy-survey/qth-prediction/2: SRTM
terrain diffraction, the FM vertical pattern, the full channel list and the
model's own dBm with an error band. pipeline writes /1. All three emit
field_proxy_w_per_m2 with the same meaning and units, so one validator reads
any of them; a /2 file additionally carries p_dbm_ref and path, which is what
makes the audibility floor frequency-aware and the path residual a
discriminator rather than a restatement.

The two /2 engines differ in ONE field that matters: model.engine. The page
stamps its deploy hash, the CLI stamps node-<sha1 of the model sources>.
Different namespaces for the same model, so predictions from the two must not
be pooled in one validation even when the model is identical (CAVEATS_V2
engine_mixing); split by engine.

pipeline is still the default because it is the engine that works with only
sota-rf's mainline. --engine report is what a survey volunteer should use.
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

# A different program with different dependencies: the pipeline needs Python
# and the FCC cache, the location report needs Node >= 18 and the map's
# exported transmitter tiles. Kept as its own override so a host can have
# one engine without the other.
QTH_CLI = Path(os.environ.get(
    "SOTA_RF_QTH_CLI", SOTA_RF_DIR / "web" / "map" / "qth_cli.mjs")
).expanduser()

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


def run_qth_cli(out_json: Path, lat: float, lon: float, ant_agl_m: float,
                fmin_mhz: float, fmax_mhz: float, min_dbm=None,
                data=None, dem_2s=None, ref_gain_dbi=None,
                timeout_s: float = 3600.0, cli: Path | None = None) -> Path:
    """Run sota-rf's location report read-only and return its JSON.

    READ-ONLY BY CONSTRUCTION, like run_sota_rf: the CLI reads the map's
    exported tiles and writes its report to --out, which is a path in our
    scratch directory. Nothing in the sota-rf tree is written.
    """
    cli = Path(cli or QTH_CLI)
    if not cli.exists():
        raise FileNotFoundError(
            f"{cli} not found. The location report lives on sota-rf branch "
            f"qth-rfi-report; set SOTA_RF_QTH_CLI or --qth-cli to its "
            f"web/map/qth_cli.mjs, or use --engine pipeline.")

    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["node", str(cli.resolve()),
           "--lat", f"{lat:.6f}", "--lon", f"{lon:.6f}",
           "--ant-agl", f"{ant_agl_m:g}",
           "--fmin", f"{fmin_mhz:.6f}", "--fmax", f"{fmax_mhz:.6f}",
           "--out", str(out_json.resolve())]
    if min_dbm is not None:
        cmd += ["--min-dbm", f"{min_dbm:g}"]
    if ref_gain_dbi is not None:
        cmd += ["--gain", f"{ref_gain_dbi:g}"]
    if data:
        cmd += ["--data", str(data)]
    if dem_2s:
        cmd += ["--dem-2s", str(Path(dem_2s).expanduser().resolve())]

    print("running:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(
            f"qth_cli.mjs exited {proc.returncode}. Exit 1 usually means the "
            f"point is outside the transmitter data; exit 2 means the query "
            f"was rejected.")
    if not out_json.exists():
        raise FileNotFoundError(f"qth_cli.mjs produced no {out_json}")
    return out_json


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


def _band_spans(bands):
    return [(b.name, b.freq_min_hz, b.freq_max_hz, b.role) for b in bands]


def _band_of(spans, hz: float):
    """(name, role) of the first band holding hz, or None."""
    return next(((n, role) for n, lo, hi, role in spans if lo <= hz <= hi),
                None)


def parse_emitters(csv_path: Path, bands) -> list[dict]:
    """Per-emitter predictions, restricted to the surveyed bands.

    One licence record can list several frequencies; each becomes its own
    predicted channel, because occupancy is measured per channel.
    """
    from occupancy_survey.channels import continuity_for

    spans = _band_spans(bands)
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
                band = _band_of(spans, hz)
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


def _num(v):
    """float, or None for blank, non-numeric, NaN and inf alike."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_qth_report(json_path: Path, bands) -> list[dict]:
    """Per-channel predictions from a sota-rf/qth-rfi location report.

    Returns the same row shape parse_emitters does -- same keys, same units
    -- plus the fields only the report has. Downstream therefore needs no
    change to read a /2 file, and the /2-only fields are additive.
    """
    from occupancy_survey.channels import continuity_for

    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    schema = str(data.get("schema", ""))
    if not schema.startswith("sota-rf/qth-rfi"):
        raise SystemExit(
            f"{json_path}: schema is {schema!r}, not a sota-rf/qth-rfi "
            f"location report. --engine qth-cli reads qth_cli.mjs output; a "
            f"pipeline CSV goes to --engine pipeline.")

    report_rows = data.get("channels") or []
    if report_rows:
        keys: set[str] = set()
        for rec in report_rows:
            keys.update(rec.keys())
        missing = _reference.missing_qth_rfi_fields(keys)
        if missing:
            raise SystemExit(
                f"{json_path}: location report is missing required field(s) "
                f"{', '.join(missing)}. Without p_dbm_ref or path this is a "
                f"/1 prediction wearing a 2: it would validate, and the "
                f"audibility floor and the path residual drawn from it would "
                f"be unsupported. Update QTH_RFI_REQUIRED_FIELDS in "
                f"occupancy_survey/reference/ if the report has genuinely "
                f"changed shape, rather than reading around the gap.")
    # An empty channel list is a legitimate answer -- nothing within the cap
    # above the floor -- so it is NOT put through the field check, which
    # would otherwise report every required field as missing.

    spans = _band_spans(bands)
    rows, n_blank, n_out_of_band = [], 0, 0
    for rec in report_rows:
        mhz = _num(rec.get("freq_mhz"))
        if mhz is None:
            # The report lists map-rule sources with no frequency on record
            # so its totals stay reconcilable. They have no channel to
            # measure, so they are dropped here, as in the /1 path.
            n_blank += 1
            continue
        hz = mhz * 1e6
        band = _band_of(spans, hz)
        if band is None:
            n_out_of_band += 1
            continue

        field_vm = _num(rec.get("field_vm"))
        proxy = None
        if field_vm is not None:
            # field_vm**2 / 30 reproduces /1's ERP/d^2 exactly when gain is
            # unity and loss zero, because sota-rf builds E as
            # sqrt(30 * ERP * g * 10^(-L/10)) / d. That is the point: the
            # two engines stay commensurable and one validator reads both.
            # It is NOT a power density -- a true one would be E^2 / 377,
            # and ERP/d^2 is itself 4*pi larger than ERP/(4*pi*d^2) -- so
            # the "_w_per_m2" in the key name is wrong by a constant in
            # BOTH schemas. Harmless for an ordering, and left alone
            # because every reader downstream is keyed on the name.
            proxy = field_vm * field_vm / 30.0

        service = (rec.get("service") or "").strip()
        cont = continuity_for(service)
        rows.append({
            # --- every key /1 emits, same meaning and units --------------
            "freq_hz": hz,
            "band": band[0],
            "band_role": band[1],
            "rf_ref": (rec.get("ref") or "").strip(),
            "rf_lat": rec.get("lat"),
            "rf_lon": rec.get("lon"),
            "owner": (rec.get("owner") or "").strip()[:60],
            "service": service,
            "continuity": cont.continuity,
            "expected_duty": cont.expected_duty,
            "continuity_basis": cont.basis,
            "distance_m": _num(rec.get("distance_m")),
            "erp_w": _num(rec.get("erp_w")),
            "field_proxy_w_per_m2": proxy,
            # --- /2 only -------------------------------------------------
            "callsign": (rec.get("call") or "").strip(),
            "erp_basis": rec.get("erp_basis") or None,
            "field_vm": field_vm,
            "p_dbm_ref": _num(rec.get("p_dbm_ref")),
            "error_db": _num(rec.get("error_db")),
            "error_basis": rec.get("error_basis") or None,
            "path": rec.get("path") or None,
            "diffraction_db": _num(rec.get("diffraction_db")),
            # The report's rank is over the WHOLE queried span before this
            # tool filters to the survey bands, so it is not 1..N here and
            # must not be presented as this file's ordering. Named apart
            # from "rank" for that reason.
            "report_rank": rec.get("rank"),
            "in_totals": rec.get("in_totals"),
        })

    # Deliberately not carried: bearing_deg and fspl_db. Nothing downstream
    # reads either, and bearing plus distance to a known emitter is the pair
    # that trilaterates the receiver. Minimum disclosure applies to local
    # files too, because a local file is one upload away from being shared.

    rows.sort(key=lambda r: (r["field_proxy_w_per_m2"] or -1.0), reverse=True)
    print(f"report: {len(report_rows)} channels, {len(rows)} in the "
          f"surveyed bands ({n_out_of_band} out of band, {n_blank} with no "
          f"frequency on record)", file=sys.stderr)
    return rows


CAVEATS_V1 = {
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
}

CAVEATS_V2 = {
    "field_proxy": "field_vm^2 / 30 -- the same quantity and units as /1, "
                   "so the two engines are commensurable, but now with "
                   "SRTM terrain diffraction and the broadcast-FM vertical "
                   "pattern in it. Still an ordering and still an upper "
                   "bound: licensed maximum ERP per site, no clutter. The "
                   "key name's units are wrong by a constant in both "
                   "schemas and are kept only for compatibility.",
    "p_dbm_ref": "The MODEL's received power at the stated reference gain, "
                 "with error_db beside it and error_basis saying how that "
                 "band was arrived at. It is a prediction, not a "
                 "measurement, and it is never written into an "
                 "occupancy-survey/1 dataset.",
    "errors_share_a_sign": "Licensed-maximum ERP applied to every channel, "
                           "no clutter, and a single knife edge at low "
                           "antenna heights all push predictions UP. "
                           "'Predicted strong, never active' will therefore "
                           "be common and mostly model error -- not "
                           "evidence of a deaf receive path.",
    "engine_mixing": "model.engine is a content hash of sota-rf's model "
                     "code. A model change changes every prediction, so "
                     "predictions from two engines must not be pooled in "
                     "one validation; split by engine or refuse.",
    "report_rank": "report_rank is the report's rank over the whole queried "
                   "span, before the filter to the surveyed bands. It is "
                   "not this file's ordering and is not 1..N.",
    "stays_local": "Rows carry distance_m and emitter coordinates. Two rows "
                   "of those trilaterate the receiver, so this file locates "
                   "the station. It must not be shared, published or "
                   "attached; the submit path's field allowlist is the only "
                   "safe exit.",
    "not_a_summit": "This is the prediction for one fixed location "
                    "with one antenna. It is not a summit field "
                    "strength and the two must not be conflated.",
    "continuity": "Continuity classes are architectural, from "
                  "duty_cycles.md section 2. Nothing in them has "
                  "been measured; that is what the survey is for.",
}


def write_prediction(rows, path: Path, meta: dict,
                     schema: str = _reference.PREDICTION_SCHEMA,
                     source: str = "sota-rf (read-only)",
                     caveats: dict | None = None,
                     model: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": schema,
        "source": source,
        "caveats": CAVEATS_V1 if caveats is None else caveats,
        "query": meta,
        "n_predicted_channels": len(rows),
        "channels": rows,
    }
    if model is not None:
        payload["model"] = model
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def _resolve_ant_agl(args) -> float:
    """Receive antenna height above ground, never defaulted.

    It moves every diffraction loss in the model, so a wrong value does not
    degrade the prediction gracefully -- it changes which channels are
    listed at all. There is no safe default, so this raises instead.
    """
    if args.ant_agl is not None:
        return float(args.ant_agl)
    if args.config:
        from occupancy_survey.config import AppConfig
        height = AppConfig.from_yaml(args.config).site.antenna_height_m
        if height is None:
            raise SystemExit(
                f"{args.config} has no site.antenna_height_m. Set it there "
                f"or pass --ant-agl METRES; the model will not be given a "
                f"guessed antenna height.")
        print(f"antenna height {height} m above ground, from {args.config}",
              file=sys.stderr)
        return float(height)
    raise SystemExit(
        "--engine qth-cli needs the receive antenna height above ground. It "
        "moves every diffraction loss in the model, so there is no safe "
        "default. Pass --ant-agl METRES, or --config CONFIG.YAML with "
        "site.antenna_height_m set.")


def read_report_query(report_path: Path, args) -> tuple[dict, dict]:
    """The query block of a downloaded location report, cross-checked.

    A UI download already records the point, the antenna height, the gain,
    the span and the channel floor it was built with, so this takes them
    from the FILE rather than the command line. Retyping them is how you get
    a prediction whose header describes a different station than its rows:
    every level in the file belongs to the point the browser traced.

    --lat/--lon/--ant-agl may still be given, as a check. A disagreement is
    refused rather than resolved, because either value could be the right
    one and guessing silently mislabels the prediction.
    """
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    schema = str(data.get("schema", ""))
    if not schema.startswith("sota-rf/qth-rfi"):
        raise SystemExit(
            f"{report_path}: schema is {schema!r}, not a sota-rf/qth-rfi "
            f"location report. --engine report reads the JSON the map UI's "
            f"\"Download JSON\" button saves (named like "
            f"qth-rfi-YYYY-MM-DD.json).")
    query = data.get("query") or {}
    for key in ("lat", "lon"):
        if query.get(key) is None:
            raise SystemExit(
                f"{report_path}: query.{key} is missing, so the report does "
                f"not say where it was built. Rebuild it in the UI.")
    if query.get("ant_agl_m") is None:
        raise SystemExit(
            f"{report_path}: query.ant_agl_m is missing. The antenna height "
            f"moves every diffraction loss in the model and the feedback "
            f"endpoint requires it, so the report cannot be used without "
            f"it. Rebuild it in the UI with the height filled in.")

    # 1e-5 degrees is about 1.1 m: tighter than any real disagreement, loose
    # enough for the rounding a typed-in coordinate picks up.
    for name, given, found in (("--lat", args.lat, query["lat"]),
                               ("--lon", args.lon, query["lon"]),
                               ("--ant-agl", args.ant_agl,
                                query["ant_agl_m"])):
        if given is None:
            continue
        tol = 1e-5 if name != "--ant-agl" else 1e-6
        if abs(float(given) - float(found)) > tol:
            raise SystemExit(
                f"{name} is {given}, but {report_path} was built at "
                f"{found}. The report's rows are levels at ITS point, so "
                f"one of the two is the wrong station. Drop {name} to use "
                f"the report's own value, or rebuild the report.")
    return data, query


def report_provenance(data: dict, rows: list[dict]) -> dict:
    """What the ingested report was built from, measured off the file.

    The live map's exported names JSON has no ref/call column (sota-rf F1,
    not yet re-exported), and a report built from it carries blank emitter
    references. That is not visible in the row count -- the rows are all
    there -- so it is counted here and stated, because make_channel_list.py
    joins summit tags on ref and a blank one joins to nothing.
    """
    model = data.get("model") or {}
    # rf_ref, not ref: parse_qth_report renames the report's column, and
    # reading the report's own name here would have counted every row as
    # blank and cried wolf on a complete export.
    n_blank_ref = sum(1 for r in rows
                      if not (r.get("rf_ref") or "").strip())
    note = None
    if rows and n_blank_ref == len(rows):
        note = (f"every one of {len(rows)} rows has a blank emitter ref. The "
                f"transmitter export behind this report predates sota-rf's "
                f"ref/call column, so rows cannot be joined to a callsign "
                f"or to a summit tag by ref.")
    elif n_blank_ref:
        note = f"{n_blank_ref} of {len(rows)} rows have a blank emitter ref."
    return {
        "report_generated": data.get("generated"),
        "report_data_date": model.get("data"),
        "report_engine": model.get("engine"),
        "report_dem": model.get("dem"),
        "report_coverage": model.get("coverage"),
        "report_n_channels": data.get("n_channels"),
        "report_n_below_floor": data.get("n_below_floor"),
        "emitter_refs": note,
    }


def _summarise(rows, engine: str) -> None:
    by_cont: dict[str, int] = {}
    for r in rows:
        by_cont[r["continuity"]] = by_cont.get(r["continuity"], 0) + 1
    for k, v in sorted(by_cont.items()):
        print(f"  {k:<13} {v}")

    if engine in ("qth-cli", "report"):
        paths: dict[str, int] = {}
        for r in rows:
            paths[str(r.get("path"))] = paths.get(str(r.get("path")), 0) + 1
        print("  path:        "
              + ", ".join(f"{k} {v}" for k, v in sorted(paths.items())))
        # These rows are in the REPORT's order, which sota-rf ranks by
        # field (V/m) on purpose: a matched antenna's aperture grows as
        # lambda^2, so dBm into one fixed gain puts every AM carrier above
        # the VHF/UHF channels a survey tunes. So the first row here is not
        # the strongest p_dbm_ref, and the heading must not say it is --
        # measured on a real report, where the top row read -32.4 dBm while
        # a TV carrier further down read -29.0.
        print("\ntop of the report's ranking -- by FIELD, not by dBm, so a "
              "stronger p_dbm_ref may appear below.\n(model dBm at the "
              "reference gain, an UPPER BOUND with the stated error band):")
        for r in rows[:12]:
            p_dbm = r.get("p_dbm_ref")
            err = r.get("error_db")
            print(f"  {r['freq_hz']/1e6:10.4f} MHz  {r['continuity']:<12} "
                  f"{'' if p_dbm is None else f'{p_dbm:7.1f} dBm':>11}"
                  f"{'' if err is None else f' +/-{err:.0f}':<6} "
                  f"{str(r.get('path') or ''):<10} "
                  f"{r['service']:<6} {r['owner'][:28]}")
        return

    print("\nstrongest predicted (uncalibrated ERP/d^2 ordering only):")
    for r in rows[:12]:
        proxy = r["field_proxy_w_per_m2"]
        print(f"  {r['freq_hz']/1e6:10.4f} MHz  {r['continuity']:<12} "
              f"{'' if proxy is None else f'{proxy:.2e}':>10}  "
              f"{r['service']:<6} {r['owner'][:34]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Predict what should be audible at a fixed location, "
                    "using sota-rf read-only.")
    ap.add_argument("--engine", choices=("pipeline", "qth-cli", "report"),
                    default="pipeline",
                    help="pipeline: sota_rf_sources.py, ERP/d^2, no "
                         "terrain, lowest 40 channels per site, writes "
                         "qth-prediction/1 (default, because it is the "
                         "engine that works with sota-rf's mainline). "
                         "report: ingest a location report downloaded from "
                         "the map UI -- no sota-rf checkout needed, and the "
                         "volunteer path. qth-cli: run that report locally. "
                         "Both of the latter write qth-prediction/2, with "
                         "terrain, the FM pattern, the full channel list "
                         "and model dBm.")
    ap.add_argument("--lat", type=float,
                    help="required for --engine pipeline and qth-cli; for "
                         "--engine report it is read from the file and "
                         "giving it here only cross-checks it")
    ap.add_argument("--lon", type=float, help="see --lat")
    ap.add_argument("--bands", default="vhf_lmr_low,vhf_lmr_high,uhf_lmr,"
                                       "nwr,bcast_fm")
    ap.add_argument("--work-dir", default="./out/qth_prediction")
    ap.add_argument("--out", default="./out/qth_prediction/prediction.json")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing sota-rf output in --work-dir "
                         "instead of rerunning it")

    rep = ap.add_argument_group("--engine report")
    rep.add_argument("--report", default=None,
                     help="the location report saved by the map UI's "
                          "\"Download JSON\" button (named like "
                          "qth-rfi-YYYY-MM-DD.json). Its own query block "
                          "supplies the point, antenna height, reference "
                          "gain, span and channel floor.")

    pipe = ap.add_argument_group("--engine pipeline")
    pipe.add_argument("--alt-m", type=float, default=0.0,
                      help="pseudo-summit altitude (m). The pipeline has no "
                           "terrain, so this only labels the row.")
    pipe.add_argument("--radius", type=float, default=30000.0,
                      help="base inclusion radius in metres (default 30 "
                           "km). sota-rf also keeps anything clearing its "
                           "field-strength floor beyond this.")

    cli = ap.add_argument_group("--engine qth-cli")
    cli.add_argument("--ant-agl", type=float, default=None,
                     help="receive antenna height above ground, metres "
                          "(0-300). REQUIRED and never defaulted: it moves "
                          "every diffraction loss.")
    cli.add_argument("--config", default=None,
                     help="survey config to read site.antenna_height_m "
                          "from, when --ant-agl is not given")
    cli.add_argument("--min-dbm", type=float, default=None,
                     help="channel floor in dBm at the reference gain "
                          "(CLI default -130). Note the 120 km cap, not "
                          "this floor, is what bounds the list at Oakland: "
                          "237 of 93,324 channels fell below -130 dBm.")
    cli.add_argument("--ref-gain-dbi", type=float, default=None,
                     help="reference antenna gain for p_dbm_ref (CLI "
                          "default 0 dBi)")
    cli.add_argument("--data", default=None,
                     help="transmitter tiles: an export_browser_data.py "
                          "directory, or a URL ending in '/'. Without it "
                          "the CLI uses the live map's copy, which is older "
                          "and has no callsigns or full channel lists.")
    cli.add_argument("--dem-2s", default=None,
                     help="directory of local 2-inch SRTM tiles. Strongly "
                          "preferred: without it the CLI fetches AWS "
                          "terrain tiles for a 120 km box around the "
                          "location, which discloses the area to a third "
                          "party.")
    cli.add_argument("--allow-remote-terrain", action="store_true",
                     help="proceed without --dem-2s, fetching terrain from "
                          "AWS and disclosing the general area to it")
    cli.add_argument("--qth-cli", default=None,
                     help=f"path to qth_cli.mjs (default {QTH_CLI}, "
                          f"overridable with SOTA_RF_QTH_CLI)")
    args = ap.parse_args(argv)

    from occupancy_survey.channels import resolve_bands
    bands = resolve_bands(b.strip() for b in args.bands.split(",") if b.strip())
    work = Path(args.work_dir)

    if args.engine == "report":
        if not args.report:
            raise SystemExit(
                "--engine report needs --report FILE: the JSON the map UI's "
                "\"Download JSON\" button saved. See sota-rf "
                "docs/volunteer/location-report-howto.html for the six "
                "steps that produce it.")
        report = Path(args.report)
        data, query = read_report_query(report, args)
        rows = parse_qth_report(report, bands)
        meta = {
            # From the file, not the command line: these describe the point
            # the browser actually traced.
            "lat": query["lat"], "lon": query["lon"],
            "ant_agl_m": float(query["ant_agl_m"]),
            "min_dbm": query.get("min_dbm"),
            "ref_gain_dbi": query.get("ref_gain_dbi"),
            # The span the REPORT was built over, which the how-to tells
            # volunteers to leave blank (the whole span). Null means that,
            # and must not be replaced by the band list: the server stores
            # this as what was queried, and a narrower value would claim
            # the report had never looked outside the surveyed bands.
            "f_min_mhz": query.get("f_min_mhz"),
            "f_max_mhz": query.get("f_max_mhz"),
            "bands": [b.name for b in bands],
            "qth_rfi_report": str(report),
            "qth_rfi_schema": data.get("schema"),
            "data_source": "downloaded from the map UI",
            "terrain": (data.get("model") or {}).get("dem"),
        }
        prov = report_provenance(data, rows)
        if prov["emitter_refs"]:
            print(f"warning: {prov['emitter_refs']}", file=sys.stderr)
        meta.update(prov)
        out = write_prediction(rows, Path(args.out), meta,
                               schema=_reference.PREDICTION_SCHEMA_V2,
                               source="sota-rf location report, downloaded "
                                      "from the map UI (read-only)",
                               caveats=CAVEATS_V2,
                               model=data.get("model"))
    elif args.engine == "pipeline":
        if args.lat is None or args.lon is None:
            raise SystemExit("--engine pipeline needs --lat and --lon.")
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
        }, schema=_reference.PREDICTION_SCHEMA, caveats=CAVEATS_V1)
    else:
        if args.lat is None or args.lon is None:
            raise SystemExit("--engine qth-cli needs --lat and --lon.")
        ant_agl_m = _resolve_ant_agl(args)
        report = work / "qth_rfi_report.json"
        if not (args.reuse and report.exists()):
            if not args.dem_2s and not args.allow_remote_terrain:
                raise SystemExit(
                    "--engine qth-cli without --dem-2s fetches terrain "
                    "tiles from AWS for a 120 km box around --lat/--lon, "
                    "which tells a third party roughly where the receiver "
                    "is. Point --dem-2s at local SRTM 2-inch tiles, or pass "
                    "--allow-remote-terrain to accept that disclosure.")
            if not args.data:
                print("warning: no --data, so the CLI will read the live "
                      "map's tiles. That copy is older than a local export: "
                      "callsigns and full per-site channel lists may be "
                      "missing, which silently shortens the prediction.",
                      file=sys.stderr)
            fmin_mhz = min(b.freq_min_hz for b in bands) / 1e6
            fmax_mhz = max(b.freq_max_hz for b in bands) / 1e6
            run_qth_cli(report, args.lat, args.lon, ant_agl_m,
                        fmin_mhz, fmax_mhz, min_dbm=args.min_dbm,
                        data=args.data, dem_2s=args.dem_2s,
                        ref_gain_dbi=args.ref_gain_dbi, cli=args.qth_cli)

        rows = parse_qth_report(report, bands)
        header = json.loads(report.read_text(encoding="utf-8"))
        out = write_prediction(rows, Path(args.out), {
            "lat": args.lat, "lon": args.lon,
            "ant_agl_m": ant_agl_m,
            "min_dbm": args.min_dbm,
            "ref_gain_dbi": args.ref_gain_dbi,
            # The span actually queried, not the band list: the feedback
            # body sends these, and deriving them from the surviving rows
            # afterwards would report the narrowest band that happened to
            # have a channel in it.
            "f_min_mhz": min(b.freq_min_hz for b in bands) / 1e6,
            "f_max_mhz": max(b.freq_max_hz for b in bands) / 1e6,
            "bands": [b.name for b in bands],
            "qth_rfi_report": str(report),
            "qth_rfi_schema": header.get("schema"),
            "data_source": args.data or "live map (default)",
            "terrain": "local SRTM 2\" " + str(args.dem_2s) if args.dem_2s
                       else "AWS terrain tiles (remote)",
        }, schema=_reference.PREDICTION_SCHEMA_V2,
            source="sota-rf location report (read-only)",
            caveats=CAVEATS_V2, model=header.get("model"))

    print(f"{len(rows)} predicted channels in the surveyed bands -> {out}")
    _summarise(rows, args.engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
