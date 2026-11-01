"""Exploratory plots of interval files: when each channel was occupied.

Validation by eye, not a result. One waterfall per band -- frequency across,
local time down, one row per interval file -- in two forms:

  duty    fraction of the hour a carrier was present, per channel
  level   95th-percentile excess over the band baseline, from the stored
          histogram, so a channel can be seen to be LOUD without being BUSY
          (an intermod product keyed by a strong neighbour) or busy at a
          level barely over threshold (noise, or a weak distant user)

An overview figure puts the per-hour health numbers side by side: active
channels per band, the baseline, FM clip fraction and gain lock. A step that
appears in every band at once is the receiver, not the traffic.

The time resolution is the interval (an hour by default). The files hold no
finer time series, by design -- runs, gaps and histograms only.
"""

from __future__ import annotations

import argparse
import glob
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np


def _expand(patterns):
    # Windows shells do not expand globs; doing it here is harmless elsewhere.
    out = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        out.extend(hits if hits else [p])
    return out


def _local_start(d) -> datetime:
    iv = d["interval"]
    start = datetime.fromisoformat(iv["start_utc"])
    return (start + timedelta(seconds=iv.get("utc_offset_s") or 0.0)) \
        .replace(tzinfo=None)


def _row_labels(datasets):
    # Local time alone repeats an hour when daylight saving ends -- the
    # first overnight run straddled exactly that -- so carry UTC too.
    return [f"{_local_start(d):%H:%M} ({d['interval']['start_utc'][11:13]}Z)"
            for d in datasets]


def band_matrices(datasets, band: str):
    """(freqs_hz, duty[t, ch], p95_excess_db[t, ch]) for one band.

    Channels are keyed by frequency, so a file that lacks a channel leaves
    NaN rather than shifting every column after it.
    """
    from occupancy_survey.presence import _percentile_from_hist

    freqs = sorted({c["freq_hz"] for d in datasets if band in d["bands"]
                    for c in d["bands"][band]["channels"]})
    col = {f: i for i, f in enumerate(freqs)}
    duty = np.full((len(datasets), len(freqs)), np.nan)
    level = np.full_like(duty, np.nan)
    for t, d in enumerate(datasets):
        b = d["bands"].get(band)
        if b is None:
            continue
        spec = d["acquisition"]["histogram"]
        for c in b["channels"]:
            if not c.get("n_decided"):
                continue
            j = col[c["freq_hz"]]
            duty[t, j] = c["n_present"] / c["n_decided"]
            level[t, j] = _percentile_from_hist(c, spec, 95.0)
    return np.asarray(freqs), duty, level


def _edges(centres):
    if len(centres) == 1:
        return np.array([centres[0] - 0.5, centres[0] + 0.5])
    mid = (centres[1:] + centres[:-1]) / 2
    return np.concatenate([[2 * centres[0] - mid[0]], mid,
                           [2 * centres[-1] - mid[-1]]])


