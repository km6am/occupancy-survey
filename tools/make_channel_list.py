"""Turn a QTH prediction into a targeted channel list, with summit tags.

WHY A TARGETED LIST -- and an airtime claim that did not survive contact
with the data.

I expected targeting to cut the cycle by an order of magnitude, on the
reasoning that licensed channels cluster into a few windows while a grid
sweep spends its airtime on empty spectrum. MEASURED AT THIS QTH
(CM87, 188 selected channels), it does not:

    grid,     RSPdx @ 6 MHz   2494 channels  14 steps  287 s  P(1%)=0.62
    targeted, RSPdx @ 6 MHz    188 channels  10 steps  205 s  P(1%)=0.74

A 30% saving, not 10x. The reason is visible per band: FCC-licensed
channels are SPREAD ACROSS their allocations rather than clustered, so the
tuning windows still have to cover nearly the same span. The targeted VHF
set spans 4.99 MHz of a 5.2 MHz band and needs the same two tune steps;
UHF spans 18.7 of 20 MHz and saves one step out of five. Only broadcast FM
saves materially (5 steps to 2), because only seven stations are predicted
audible.

So the real benefits are the other ones, and they are still worth having:

  - 188 channels instead of 2494: interval files about 13x smaller, and a
    per-channel output a human can read.
  - EXACT FREQUENCIES. This is a measurement improvement, not a
    convenience: a carrier offset from the uniform grid is split across two
    channels and both read low. FCC frequencies are exact, so that whole
    error disappears.
  - service code, hence continuity class, per channel by construction.
  - a volunteer never runs sota-rf -- they receive a small JSON beside
    their config. No FCC download, no pandas, no geo stack.
  - a modest airtime gain, which is still a real improvement to every
    catch probability in docs/sampling-design.md.

Targeting WOULD cut airtime sharply at a QTH whose licensed channels
happen to cluster, or if the band set is narrowed -- dropping uhf_lmr and
bcast_fm from the schedule saves far more than targeting does. Check with
`--plan` rather than assuming either way.

WHAT IT FORECLOSES, and the mitigation. Measuring only licensed
frequencies makes "active but unpredicted" structurally impossible. That
was one of the four validation quadrants and the one that catches both
unlicensed activity and stale licence records, so the cross-check drops
from two-way to one-way. Keep a grid band in the schedule alongside the
targeted ones -- the scheduler handles mixed band types -- and spend a
slice of the airtime on discovery. `--keep-grid-band` writes the config
stanza for that.

TAG, DO NOT FILTER, on summits. Duty cycle transfers between locations;
field strength does not. That asymmetry is what makes this project work: an
18% dispatch channel heard from the valley is 18% for an activator beside
the transmitter too, because the path changes how LOUD, not how OFTEN. So
filtering the target list down to summit-co-sited emitters buys nothing the
risk model needs -- duty_cycles.md's table is keyed by SERVICE CODE, not by
callsign, so any audible IG emitter informs the IG row -- while shrinking
the sample for every row.

The one hypothesis that would justify filtering is that summit-co-sited
emitters are systematically busier than valley ones: a mountaintop repeater
serving a county versus a local business base station. That is testable,
and filtering now would bake it in untested. So every channel carries a
`near_summit` tag instead, and the two distributions can be compared once
there is data. If they agree, pool and keep the larger sample; if they
diverge, that is a finding worth more than the filtered measurement.

The tag is a join on sota-rf's `rf_ref` against its per-summit output
(e.g. ~/sota-rf/W6_rf_sources.csv), not a second expensive run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from occupancy_survey import disclosure as _disclosure

SCHEMA = "occupancy-survey/channel-list/1"
DEFAULT_SUMMIT_CSV = Path(
    os.environ.get("SOTA_RF_DIR", Path.home() / "sota-rf")
).expanduser() / "W6_rf_sources.csv"


def load_summit_emitters(path: Path) -> dict[str, list[dict]]:
    """Map rf_ref -> the summits that emitter sits near.

    Keyed on rf_ref because it is sota-rf's own emitter identity and is
    populated for every row of its output; matching on rounded lat/lon
    would merge co-sited emitters that happen to share a mast.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        for rec in csv.DictReader(f):
            ref = (rec.get("rf_ref") or "").strip()
            if not ref:
                continue
            try:
                dist = float(rec.get("distance_m") or "nan")
            except ValueError:
                dist = float("nan")
            out[ref].append({
                "summit": (rec.get("summit") or "").strip(),
                "summit_name": (rec.get("summit_name") or "").strip(),
                "distance_m": dist,
            })
    return out


