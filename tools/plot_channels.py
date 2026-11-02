"""Per-channel occupancy over time: one row per channel heard, one column
per interval.

plot_occupancy.py draws frequency on a continuous axis, which suits a grid
band and misleads on a targeted list: each sparse channel is stretched
across the empty spectrum to its neighbour, so a single 12.5 kHz licence
reads as a 2 MHz block. Here the channels are categories, labelled with
their service and licensee from the channel list, so every row is exactly
one channel and nothing is drawn where nothing was measured.

    cell    fraction of the interval a carrier was present
    bar     the whole run's duty for that channel, n-weighted across intervals

The time resolution is the interval the survey wrote -- an hour by default.
Set sampling.interval_s to 600 for 10-minute columns; the files hold no finer
time series, by design.

Rows are channels whose whole-run duty reaches --min-duty (default 0.1%);
--all keeps every channel. Receiver spurs are left out, as in the service
figures. Missing cells (a file that did not cover the channel) are hatched,
not drawn as zero.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_occupancy import _expand  # noqa: E402

TICKS = (0, 1, 5, 10, 25, 50, 100)
BAND_GAP_PX = 16


def load(paths):
    ds = [json.load(gzip.open(p)) for p in paths]
    ds.sort(key=lambda d: d["interval"]["start_utc"])
    return ds


def load_names(path) -> dict:
    """freq_hz -> (service, owner) from a make_channel_list.py file."""
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for band in data.get("bands", {}).values():
        for c in band["channels"]:
            out[round(float(c["freq_hz"]))] = (c.get("service") or "",
                                               c.get("owner") or "")
    return out


def column_labels(datasets):
    out = []
    for d in datasets:
        t = datetime.fromisoformat(d["interval"]["start_utc"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        # Local time alone repeats an hour when daylight saving ends -- the
        # first overnight run straddled exactly that -- so carry UTC too.
        out.append(f"{t.astimezone():%H:%M} ({t.astimezone(timezone.utc):%H}Z)")
    return out


def channel_matrix(datasets, bands, min_duty=1e-3, keep_all=False):
    """Rows (band, freq_hz, whole-run duty) and duty[row, interval].

    NaN where an interval did not cover the channel.
    """
    cells = {}
    totals = {}
    for j, d in enumerate(datasets):
        for band in bands:
            for c in d["bands"].get(band, {}).get("channels", []):
                if c.get("receiver_spur") or not c.get("n_decided"):
                    continue
                key = (band, round(float(c["freq_hz"])))
                cells.setdefault(key, {})[j] = float(c["occupancy"])
                n, k = totals.get(key, (0, 0))
                totals[key] = (n + c["n_decided"], k + c["n_present"])
    rows = []
    for key, (n, k) in totals.items():
        duty = k / n if n else 0.0
        if keep_all or duty >= min_duty:
            rows.append((key[0], key[1], duty))
    rows.sort(key=lambda r: (bands.index(r[0]), r[1]))
    m = np.full((len(rows), len(datasets)), np.nan)
    for i, (band, f, _) in enumerate(rows):
        for j, v in cells[(band, f)].items():
            m[i, j] = v
    return rows, m


def plot(datasets, bands, names, out_path, title, min_duty, keep_all):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import PowerNorm

    rows, m = channel_matrix(datasets, bands, min_duty, keep_all)
    if not rows:
        print(f"{out_path.name}: no channel reaches the duty floor")
        return None
    cols = column_labels(datasets)
    h = max(3.0, 0.24 * len(rows) + 1.6)
    w = max(7.0, 0.42 * len(cols) + 6.5)
    fig, (ax, axb) = plt.subplots(
        1, 2, figsize=(w, h), sharey=True,
        gridspec_kw={"width_ratios": [max(4, len(cols)), 4], "wspace": 0.04})

    # One hue, light to dark; square-root so a 2% channel is visible beside
    # a 100% one without hiding which is which.
    cmap = plt.get_cmap("Blues").copy()
    cmap.set_bad("white")
    norm = PowerNorm(gamma=0.5, vmin=0, vmax=100)
    y = np.arange(len(rows))
    img = ax.imshow(100 * m, aspect="auto", cmap=cmap, norm=norm,
                    interpolation="nearest",
                    extent=(-0.5, len(cols) - 0.5, len(rows) - 0.5, -0.5))
    missing = np.argwhere(np.isnan(m))
    for i, j in missing:
        ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False,
                                   hatch="///", edgecolor="#bbbbbb", lw=0))
    # Hairline separators keep rows and columns countable.
    ax.set_xticks(np.arange(len(cols) + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(len(rows) + 1) - 0.5, minor=True)
    ax.grid(which="minor", color="white", linewidth=1.0)
    ax.tick_params(which="minor", length=0)
    for side in ax.spines.values():
        side.set_visible(False)

    labels = []
    for band, f, _ in rows:
        svc, owner = names.get(f, ("", ""))
        # Fixed width, so the right-aligned monospace labels line up.
        labels.append(f"{f / 1e6:9.4f}  {svc:<3} {owner[:24]:<24}")
    ax.set_yticks(y, labels, fontsize=7.5, family="monospace")
    step = max(1, len(cols) // 16)
    ax.set_xticks(np.arange(0, len(cols), step), cols[::step], fontsize=8,
                  rotation=90)
    ax.set_xlabel("local start of interval")

    # Band dividers and names when several bands share the figure.
    for i in range(len(rows)):
        if i and rows[i][0] != rows[i - 1][0]:
            for a in (ax, axb):
                a.axhline(i - 0.5, color="#555555", lw=1.0)
        if len(bands) > 1 and (i == 0 or rows[i][0] != rows[i - 1][0]):
            axb.text(1.0, i - 0.5, rows[i][0] + " ", ha="right", va="top",
                     fontsize=7.5, color="#555555", style="italic",
                     transform=axb.get_yaxis_transform())

    duty = np.array([r[2] for r in rows]) * 100
    axb.barh(y, duty, height=0.7, color="#3b6fb6")
    axb.set_xscale("symlog", linthresh=1)
    axb.set_xlim(0, 150)
    axb.set_xticks([0, 1, 10, 100], ["0", "1", "10", "100"], fontsize=8)
    axb.set_xlabel("whole run (%)")
    for yi, v in zip(y, duty):
        axb.text(v * 1.08 + 0.05, yi, f"{v:.1f}" if v < 99.95 else "100",
                 va="center", fontsize=7, color="#333333")
    axb.tick_params(axis="y", length=0)
    for side in ("top", "right"):
        axb.spines[side].set_visible(False)
    axb.grid(axis="x", color="#e5e5e5", lw=0.6)
    axb.set_axisbelow(True)

    cb = fig.colorbar(img, ax=[ax, axb], fraction=0.025, pad=0.02,
                      ticks=TICKS)
    cb.set_label("carrier present (% of interval)")
    span = (f"{cols[0]} to {cols[-1]} local, {len(cols)} interval(s) of "
            f"{datasets[0]['interval'].get('duration_s', 0) / 60:.0f} min")
    floor = "all channels" if keep_all else f"duty >= {100 * min_duty:g}%"
    ax.set_title(f"{title} -- {len(rows)} channel(s), {floor}", loc="left",
                 fontsize=10)
    fig.text(0.01, 0.002, f"{span}. Hatched: not covered. Spurs excluded. "
             f"Uncalibrated, one site, no intermod discrimination.",
             fontsize=7, color="#555555")
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_waterfall(datasets, bands, out_path, title, row_px=24):
    """SDR-style waterfall: one pixel per channel, time down, every channel.

    Channels sit side by side in frequency order with no spacing, so on a
    grid band the x axis is frequency; on a targeted list it is channel
    index, and the tick labels say which frequency is where. Spurs and
    uncovered cells are grey, never zero.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import PowerNorm

    rows, m = channel_matrix(datasets, bands, keep_all=True)
    if not rows:
        return None
    n_ch, n_t = len(rows), len(datasets)
    # One pixel per channel; a short targeted list gets a whole number of
    # pixels each instead, or 183 channels would be 183 pixels wide.
    ch_px = max(1, 900 // n_ch)

    # Built at pixel resolution so bands can sit apart: a blank gap between
    # them, which a divider line on abutting bands did not make readable.
    x0 = []                                  # first pixel of each channel
    x = 0
    for i in range(n_ch):
        if i and rows[i][0] != rows[i - 1][0]:
            x += BAND_GAP_PX
        x0.append(x)
        x += ch_px
    width = x
    img = np.full((n_t, width), np.nan)
    gap = np.ones(width, dtype=bool)
    for i, xs in enumerate(x0):
        img[:, xs:xs + ch_px] = 100 * m[i][:, None]
        gap[xs:xs + ch_px] = False

    dpi = 100
    left, right, top, bottom = 110, 120, 50, 90      # margins, pixels
    w_px = width + left + right
    h_px = n_t * row_px + top + bottom
    fig = plt.figure(figsize=(w_px / dpi, h_px / dpi), dpi=dpi)
    ax = fig.add_axes([left / w_px, bottom / h_px,
                       width / w_px, n_t * row_px / h_px])
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("#9a9a9a")
    norm = PowerNorm(gamma=0.5, vmin=0, vmax=100)
    im = ax.imshow(img, aspect="auto", cmap=cmap, norm=norm,
                   interpolation="nearest",
                   extent=(0, width, n_t - 0.5, -0.5))
    # Gaps are page, not "not covered" grey.
    edges = np.flatnonzero(np.diff(np.r_[0, gap.astype(int), 0]))
    for a, b in zip(edges[::2], edges[1::2]):
        ax.axvspan(a, b, color="white", lw=0)
    for side in ax.spines.values():
        side.set_visible(False)

    # Ticks at round frequencies, wherever they fall in channel order.
    freqs = np.array([r[1] for r in rows]) / 1e6
    n_ticks = max(2, min(12, width // 90))
    idx = np.unique(np.linspace(0, n_ch - 1, n_ticks).round().astype(int))
    ax.set_xticks([x0[i] + ch_px / 2 for i in idx],
                  [f"{freqs[i]:.3f}" for i in idx], fontsize=7, rotation=90)
    ax.set_xlabel("channel (MHz)")
    ax.set_yticks(range(n_t), column_labels(datasets), fontsize=7)
    if len(bands) > 1:
        starts = [i for i in range(n_ch)
                  if i == 0 or rows[i][0] != rows[i - 1][0]]
        # Staggered: a narrow band (NWR is six channels) would put its
        # name on top of the next band's.
        for k, i in enumerate(starts):
            ax.text(x0[i], -0.6 - 0.55 * (k % 2), rows[i][0], fontsize=7,
                    va="bottom", color="#333333")
    ax.set_title(f"{title} -- {n_ch} channels, {ch_px} px each", loc="left",
                 fontsize=9, pad=22 if len(bands) > 1 else 4)
    cax = fig.add_axes([(left + width + 15) / w_px, bottom / h_px,
                        12 / w_px, n_t * row_px / h_px])
    cb = fig.colorbar(im, cax=cax, ticks=TICKS)
    cb.set_label("carrier present (%)", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)
    return out_path


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("datasets", nargs="+", help="occ_*.json.gz files")
    ap.add_argument("--channels", help="channel list from "
                    "tools/make_channel_list.py, for service and licensee")
    ap.add_argument("--min-duty", type=float, default=1e-3,
                    help="whole-run duty a channel needs to get a row "
                         "(fraction, default 0.001)")
    ap.add_argument("--all", action="store_true",
                    help="a row for every channel, heard or not")
    ap.add_argument("--out", default="out/plots")
    args = ap.parse_args(argv)

    datasets = load(_expand(args.datasets))
    if not datasets:
        print("no datasets", file=sys.stderr)
        return 2
    names = load_names(args.channels)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    bands = sorted(datasets[0]["bands"],
                   key=lambda b: datasets[0]["bands"][b]["freq_min_hz"])
    targets = [b for b in bands
               if datasets[0]["bands"][b].get("role") != "control"]
    written = [plot(datasets, targets, names, out / "channels_targets.png",
                    "target bands", args.min_duty, args.all)]
    for b in bands:
        written.append(plot(datasets, [b], names, out / f"channels_{b}.png",
                            b, args.min_duty, args.all))
    written.append(plot_waterfall(datasets, targets,
                                  out / "waterfall_targets.png",
                                  "target bands"))
    for b in bands:
        written.append(plot_waterfall(datasets, [b],
                                      out / f"waterfall_{b}.png", b))
    for p in written:
        if p:
            print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
