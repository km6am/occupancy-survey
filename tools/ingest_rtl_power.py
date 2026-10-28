"""Turn an rtl_power CSV into an occupancy dataset, bias and all.

The zero-install path: a volunteer who can run `rtl_power` but cannot
install Python packages still produces a mergeable dataset.

READ occupancy_survey/backends/rtl_power_csv.py BEFORE TRUSTING THE OUTPUT.
rtl_power averages each bin over its whole interval before this tool can
threshold anything, so an intermittent channel tends to read 0% or 100%
rather than its duty cycle, and a transmission shorter than the interval is
attenuated by 10*log10(burst/interval) dB. Every dataset written here
carries `averaging_bias: pre-threshold` and the interval used, and
validate_occupancy.py prints a warning on any figure built from one.

Use `rtl_power -i 1s` or shorter. The default 10 s makes the output nearly
useless for occupancy.

Example:
    rtl_power -f 150.8M:156M:12.5k -i 1s -g 40 -e 1h vhf.csv
    python tools/ingest_rtl_power.py vhf.csv --site-id home-1 \
        --antenna "discone at 6 m" --gain 40 --interval-s 1 --bands vhf_lmr_low
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from occupancy_survey.backends.rtl_power_csv import (parse_rtl_power,
                                                         receiver_metadata)
    from occupancy_survey.channels import resolve_bands
    from occupancy_survey.dataset import AcquisitionMetadata, IntervalDataset
    from occupancy_survey.detector import (BandBaseline, ChannelLevelExtractor,
                                           ThresholdPolicy, decide)
    from occupancy_survey.occupancy import BandAccumulator, HistogramSpec
    from occupancy_survey.schedule import SamplingDesign, Schedule
    from occupancy_survey.site import SiteMetadata

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", help="rtl_power output")
    ap.add_argument("--site-id", required=True)
    ap.add_argument("--antenna", required=True,
                    help="in plain words; a quiet channel and a deaf "
                         "receive path produce the same number")
    ap.add_argument("--receiver-model", default="RTL-SDR")
    ap.add_argument("--grid-square", default=None,
                    help="optional, 4 or 6 characters, nothing finer")
    ap.add_argument("--gain", type=float, required=True)
    ap.add_argument("--interval-s", type=float, required=True,
                    help="the -i value rtl_power was run with. This sets "
                         "the averaging bias recorded in the dataset.")
    ap.add_argument("--bands", default="vhf_lmr_low,nwr,vhf_lmr_high")
    ap.add_argument("--threshold-db", type=float, default=10.0)
    ap.add_argument("--out-dir", default="./data")
    args = ap.parse_args(argv)

    bands = resolve_bands(b.strip() for b in args.bands.split(",") if b.strip())
    policy = ThresholdPolicy(threshold_db=args.threshold_db)
    histogram = HistogramSpec()
    site = SiteMetadata(site_id=args.site_id,
                        receiver_model=args.receiver_model,
                        antenna=args.antenna, grid_square=args.grid_square)

    rows = list(parse_rtl_power(args.csv))
    if not rows:
        print(f"{args.csv}: no parseable rows", file=sys.stderr)
        return 1

    # One rtl_power sweep is several rows sharing a timestamp, each covering
    # a frequency segment. Grouping by timestamp reconstructs the sweep.
    by_time = defaultdict(list)
    for r in rows:
        by_time[r.t_unix].append(r)
    timestamps = sorted(by_time)

    design = SamplingDesign(
        integration_s=args.interval_s,
        dwell_s=max(args.interval_s, 1.0),
        revisit_target_s=max(args.interval_s * len(by_time[timestamps[0]]), 1.0),
        jitter_fraction=0.0,
        usable_bandwidth_hz=max(r.hz_high - r.hz_low for r in rows),
        usable_fraction=1.0,   # rtl_power already crops its own edges
        interval_s=3600.0,
    )

    accumulators, baselines, bins_per_channel, observed = {}, {}, {}, {}
    for band in bands:
        accumulators[band.name] = BandAccumulator(band, band.channels(),
                                                  histogram, policy)
        bins_per_channel[band.name] = 0
        observed[band.name] = 0

    for t in timestamps:
        for seg in by_time[t]:
            axis = seg.freq_axis()
            if axis.size < 2:
                continue
            for band in bands:
                if seg.hz_high < band.freq_min_hz or \
                        seg.hz_low > band.freq_max_hz:
                    continue
                key = (band.name, round(seg.hz_low), round(seg.hz_step))
                ex = ChannelLevelExtractor(
                    accumulators[band.name].channels, axis,
                    usable_fraction=design.usable_fraction)
                if ex.n_observed == 0:
                    continue
                bins_per_channel[band.name] = max(
                    bins_per_channel[band.name], ex.bins_per_channel)
                observed[band.name] = max(observed[band.name], ex.n_observed)

                baseline = baselines.get(key)
                if baseline is None:
                    baseline = BandBaseline(policy)
                    baselines[key] = baseline
                levels = ex.levels_db(seg.levels_db)
                accumulators[band.name].add_sweep(
                    decide(levels, baseline.update(levels), policy))

    sched = Schedule(bands, design)
    sampling = sched.design_summary()
    sampling["warnings"] = sampling.get("warnings", []) + [
        f"rtl_power source: bins were averaged over {args.interval_s:g} s "
        f"BEFORE thresholding, so intermittent channels tend to read 0% or "
        f"100% rather than their duty cycle"
    ]

    meta = AcquisitionMetadata(
        site=site.as_metadata(), sampling=sampling,
        detection=policy.as_metadata(), histogram=histogram.as_metadata(),
        receiver=receiver_metadata(args.interval_s, args.gain, args.csv))

    ds = IntervalDataset(
        metadata=meta,
        interval_start=datetime.fromtimestamp(timestamps[0], timezone.utc),
        interval_end=datetime.fromtimestamp(
            timestamps[-1] + args.interval_s, timezone.utc))
    for band in bands:
        if observed[band.name] == 0:
            continue
        ds.add_band(band, accumulators[band.name],
                    bins_per_channel[band.name], observed[band.name])

    if not ds.bands:
        print("no band in the CSV overlapped the requested bands",
              file=sys.stderr)
        return 1

    from occupancy_survey.detector import occupancy_floor_warning
    for band in bands:
        if observed[band.name] == 0:
            continue
        w = occupancy_floor_warning(args.threshold_db,
                                    bins_per_channel[band.name])
        if w:
            print(f"\nWARNING {band.name}: {w}", file=sys.stderr)
            print(f"  rtl_power's bin size is set by the third field of -f. "
                  f"Use a bin several times\n  finer than the channel "
                  f"spacing, e.g. -f 150.8M:156M:2k for 12.5 kHz "
                  f"channels.", file=sys.stderr)

    path = ds.write(args.out_dir)
    print(path)
    print(f"\nNOTE: this dataset carries averaging_bias=pre-threshold. "
          f"Occupancy from it is\nnot comparable with rtl_sdr data at the "
          f"same threshold, and short transmissions\nare under-reported. "
          f"See occupancy_survey/backends/rtl_power_csv.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