def tag_and_select(prediction: dict, summit_emitters: dict,
                   top_per_band: int, min_proxy: float | None):
    """Tag every predicted channel, then select what to measure."""
    by_band: dict[str, list[dict]] = defaultdict(list)
    for ch in prediction["channels"]:
        near = summit_emitters.get(str(ch.get("rf_ref") or "").strip(), [])
        tagged = dict(ch)
        if near:
            best = min(near, key=lambda s: (s["distance_m"]
                                            if s["distance_m"] == s["distance_m"]
                                            else 1e12))
            tagged["near_summit"] = {
                "summit": best["summit"],
                "summit_name": best["summit_name"],
                "distance_m": best["distance_m"],
                "n_summits": len(near),
            }
        else:
            tagged["near_summit"] = None
        by_band[ch["band"]].append(tagged)

    selected: dict[str, list[dict]] = {}
    for band, rows in by_band.items():
        rows.sort(key=lambda r: (r["field_proxy_w_per_m2"] or -1.0),
                  reverse=True)
        keep, seen = [], set()
        for r in rows:
            if r["freq_hz"] in seen:
                continue
            proxy = r["field_proxy_w_per_m2"]
            # Controls are kept whatever their proxy -- the run needs them
            # even if the prediction thinks they are marginal, because a
            # control that is dropped for being weak cannot calibrate
            # anything.
            is_control = r["band_role"] == "control"
            over_floor = min_proxy is None or (proxy or 0.0) >= min_proxy
            if is_control or (over_floor and len(keep) < top_per_band):
                keep.append(r)
                seen.add(r["freq_hz"])
        selected[band] = keep
    return selected