def plot_band(datasets, band, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    freqs, duty, level = band_matrices(datasets, band)
    starts = [_local_start(d) for d in datasets]
    threshold = datasets[0]["acquisition"]["detection"]["threshold_db"]
    b0 = datasets[0]["bands"][band]
    mhz = _edges(freqs / 1e6)
    rows = np.arange(len(datasets) + 1)

    fig, axes = plt.subplots(
        3, 2, figsize=(13, 9.5), sharex="col",
        gridspec_kw={"height_ratios": [1, 2.2, 2.2],
                     "width_ratios": [1, 0.025], "hspace": 0.12,
                     "wspace": 0.03})
    ax_prof, ax_duty, ax_lev = axes[:, 0]
    axes[0, 1].axis("off")

    night = np.nanmean(duty, axis=0)
    ax_prof.fill_between(freqs / 1e6, 100 * night, step="mid",
                         color="#b2182b", linewidth=0)
    ax_prof.set_ylabel("duty, whole run\n(%)")
    ax_prof.set_ylim(0, 100)
    ax_prof.grid(alpha=0.3)
    role = b0.get("role", "")
    ax_prof.set_title(
        f"{band} ({b0.get('service')}, {role}) -- "
        f"{len(freqs)} channels, {len(datasets)} interval(s), "
        f"threshold {threshold:g} dB", loc="left", fontsize=11)

    m = ax_duty.pcolormesh(mhz, rows, 100 * duty, cmap="inferno",
                           vmin=0, vmax=100, shading="flat")
    fig.colorbar(m, cax=axes[1, 1], label="carrier present (% of interval)")
    m = ax_lev.pcolormesh(mhz, rows, level, cmap="viridis",
                          vmin=-2, vmax=40, shading="flat")
    cb = fig.colorbar(m, cax=axes[2, 1], label="p95 excess over baseline (dB)")
    cb.ax.axhline(threshold, color="white", linewidth=1.5)

    labels = _row_labels(datasets)
    step = max(1, len(labels) // 14)
    for ax, name in ((ax_duty, "duty"), (ax_lev, "level")):
        ax.set_yticks(np.arange(len(labels))[::step] + 0.5)
        ax.set_yticklabels(labels[::step], fontsize=8)
        ax.invert_yaxis()
        ax.set_ylabel(f"{name}\nlocal start of interval")
    ax_lev.set_xlabel("frequency (MHz)")
    fig.text(0.01, 0.005,
             f"{starts[0]:%Y-%m-%d %H:%M} to "
             f"{starts[-1] + timedelta(seconds=datasets[-1]['interval']['duration_s']):%Y-%m-%d %H:%M} local.  "
             "Exploratory: uncalibrated, one site, no intermod discrimination. "
             "White line on the level scale is the detection threshold.",
             fontsize=7.5, color="#555555")
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def plot_overview(datasets, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Plotted by index, not time: local time is not monotonic across a
    # daylight-saving change, and the intervals are contiguous anyway.
    starts = np.arange(len(datasets))
    bands = list(datasets[0]["bands"])
    fig, axes = plt.subplots(4, 1, figsize=(11, 10), sharex=True)

    def series(band, fn):
        return [fn(d["bands"][band]) if band in d["bands"] else np.nan
                for d in datasets]

    for band in bands:
        axes[0].plot(starts, series(band, lambda b: b["summary"]
                                    ["active_channels"]["mean"]),
                     marker="o", label=band)
        axes[1].plot(starts, series(band, lambda b: sum(
            1 for c in b["channels"] if c.get("n_decided")
            and c["n_present"] / c["n_decided"] > 0.5)), marker="o",
            label=band)
        axes[2].plot(starts, series(band, lambda b: b["summary"]
                                    ["baseline_dbfs"]["mean"]),
                     marker="o", label=band)
    axes[0].set_ylabel("channels active\nper sweep (mean)")
    axes[1].set_ylabel("channels busy\n> 50% of the hour")
    axes[2].set_ylabel("band baseline\n(dBFS, uncalibrated)")
    axes[0].legend(fontsize=8, ncol=len(bands))

    clip = [max(b["summary"]["clip_fraction"]["max"]
                for b in d["bands"].values()) for d in datasets]
    drift = [d["acquisition"]["receiver"].get("gain_in_effect", {})
             .get("worst_drift_db", np.nan) for d in datasets]
    axes[3].plot(starts, clip, marker="o", color="#b2182b",
                 label="worst clip fraction (any band)")
    axes[3].set_ylabel("clip fraction")
    ax2 = axes[3].twinx()
    ax2.step(starts, drift, where="post", color="#2166ac",
             label="gain readback drift (dB)")
    ax2.set_ylabel("gain drift (dB)")
    axes[3].legend(loc="upper left", fontsize=8)
    ax2.legend(loc="upper right", fontsize=8)
    for ax in axes:
        ax.grid(alpha=0.3)
    axes[-1].set_xticks(starts)
    axes[-1].set_xticklabels(_row_labels(datasets), rotation=45, ha="right",
                             fontsize=8)
    axes[-1].set_xlabel("local start of interval")
    axes[0].set_title(f"{datasets[0]['acquisition']['site']['site_id']} -- "
                      f"per-interval health, {len(datasets)} interval(s)",
                      loc="left")
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from occupancy_survey.dataset import read_dataset

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("datasets", nargs="+", help="occ_*.json.gz files")
    ap.add_argument("--out", default="out/plots",
                    help="directory for the PNGs (default out/plots)")
    args = ap.parse_args(argv)

    datasets = [read_dataset(p) for p in _expand(args.datasets)]
    sites = {d["acquisition"]["site"]["site_id"] for d in datasets}
    if len(sites) > 1:
        print(f"one site at a time; got {sorted(sites)}", file=sys.stderr)
        return 2
    datasets.sort(key=lambda d: d["interval"]["start_utc"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    plot_overview(datasets, out / "overview.png")
    print(out / "overview.png")
    for band in datasets[0]["bands"]:
        path = out / f"waterfall_{band}.png"
        plot_band(datasets, band, path)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
