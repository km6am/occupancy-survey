"""Merge interval files from one or many sites into service-level figures.

This is the output the survey exists for: "VHF business-pool channels around
here are busy X% of the time" -- the measured input duty_cycles.md lacks.

Reports BOTH service-level averages, because conflating them would make
every measured duty cycle look an order of magnitude below the standard it
replaces: `mean_channel_occupancy` averages over all grid channels including
the unlicensed-and-empty ones, while `active_channel_occupancy` averages
only over channels that were busy at all and is the one comparable with
TIA-603 figures for a channel in use.

`--threshold-db` re-derives everything at a different threshold from the
stored histograms, with no re-acquisition. That is the point of storing them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from occupancy_survey.channels import continuity_for
    from occupancy_survey.dataset import (filter_by_local_hour,
                                          merge_channels,
                                          occupancy_by_local_hour,
                                          read_dataset, service_occupancy)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("datasets", nargs="+", help="occ_*.json.gz files")
    ap.add_argument("--threshold-db", type=float,
                    help="re-threshold the stored histograms instead of "
                         "using each file's default")
    ap.add_argument("--min-decided", type=int, default=100,
                    help="skip channels with fewer decided samples")
    ap.add_argument("--band", help="also list per-channel detail for a band")
    ap.add_argument("--hours", metavar="START-END",
                    help="keep only intervals whose LOCAL start hour is in "
                         "this window, e.g. 08-18 for daytime. Land mobile "
                         "is strongly diurnal, so a figure pooled over the "
                         "whole clock understates what a daylight "
                         "operation meets.")
    ap.add_argument("--by-hour", metavar="BAND",
                    help="tabulate active-channel occupancy per local hour "
                         "for this band")
    ap.add_argument("--json-out", help="write the summary as JSON")
    args = ap.parse_args(argv)

    datasets = [read_dataset(p) for p in args.datasets]
    synthetic = sum(1 for d in datasets
                    if d["acquisition"]["receiver"].get("synthetic"))
    if synthetic:
        print(f"WARNING: {synthetic} of {len(datasets)} datasets are "
              f"synthetic. These are not measurements of anything.",
              file=sys.stderr)

    all_datasets = datasets
    hour_window = None
    if args.hours:
        try:
            a, b = (float(x) for x in args.hours.replace("to", "-").split("-"))
        except ValueError:
            print(f"--hours wants START-END, e.g. 08-18 (got {args.hours!r})",
                  file=sys.stderr)
            return 2
        hour_window = (a, b)
        datasets = filter_by_local_hour(datasets, a, b)
        dropped = len(all_datasets) - len(datasets)
        if not datasets:
            print(f"no intervals fall in local hours {a:g}-{b:g}; "
                  f"{dropped} excluded", file=sys.stderr)
            return 1

    summary = service_occupancy(datasets, args.threshold_db,
                               args.min_decided)

    sites = sorted({d["acquisition"]["site"]["site_id"] for d in datasets})
    print(f"{len(datasets)} interval file(s) from {len(sites)} site(s): "
          f"{', '.join(sites)}")
    if hour_window:
        a, b = hour_window
        print(f"local hours {a:g}:00-{b:g}:00 only "
              f"({len(all_datasets) - len(datasets)} interval(s) excluded)")
    else:
        hours = sorted({int(d["interval"]["local_hour"]) for d in datasets
                        if d.get("interval", {}).get("local_hour") is not None})
        if hours and (max(hours) - min(hours)) > 8:
            print(f"NOTE: these intervals span local hours {min(hours)}-"
                  f"{max(hours)}. Land mobile is strongly diurnal, so this "
                  f"pooled figure is\n      diluted by the quiet hours. "
                  f"Use --hours 08-18 for a daytime number.")
    thr = args.threshold_db
    print(f"threshold: {'per-file default' if thr is None else f'{thr:.1f} dB (re-derived from stored histograms)'}")
    print()

    for group, label in (("targets", "TARGET BANDS"),
                         ("controls", "CONTROL BANDS")):
        if not summary[group]:
            continue
        print(label)
        if group == "controls":
            print("  (known-continuous by architecture -- these calibrate "
                  "the threshold)")
        print(f"  {'band':<14} {'chans':>6} {'active':>7} "
              f"{'mean occ':>9} {'active occ':>11} {'max':>7}")
        for name, e in summary[group].items():
            def pct(x):
                return "   n/a" if x is None else f"{100*x:6.2f}%"
            print(f"  {name:<14} {e['channels_considered']:6d} "
                  f"{e['channels_active']:7d} {pct(e['mean_channel_occupancy']):>9} "
                  f"{pct(e['active_channel_occupancy']):>11} "
                  f"{pct(e['max_channel_occupancy']):>7}")
            if not e["comparable_across_channel_widths"]:
                print("        note: pooled across differing channel "
                      "widths; excess-dB zero points differ (~3 dB)")
            for check in e.get("control_checks") or []:
                if check and check["verdict"] != "pass":
                    print(f"        CONTROL {check['verdict'].upper()}: "
                          f"target numbers from this run inherit the fault")
        print()

    print("mean_channel_occupancy  = over ALL grid channels, most of which "
          "have no licensee.")
    print("                          Use this for 'how often is a random "
          "channel busy'.")
    print("active_channel_occupancy= over channels busy at all. Compare "
          "THIS with the")
    print("                          TIA-603 figures in duty_cycles.md, "
          "which describe a")
    print("                          channel in use, not one picked off a "
          "grid.")

    if args.band:
        print()
        print(f"PER-CHANNEL DETAIL: {args.band}")
        merged = merge_channels(datasets, args.band)
        default_thr = datasets[0]["acquisition"]["detection"]["threshold_db"]
        use = thr if thr is not None else default_thr
        rows = [(m, m.occupancy_at(use)) for m in merged
                if m.n_decided >= args.min_decided]
        rows = [(m, o) for m, o in rows if o and o > 0.0]
        rows.sort(key=lambda r: r[1], reverse=True)
        if not rows:
            print("  no channel in this band was ever active")
        for m, occ in rows[:40]:
            cont = continuity_for(m.service).continuity
            print(f"  {m.freq_hz/1e6:10.4f} MHz  {100*occ:6.2f}%  "
                  f"n={m.n_decided:7d}  {cont:<12} "
                  f"sites={','.join(sorted(m.sites))}")

    if args.by_hour:
        out = occupancy_by_local_hour(all_datasets, args.by_hour,
                                      args.threshold_db, args.min_decided)
        print()
        print(f"ACTIVE-CHANNEL OCCUPANCY BY LOCAL HOUR: {args.by_hour}")
        if not out["hours"]:
            print("  no intervals carry a local hour (pre-2026-10-29 "
                  "datasets did not record one)")
        for h, e in out["hours"].items():
            bar = "#" * int(40 * e["mean_active_occupancy"])
            print(f"  {int(h):02d}:00  {100*e['mean_active_occupancy']:6.2f}% "
                  f"(n={e['n_channel_intervals']:5d})  {bar}")
        print("  A 24-hour mean is not what a daylight operation meets.")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(summary, indent=1),
                                       encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