def narrow_controls(selected: dict, plans: dict, span_mhz: float) -> dict:
    """Keep each control band to one window around its strongest station.

    A control answers "is the receive path and threshold working", and a
    few strong stations in one tuning answer it as well as eighty-five
    across five. The rest of the FM band is airtime taken from the
    land-mobile channels. The window is centred on the strongest predicted
    station, so the control keeps the one most certain to be heard.
    """
    out = dict(selected)
    half = 0.5 * span_mhz * 1e6
    for band, rows in selected.items():
        plan = plans.get(band)
        if plan is None or plan.role != "control" or not rows:
            continue
        top = max(rows, key=lambda r: r["field_proxy_w_per_m2"] or -1.0)
        out[band] = [r for r in rows
                     if abs(r["freq_hz"] - top["freq_hz"]) <= half]
    return out


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from occupancy_survey.channels import BAND_PLANS

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prediction", required=True)
    ap.add_argument("--summit-csv", default=str(DEFAULT_SUMMIT_CSV),
                    help="sota-rf per-summit output used for the near_summit "
                         "tag (read-only)")
    ap.add_argument("--top-per-band", type=int, default=60,
                    help="strongest N predicted channels per band")
    ap.add_argument("--min-proxy", type=float, default=None,
                    help="drop channels below this ERP/d^2 (uncalibrated)")
    ap.add_argument("--control-span-mhz", type=float, default=None,
                    help="keep control bands (broadcast FM) to this span "
                         "around the strongest station, e.g. 2.4 for one "
                         "tune step. Default: every predicted station")
    ap.add_argument("--out", default="./out/channels.json")
    args = ap.parse_args(argv)

    prediction = json.loads(Path(args.prediction).read_text(encoding="utf-8"))
    summit_path = Path(args.summit_csv)
    summit_emitters = load_summit_emitters(summit_path)
    if not summit_emitters:
        print(f"WARNING: no summit emitters loaded from {summit_path}; "
              f"near_summit will be null for every channel", file=sys.stderr)

    selected = tag_and_select(prediction, summit_emitters,
                              args.top_per_band, args.min_proxy)
    if args.control_span_mhz is not None:
        selected = narrow_controls(selected, BAND_PLANS,
                                   args.control_span_mhz)

    bands_out = {}
    for band_name, rows in sorted(selected.items()):
        if band_name not in BAND_PLANS or not rows:
            continue
        plan = BAND_PLANS[band_name]
        bands_out[band_name] = {
            "role": plan.role,
            "service": plan.service,
            "channel_bandwidth_hz": plan.channel_bandwidth_hz,
            "expected_occupancy": plan.expected_occupancy,
            "channels": [
                {"freq_hz": r["freq_hz"], "service": r["service"],
                 "continuity": r["continuity"],
                 "expected_duty": r["expected_duty"],
                 "owner": r["owner"],
                 "field_proxy_w_per_m2": r["field_proxy_w_per_m2"],
                 "distance_m": r["distance_m"],
                 "near_summit": r["near_summit"]}
                for r in rows
            ],
        }

    # REDACTED BY CONSTRUCTION. This file is handed to volunteers, and an
    # earlier version copied the prediction's `query` block through intact
    # -- which carries the generator's exact latitude and longitude -- plus
    # absolute paths holding the generator's account name. Measured
    # 2026-11-01. Coordinates become a 4-character grid square (about
    # 100 x 160 km) and paths become basenames.
    #
    # The channel list still narrows the region: it is the set of licensed
    # channels near one point, so a reader with the FCC database can
    # intersect them. That is not fixable while the list is targeted at
    # all, so it is stated here rather than implied away.
    query = dict(prediction.get("query") or {})
    try:
        where = _disclosure.grid_square(float(query["lat"]),
                                        float(query["lon"]))
    except (KeyError, TypeError, ValueError):
        where = None
    generated_from = {
        "prediction": Path(str(args.prediction)).name,
        "summit_csv": Path(str(summit_path)).name if summit_path else None,
        "top_per_band": args.top_per_band,
        "min_proxy": args.min_proxy,
        "control_span_mhz": args.control_span_mhz,
        "query": {
            "grid_square": where,
            "location_precision": ("maidenhead 4-character"
                                   if where else "withheld"),
            "radius_m": query.get("radius_m"),
            "bands": query.get("bands"),
        },
        "disclosure_note": (
            "Coordinates are reduced to a 4-character grid square on "
            "purpose. The channel set itself still indicates a region; see "
            "occupancy_survey/disclosure.py."),
    }

    payload = {
        "schema": SCHEMA,
        "generated_from": generated_from,
        "caveats": {
            "forecloses_discovery": (
                "A targeted list cannot find a busy channel with no FCC "
                "record -- one of the four validation quadrants. Keep a "
                "grid band in the schedule alongside these."),
            "summit_tag": (
                "near_summit is a TAG, not a filter. Duty cycle transfers "
                "between locations and field strength does not, so an "
                "audible emitter informs its service's duty cycle wherever "
                "it sits. The tag exists to test whether summit-co-sited "
                "emitters are systematically busier, which would otherwise "
                "be assumed."),
            "field_proxy": (
                "ERP/d^2, uncalibrated; an ordering, not a level."),
        },
        "bands": bands_out,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    total = sum(len(b["channels"]) for b in bands_out.values())
    tagged = sum(1 for b in bands_out.values() for c in b["channels"]
                 if c["near_summit"])
    print(f"{total} channels -> {out}")
    print(f"  near a summit: {tagged}   elsewhere: {total - tagged}")
    for name, b in bands_out.items():
        n_sum = sum(1 for c in b["channels"] if c["near_summit"])
        print(f"  {name:<14} {b['role']:<8} {len(b['channels']):4d} channels"
              f"  ({n_sum} near a summit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
