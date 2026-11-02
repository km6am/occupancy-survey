"""Compare measured occupancy against sota-rf's prediction for the same QTH.

This is why the survey is more than data collection. It cross-checks the
monitor and the model at once, and the four cases it can produce mean
different things:

  predicted strong + measured active    consistent. Weak evidence: it is
                                        also what a broken detector that
                                        fires on anything would produce.
  predicted strong + never active       the prediction or the RECEIVE PATH
                                        is wrong. Worth chasing either way.
  predicted weak + measured active      propagation better than ERP/d^2, or
                                        a licence record this tool did not
                                        match.
  busy with no FCC record at all        worth knowing about.

THE DISCRIMINATING COMPARISON is the second panel, not the first. The first
panel -- occupancy against predicted field proxy -- is merely CONSISTENT
with the model being right; a detector that triggered on noise would also
put the strong emitters at the top. The second panel asks a question whose
answers differ: duty_cycles.md section 2 claims, from architecture alone,
that trunked sites (Y-prefix service codes) and broadcast carriers are
CONTINUOUS while conventional land mobile (PW, IG) is intermittent at
10-25%. Those predict different occupancy distributions, so the measurement
can distinguish them, and it can fail.

TITLE DISCIPLINE. No title here asserts that the model is validated, that
the prediction is confirmed, or that anything is caused by anything. They
say what was plotted against what, where and under which acquisition
conditions. A plot title carries the same evidential weight as a sentence.

A QTH IS NOT A SUMMIT. Every figure says so. This yields duty cycles per
service, which is what the risk model lacks. It does not yield summit field
strengths: different propagation, different antenna.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

import numpy as np

# Colour-blind safe, and continuity class is the only encoded dimension.
CONT_COLOURS = {
    "continuous": "#0072B2",
    "intermittent": "#D55E00",
    "unknown": "#999999",
}
ASSUMED_LINES = [
    (1.00, "assumed 100% (continuous, architectural)", "#0072B2"),
    (0.225, "assumed 22.5% (conventional LMR, TIA-603)", "#D55E00"),
    (0.10, "assumed 10% (portable, TIA-603)", "#D55E00"),
]


def load_measured(paths, threshold_db=None, min_decided=100):
    """Measured occupancy per channel, pooled over intervals.

    Refuses synthetic datasets: they are useful for testing the pipeline and
    must never reach a result that claims to be measured.
    """
    from occupancy_survey.dataset import merge_channels, read_dataset

    datasets = [read_dataset(p) for p in paths]
    if not datasets:
        raise SystemExit("no datasets given")

    synthetic = [d for d in datasets
                 if d["acquisition"]["receiver"].get("synthetic")]
    if synthetic:
        raise SystemExit(
            f"{len(synthetic)} of {len(datasets)} datasets came from the "
            f"synthetic backend. Refusing to validate a model against "
            f"simulated data -- it would agree with whatever it was told to."
        )

    band_names = sorted({n for d in datasets for n in d.get("bands", {})})
    measured: dict[float, dict] = {}
    controls: dict[str, list] = {}

    for name in band_names:
        roles = {d["bands"][name]["role"] for d in datasets
                 if name in d.get("bands", {})}
        role = roles.pop() if len(roles) == 1 else "mixed"
        if role == "control":
            controls[name] = [d["bands"][name].get("control_check")
                              for d in datasets if name in d.get("bands", {})]

        thr = threshold_db
        if thr is None:
            ts = {d["acquisition"]["detection"]["threshold_db"]
                  for d in datasets if name in d.get("bands", {})}
            if len(ts) != 1:
                raise SystemExit(
                    f"'{name}' spans datasets with different thresholds "
                    f"{sorted(ts)}; pass --threshold-db so the figure states "
                    f"which one it used.")
            thr = ts.pop()

        for m in merge_channels(datasets, name):
            if m.n_decided < min_decided:
                continue
            measured[round(m.freq_hz, 1)] = {
                "freq_hz": m.freq_hz, "band": name, "band_role": role,
                "occupancy": m.occupancy_at(thr), "n_decided": m.n_decided,
                "threshold_db": thr, "sites": sorted(m.sites),
                "comparable": m.comparable,
                # Only read on channels that are present for most samples:
                # on an intermittent channel the median is the noise floor.
                # The FM path residual is the one consumer, and it selects
                # on occupancy before using these.
                "excess_db_p50": m.excess_db_percentile(50.0),
                "excess_db_p90": m.excess_db_percentile(90.0),
            }

    acq = datasets[0]["acquisition"]
    conditions = {
        "sites": sorted({d["acquisition"]["site"]["site_id"]
                         for d in datasets}),
        "intervals": len(datasets),
        "first_utc": min(d["interval"]["start_utc"] for d in datasets),
        "last_utc": max(d["interval"]["end_utc"] for d in datasets),
        "threshold_db": threshold_db,
        "baseline": acq["detection"]["estimator"],
        "baseline_percentile": acq["detection"]["baseline_percentile"],
        "dwell_s": acq["sampling"]["dwell_s"],
        "integration_s": acq["sampling"]["integration_s"],
        "revisit_s": acq["sampling"].get("cycle_time_s"),
        "min_burst_s": acq["sampling"].get(
            "min_detectable_burst_s_at_6db_margin"),
        "receiver": acq["receiver"].get("backend"),
        "receiver_model": acq["site"]["receiver_model"],
        "antenna": acq["site"]["antenna"],
        "gain": acq["receiver"].get("gain"),
        "grid_square": acq["site"].get("grid_square"),
        "averaging_bias": acq["receiver"].get("averaging_bias"),
        "warnings": acq["sampling"].get("warnings", []),
    }
    return measured, controls, conditions


def load_prediction(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not str(data.get("schema", "")).startswith(
            "occupancy-survey/qth-prediction"):
        raise SystemExit(f"{path}: not a qth_predict.py prediction file")
    return data


def join(prediction, measured, match_tolerance_hz=6_500.0):
    """Match predicted channels to measured ones.

    The tolerance is half a 12.5 kHz channel: an FCC-listed frequency need
    not land exactly on this tool's uniform grid, and a prediction half a
    channel away is the same transmitter, not a different one.
    """
    rows = []
    unmatched_measured = dict(measured)

    for pred in prediction["channels"]:
        best, best_d = None, None
        for key, m in measured.items():
            d = abs(m["freq_hz"] - pred["freq_hz"])
            if d <= match_tolerance_hz and (best_d is None or d < best_d):
                best, best_d = key, d
        row = dict(pred)
        if best is None:
            row.update(matched=False, occupancy=None, n_decided=0,
                       offset_hz=None)
        else:
            m = measured[best]
            row.update(matched=True, occupancy=m["occupancy"],
                       n_decided=m["n_decided"], offset_hz=best_d,
                       band_measured=m["band"], sites=m["sites"],
                       band_role=m["band_role"])
            unmatched_measured.pop(best, None)
        rows.append(row)

    busy_unpredicted = [m for m in unmatched_measured.values()
                        if (m["occupancy"] or 0.0) > 0.0
                        and m["band_role"] != "control"]
    busy_unpredicted.sort(key=lambda m: m["occupancy"], reverse=True)
    return rows, busy_unpredicted


def empirical_audibility_floor(rows):
    """The field proxy at which this receive path stops hearing things.

    Measured, not assumed, and this is what makes the "predicted but never
    active" list meaningful.

    A smoke test against real FCC data for one location returned 11,980
    predicted channels in the surveyed bands, 10,369 of them intermittent.
    Most are low-power licences far away that NOTHING could hear, so an
    unfiltered list of predicted-but-silent channels is thousands of rows
    long and says nothing.

    The control bands fix that. A broadcast-FM transmitter radiates a
    carrier through programme silence, so a control channel that was
    detected is one this antenna and gain setting can definitely hear. The WEAKEST
    such channel's predicted proxy is therefore an empirical floor: above
    it, silence is an anomaly worth chasing; below it, silence is expected.

    Returns (floor, n_controls_used), or (None, 0) when no control channel
    was both predicted and measured active -- in which case the caller must
    say the list is unfiltered rather than quietly filtering on a guess.

    The weak link: the controls are broadcast transmitters, usually high up
    and high power. Extending their floor to land-mobile sites assumes the
    proxy's errors do not depend on the kind of emitter, and terrain
    shadowing makes that doubtful. Treat the floor as an order of
    magnitude, not a threshold.
    """
    usable = [r for r in rows
              if r.get("matched") and r.get("band_role") == "control"
              and (r.get("occupancy") or 0.0) > 0.5
              and r.get("field_proxy_w_per_m2")]
    if not usable:
        return None, 0
    return min(r["field_proxy_w_per_m2"] for r in usable), len(usable)


# A NamedTuple, not a dataclass: these tool modules are loaded by path with
# importlib without being registered in sys.modules, and dataclass() reaches
# into sys.modules[cls.__module__] to resolve annotations, so a dataclass
# here raises at import time.
class AudibilityFloor(NamedTuple):
    """Where this receive path stops hearing things, and in what units.

    Two engines give two different floors, and they are not interchangeable:

      dBm    from a /2 prediction's p_dbm_ref. Preferred, because it is
             LESS frequency-blind: at equal field a matched antenna's
             capture area grows as lambda^2, so a 150 MHz channel delivers
             about 9.5 dB more power than a 450 MHz one, and the proxy
             cannot see that difference while p_dbm_ref can. It is not
             frequency-CORRECT -- p_dbm_ref is computed at a reference gain,
             not this antenna's actual gain, which also varies with
             frequency -- so transferring an FM-derived floor to 450 MHz is
             still an extrapolation, just a smaller one.

      proxy  from ERP/d^2, frequency-blind. All a /1 prediction can give.

    Either way the controls are broadcast sites, usually high and strong,
    and land mobile is shadowed differently. An order of magnitude, not a
    threshold.
    """
    kind: str                      # "dbm" | "proxy" | "none"
    value: float | None
    n_controls: int
    field: str | None              # the row key to compare against
    unit: str

    @property
    def available(self) -> bool:
        return self.value is not None

    def describe(self) -> str:
        if not self.available:
            return "no empirical audibility floor"
        shown = (f"{self.value:.1f} {self.unit}" if self.kind == "dbm"
                 else f"{self.value:.2e} {self.unit}")
        return (f"{shown}, from the weakest of {self.n_controls} control "
                f"channel(s) actually detected")


def audibility_floor(rows) -> AudibilityFloor:
    """The floor, in dBm when the prediction carries levels, else proxy."""
    usable = [r for r in rows
              if r.get("matched") and r.get("band_role") == "control"
              and (r.get("occupancy") or 0.0) > 0.5]

    dbm = [r["p_dbm_ref"] for r in usable
           if r.get("p_dbm_ref") is not None]
    if dbm:
        return AudibilityFloor("dbm", min(dbm), len(dbm), "p_dbm_ref", "dBm")

    proxy = [r["field_proxy_w_per_m2"] for r in usable
             if r.get("field_proxy_w_per_m2")]
    if proxy:
        return AudibilityFloor("proxy", min(proxy), len(proxy),
                               "field_proxy_w_per_m2", "W/m^2")
    return AudibilityFloor("none", None, 0, None, "")


# Predicted, matched, and never measured active. The three-way split exists
# because a two-way one puts every channel NEAR the floor into the anomaly
# list, where the model's own error band says it does not belong.
SILENT_ANOMALY = "anomaly"
SILENT_UNDETERMINED = "undetermined"
SILENT_EXPECTED = "expected"
SILENT_UNFILTERED = "unfiltered"

SILENT_MEANING = {
    SILENT_ANOMALY: "above the floor by more than the model's own error "
                    "band: the prediction or the receive path is wrong",
    SILENT_UNDETERMINED: "within the model's error band of the floor, or "
                         "with no level to compare: the measurement cannot "
                         "decide, and neither can this tool",
    SILENT_EXPECTED: "below the floor by more than the error band: silence "
                     "is what this receive path should produce",
    SILENT_UNFILTERED: "no control channel was heard, so there is no floor "
                       "and nothing has been filtered",
}


def classify_silent(rows, floor: AudibilityFloor) -> dict[str, list]:
    """Split predicted-but-never-active channels by what silence means.

    Three-way only with a dBm floor AND a per-row error_db, because the
    middle class is defined by that band and nothing else. A /1 prediction
    gets the old two-way split, and the caller must say which it got.
    """
    out: dict[str, list] = {SILENT_ANOMALY: [], SILENT_UNDETERMINED: [],
                            SILENT_EXPECTED: [], SILENT_UNFILTERED: []}
    silent = [r for r in rows
              if r.get("matched") and (r.get("occupancy") or 0.0) <= 0.0]

    for r in silent:
        if not floor.available:
            out[SILENT_UNFILTERED].append(dict(r, margin_db=None))
            continue

        if floor.kind == "dbm":
            level = r.get("p_dbm_ref")
            err = r.get("error_db")
            if level is None or err is None:
                # No level, or no error band to judge the margin's
                # significance by. Undetermined is the honest class; calling
                # it an anomaly would manufacture a finding.
                out[SILENT_UNDETERMINED].append(dict(r, margin_db=None))
                continue
            margin = level - floor.value
            if margin > err:
                out[SILENT_ANOMALY].append(dict(r, margin_db=margin))
            elif margin < -err:
                out[SILENT_EXPECTED].append(dict(r, margin_db=margin))
            else:
                out[SILENT_UNDETERMINED].append(dict(r, margin_db=margin))
            continue

        # proxy floor: two-way, no error band exists to define a middle
        proxy = r.get("field_proxy_w_per_m2") or 0.0
        if proxy >= floor.value:
            out[SILENT_ANOMALY].append(dict(r, margin_db=None))
        else:
            out[SILENT_EXPECTED].append(dict(r, margin_db=None))

    key = (lambda r: (r.get("margin_db") if r.get("margin_db") is not None
                      else -1e9))
    out[SILENT_ANOMALY].sort(key=key, reverse=True)
    out[SILENT_UNDETERMINED].sort(key=key, reverse=True)
    out[SILENT_EXPECTED].sort(key=key, reverse=True)
    out[SILENT_UNFILTERED].sort(
        key=lambda r: (r.get("field_proxy_w_per_m2") or 0.0), reverse=True)
    return out


def fm_path_residual(rows, min_occupancy: float = 0.5,
                     min_per_class: int = 3) -> dict:
    """Measured excess minus predicted level, split by path class.

    THIS IS THE DISCRIMINATING TEST, and the reason /2 is worth having.

    For a control carrier that is on continuously, the measured excess-dB
    median is the carrier level above the local noise floor, and p_dbm_ref
    is the model's prediction of the same carrier. Their difference is the
    receive chain's unknown constant -- antenna gain, feedline, noise
    figure, bandwidth -- plus whatever the model got wrong.

    The constant is the SAME for every channel in the band, so its absolute
    value says nothing. What it cannot hide is a DIFFERENCE between path
    classes: if the diffraction model is right, a line-of-sight carrier and
    a diffracted one must share the offset. A systematic gap between them is
    path-model error, and it points the right way -- a positive gap means
    diffraction loss was overestimated.

    Rank agreement between prediction and measurement does not do this. It
    is consistent with the model being right and equally consistent with a
    detector that fires on anything strong.

    Restricted to controls on purpose: on an intermittent channel the
    excess-dB median is the noise floor, not the signal, so the residual
    would measure duty cycle and be reported as path error.
    """
    usable = [r for r in rows
              if r.get("matched") and r.get("band_role") == "control"
              and (r.get("occupancy") or 0.0) >= min_occupancy
              and r.get("p_dbm_ref") is not None
              and r.get("excess_db_p50") is not None
              and r.get("path")]

    classes: dict[str, list] = {}
    for r in usable:
        classes.setdefault(str(r["path"]), []).append(
            float(r["excess_db_p50"]) - float(r["p_dbm_ref"]))

    stats = {}
    for name, vals in sorted(classes.items()):
        a = np.asarray(vals, dtype=float)
        stats[name] = {
            "n": int(a.size),
            "median_db": float(np.median(a)),
            "iqr_db": (float(np.percentile(a, 25)),
                       float(np.percentile(a, 75))),
            "spread_db": float(np.percentile(a, 75) - np.percentile(a, 25)),
        }

    out = {"classes": stats, "n_usable": len(usable), "gap_db": None,
           "runnable": False, "reason": None}

    if len(usable) == 0:
        out["reason"] = ("no control channel had both a predicted level and "
                         "a measured excess percentile; a /1 prediction "
                         "carries no p_dbm_ref at all")
        return out

    ready = {k: v for k, v in stats.items() if v["n"] >= min_per_class}
    if len(ready) < 2:
        have = ", ".join(f"{k} n={v['n']}" for k, v in stats.items()) or "none"
        out["reason"] = (
            f"needs {min_per_class}+ control channels in each of two path "
            f"classes; have {have}. Broadcast FM sites are high and mostly "
            f"line of sight, so the diffracted class is often empty and "
            f"this test simply cannot run at many locations.")
        return out

    if "los" in ready and "diffracted" in ready:
        out["gap_db"] = (ready["diffracted"]["median_db"]
                         - ready["los"]["median_db"])
    else:
        names = sorted(ready)
        out["gap_db"] = (ready[names[-1]]["median_db"]
                         - ready[names[0]]["median_db"])
        out["gap_between"] = (names[0], names[-1])
    out["runnable"] = True

    # The gap is only readable against the within-class scatter. A gap
    # smaller than the spread it sits inside is not evidence of anything.
    spread = max(v["spread_db"] for v in ready.values())
    out["spread_db"] = spread
    out["gap_exceeds_spread"] = abs(out["gap_db"]) > spread
    return out


def _provenance_line(prediction) -> str:
    """Which model built this prediction. A model change changes all of it."""
    schema = str(prediction.get("schema", "unknown"))
    model = prediction.get("model") or {}
    bits = [f"prediction {schema}"]
    if model.get("engine"):
        bits.append(f"engine {model['engine']}")
    if model.get("data"):
        bits.append(f"data {model['data']}")
    if model.get("dem"):
        bits.append(f"terrain {model['dem']}")
    if not model:
        bits.append("no model header: the pipeline engine, no terrain, "
                    "ERP/d^2")
    return " | ".join(bits)


def load_intermod_reports(paths) -> dict:
    """Read the JSON saved by `--intermod-check --intermod-json`.

    Why this is wired into the validator at all: intermod products appear
    at frequencies determined by the STRONG emitters, so they correlate
    with exactly the channels the prediction calls strong. An overloaded
    front end therefore raises measured occupancy on predicted-strong
    channels, which is the error direction that FLATTERS this comparison.
    An unchecked figure cannot distinguish a confirmed prediction from a
    dirty front end.
    """
    out = {"supplied": bool(paths), "bands": {}, "files": []}
    for path in paths or ():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out["files"].append(str(path))
        for entry in data.get("steps") or []:
            band = str(entry.get("band") or "?")
            b = out["bands"].setdefault(
                band, {"steps": 0, "assessable": 0, "suspect": 0,
                       "unexplained": 0, "unstable": 0})
            b["steps"] += 1
            for key, dest in (("channels_assessable", "assessable"),
                              ("channels_suspect", "suspect"),
                              ("channels_unexplained", "unexplained"),
                              ("channels_unstable", "unstable")):
                b[dest] += int(entry.get(key) or 0)
    return out


def _conditions_line(c):
    bits = [
        f"{', '.join(c['sites'])}",
        f"{c['grid_square'] or 'location withheld'}",
        f"{c['intervals']} interval(s), {c['first_utc'][:16]} to "
        f"{c['last_utc'][:16]} UTC",
    ]
    return " | ".join(bits)


def _method_line(c, threshold_db):
    return (f"threshold {threshold_db:.1f} dB over a "
            f"{c['baseline_percentile']:.0f}th-pct pooled baseline | "
            f"dwell {c['dwell_s']:.0f} s, integration "
            f"{c['integration_s']:.2f} s, revisit "
            f"{(c['revisit_s'] or 0):.0f} s | bursts < "
            f"{(c['min_burst_s'] or 0)*1000:.0f} ms not reliably seen | "
            f"{c['receiver_model']}, {c['antenna']}, fixed gain, AGC off")


def plot(rows, busy_unpredicted, conditions, threshold_db, out_path,
         prediction=None, intermod=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matched = [r for r in rows if r["matched"] and r["occupancy"] is not None]
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(13.5, 6.0), gridspec_kw={"width_ratios": [1.35, 1]})

    # ---------------- panel 1: occupancy vs predicted proxy ------------- #
    with_proxy = [r for r in matched
                  if r["field_proxy_w_per_m2"] not in (None, 0)]
    for cont, colour in CONT_COLOURS.items():
        pts = [r for r in with_proxy if r["continuity"] == cont]
        if not pts:
            continue
        ax1.scatter([r["field_proxy_w_per_m2"] for r in pts],
                    [100.0 * r["occupancy"] for r in pts],
                    s=38, alpha=0.8, color=colour, edgecolor="white",
                    linewidth=0.6, label=f"{cont} (n={len(pts)})")

    # The line stays on the proxy axis because that is this panel's axis.
    # The CLASSIFICATION below may use a dBm floor instead, which cannot be
    # drawn here, so the label says which quantity the line is.
    proxy_floor, n_ctrl = empirical_audibility_floor(rows)
    if proxy_floor is not None:
        ax1.axvline(proxy_floor, color="#555555", linestyle="--",
                    linewidth=1.1)
        ax1.annotate(
            f"proxy audibility floor\n({n_ctrl} control channel"
            f"{'s' if n_ctrl != 1 else ''} heard)",
            (proxy_floor, 46), fontsize=7.3, color="#555555", rotation=90,
            ha="right", va="center")

    floor = audibility_floor(rows)
    silent = classify_silent(rows, floor)

    def _plottable(group):
        return [r for r in group
                if r.get("field_proxy_w_per_m2") not in (None, 0)]

    # Hollow red: above the floor by more than the model's own error band.
    # Hollow amber: inside that band, where the measurement cannot decide.
    # Expected silence is not drawn at all -- it is the boring class, and
    # drawing it would put the thousands of distant low-power licences back
    # on the figure that the floor exists to remove.
    for cls, colour, note in (
            (SILENT_ANOMALY, "#CC0000", "above floor beyond the error band"),
            (SILENT_UNDETERMINED, "#E69F00", "within the error band")):
        group = _plottable(silent[cls])
        if not group:
            continue
        ax1.scatter([r["field_proxy_w_per_m2"] for r in group],
                    [0.0] * len(group), s=90, facecolor="none",
                    edgecolor=colour, linewidth=1.4, zorder=5,
                    label=f"never active, {note} (n={len(group)})")
    unfiltered = _plottable(silent[SILENT_UNFILTERED])
    if unfiltered:
        ax1.scatter([r["field_proxy_w_per_m2"] for r in unfiltered],
                    [0.0] * len(unfiltered), s=90, facecolor="none",
                    edgecolor="#999999", linewidth=1.2, zorder=4,
                    label=f"never active, NO floor to filter on "
                          f"(n={len(unfiltered)})")

    ax1.set_xscale("log")
    ax1.set_xlabel("FCC-record field proxy, ERP / distance$^2$ "
                   "(W m$^{-2}$, uncalibrated — ordering only)")
    ax1.set_ylabel("measured occupancy (% of decided samples)")
    # Headroom above 100% so the legend and the note never sit on data:
    # occupancy cannot exceed 100, so the band is guaranteed empty.
    ax1.set_ylim(-4, 126)
    ax1.set_yticks([0, 20, 40, 60, 80, 100])
    ax1.set_title("Measured occupancy vs FCC-record field proxy",
                  fontsize=11, loc="left")
    ax1.grid(alpha=0.25, linewidth=0.6)
    ax1.legend(fontsize=8, loc="upper left", framealpha=0.95, ncol=2)
    # Right-middle: occupancy is bimodal here (near 0 or near 100), so the
    # mid-range band on the strong-signal side is reliably empty.
    ax1.text(0.975, 0.50,
             "consistent with the model\nis not evidence for it;\n"
             "the right-hand panel is\nthe discriminating comparison",
             transform=ax1.transAxes, fontsize=7.8, style="italic",
             ha="right", va="center", color="#555555", linespacing=1.5)

    # ---------------- panel 2: occupancy by continuity class ------------ #
    classes = ["continuous", "intermittent", "unknown"]
    data, labels, colours = [], [], []
    for i, cont in enumerate(classes):
        vals = [100.0 * r["occupancy"] for r in matched
                if r["continuity"] == cont]
        if not vals:
            continue
        data.append(vals)
        labels.append(f"{cont}\n(n={len(vals)})")
        colours.append(CONT_COLOURS[cont])

    if data:
        rng = np.random.default_rng(0)
        for i, (vals, colour) in enumerate(zip(data, colours)):
            x = (i + 1) + rng.uniform(-0.13, 0.13, size=len(vals))
            ax2.scatter(x, vals, s=34, alpha=0.75, color=colour,
                        edgecolor="white", linewidth=0.5, zorder=3)
            med = float(np.median(vals))
            ax2.plot([i + 0.72, i + 1.28], [med, med], color=colour,
                     linewidth=2.4, zorder=4)
            ax2.annotate(f"median {med:.0f}%", (i + 1.31, med), fontsize=8,
                         color=colour, va="center", ha="left",
                         fontweight="bold", zorder=5)
        ax2.set_xticks(range(1, len(data) + 1))
        ax2.set_xticklabels(labels, fontsize=9)
    x_hi = (len(data) + 1.0) if data else 1.5
    ax2.set_xlim(0.30, x_hi)

    for value, label, colour in ASSUMED_LINES:
        ax2.axhline(100.0 * value, color=colour, linestyle=":", linewidth=1.2,
                    alpha=0.8)
        # Left margin: the strips sit at tick positions 1..n, so the
        # column left of the first strip is the reliably empty one, and
        # the median labels take the space to the right of each bar.
        ax2.annotate(label, (0.36, 100.0 * value + 1.8), fontsize=7.3,
                     color=colour, va="bottom", ha="left")

    ax2.set_ylabel("measured occupancy (%)")
    ax2.set_ylim(-4, 126)
    ax2.set_yticks([0, 20, 40, 60, 80, 100])
    ax2.set_title("Measured occupancy by assumed continuity class\n"
                  "(dotted lines are the assumptions, not measurements)",
                  fontsize=11, loc="left")
    ax2.grid(alpha=0.25, axis="y", linewidth=0.6)

    fig.suptitle("Channel occupancy measured at one fixed QTH, against "
                 "FCC-record predictions for the same location",
                 fontsize=13, y=0.985)
    fig.text(0.5, 0.925, _conditions_line(conditions), ha="center",
             fontsize=9, color="#333333")
    footer = ("QTH, NOT A SUMMIT — this measures duty cycle per service at "
              "one location with one antenna; it is not a summit field "
              "strength.\n" + _method_line(conditions, threshold_db))
    if prediction is not None:
        footer += "\n" + _provenance_line(prediction)
    fig.text(0.5, 0.028, footer, ha="center", fontsize=7.6, color="#444444")

    if not (intermod or {}).get("supplied"):
        # Stated on the figure, not only in the report: intermod products
        # correlate with predicted-strong emitters and read as carriers, so
        # an unchecked front end inflates occupancy exactly where it makes
        # the model look right.
        fig.text(0.5, 0.108,
                 "No intermod check supplied — front-end products would "
                 "raise occupancy on predicted-strong channels, the "
                 "direction that flatters this comparison.",
                 ha="center", fontsize=7.6, color="#CC0000")

    if conditions.get("averaging_bias") == "pre-threshold":
        fig.text(0.5, 0.075,
                 "WARNING: rtl_power source — levels were averaged before "
                 "thresholding, so intermittent channels read 0% or 100% "
                 "rather than their duty cycle.",
                 ha="center", fontsize=8, color="#CC0000")

    fig.tight_layout(rect=[0, 0.085, 1, 0.915])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def report(rows, busy_unpredicted, controls, conditions, threshold_db,
           prediction=None, intermod=None):
    matched = [r for r in rows if r["matched"] and r["occupancy"] is not None]
    floor = audibility_floor(rows)
    silent = classify_silent(rows, floor)
    residual = fm_path_residual(rows)

    lines = []
    add = lines.append
    add("QTH OCCUPANCY VALIDATION")
    add("=" * 72)
    add(_conditions_line(conditions))
    add(_method_line(conditions, threshold_db))
    if prediction is not None:
        add(_provenance_line(prediction))
        add("  A model change changes every prediction. Predictions from "
            "two engines must")
        add("  not be pooled in one comparison.")
    add("")

    add("CONTROL BANDS -- the calibration for the threshold")
    if not controls:
        add("  none in this dataset. Without a control band, a band that "
            "reads quiet cannot be distinguished from a deaf receive path.")
    for name, checks in controls.items():
        verdicts = [c["verdict"] for c in checks if c]
        best = max((c["max_measured_occupancy"] or 0.0)
                   for c in checks if c)
        add(f"  {name:<12} {verdicts.count('pass')}/{len(verdicts)} pass, "
            f"best channel {100*best:.1f}%")
        if "fail" in verdicts:
            add("    FAILED. A control band below its floor means the "
                "threshold or the receive path is wrong, and every target "
                "number from the same run inherits the fault.")
    add("")

    add(f"PREDICTED CHANNELS: {len(rows)}, of which {len(matched)} were "
        f"observed")
    add("")
    if floor.available:
        add(f"Empirical audibility floor: {floor.describe()}.")
        if floor.kind == "dbm":
            add("  In dBm, from the prediction's own modelled level. Less "
                "frequency-blind")
            add("  than the ERP/d^2 proxy -- a matched antenna's capture "
                "area grows as")
            add("  lambda^2, about 9.5 dB between 150 and 450 MHz, which "
                "the proxy cannot")
            add("  see -- but NOT frequency-correct: p_dbm_ref is at a "
                "reference gain, not")
            add("  this antenna's. An order of magnitude, not a threshold: "
                "the controls")
            add("  are high broadcast sites and land mobile is shadowed "
                "differently.")
        else:
            add("  From ERP/d^2, which is frequency-blind; a /2 prediction "
                "would give a")
            add("  floor in dBm instead. An order of magnitude, not a "
                "threshold.")
    else:
        add("NO empirical audibility floor: no control channel was both "
            "predicted and")
        add("  measured active, so the list below is UNFILTERED and will "
            "include every")
        add("  distant low-power licence that nothing could hear.")
    add("")

    three_way = floor.kind == "dbm"
    add("PREDICTED BUT NEVER ACTIVE"
        + ("  (three-way, against each channel's own error band)"
           if three_way else "  (two-way: no error band in a /1 prediction)"))
    order = ([SILENT_ANOMALY, SILENT_UNDETERMINED, SILENT_EXPECTED]
             if floor.available else [SILENT_UNFILTERED])
    for cls in order:
        group = silent[cls]
        add(f"  {cls.upper()}: {len(group)} -- {SILENT_MEANING[cls]}")
        if cls == SILENT_EXPECTED:
            continue            # counted, not listed: it is the boring class
        for r in group[:12]:
            margin = r.get("margin_db")
            level = r.get("p_dbm_ref")
            proxy = r.get("field_proxy_w_per_m2")
            if level is not None:
                shown = f"{level:7.1f} dBm"
                if margin is not None:
                    shown += f" ({margin:+5.1f} vs floor, +/-" \
                             f"{r.get('error_db') or 0:.0f})"
            else:
                shown = (f"proxy {'n/a' if not proxy else f'{proxy:.2e}'}")
            add(f"    {r['freq_hz']/1e6:10.4f} MHz  {r['continuity']:<12} "
                f"{shown:<34} {r['service']:<5} {r['owner'][:26]}")
        if len(group) > 12:
            add(f"    ... and {len(group) - 12} more")
    add("")

    add("Busy with NO MATCHING FCC RECORD -- worth knowing about:")
    if not busy_unpredicted:
        add("  none")
    for m in busy_unpredicted[:15]:
        add(f"  {m['freq_hz']/1e6:10.4f} MHz  occupancy "
            f"{100*m['occupancy']:5.1f}%  n={m['n_decided']}  "
            f"band {m['band']}")
    add("")

    add("FM RESIDUAL BY PATH CLASS -- the discriminating test")
    add("  Measured excess-dB median minus the model's p_dbm_ref, for "
        "control carriers")
    add("  that were on continuously. The absolute offset is this receive "
        "chain's")
    add("  unknown constant and means NOTHING. The two path classes must "
        "share it if")
    add("  the diffraction model is right, so a gap between them is "
        "path-model error.")
    if not residual["runnable"]:
        add(f"  CANNOT RUN: {residual['reason']}")
        for name, st in residual["classes"].items():
            add(f"    {name:<12} n={st['n']:3d}  median "
                f"{st['median_db']:+7.1f} dB")
    else:
        for name, st in residual["classes"].items():
            add(f"    {name:<12} n={st['n']:3d}  median "
                f"{st['median_db']:+7.1f} dB  IQR {st['iqr_db'][0]:+.1f} "
                f"to {st['iqr_db'][1]:+.1f}")
        gap = residual["gap_db"]
        spread = residual["spread_db"]
        add(f"    gap {gap:+.1f} dB against a within-class IQR of "
            f"{spread:.1f} dB")
        if residual["gap_exceeds_spread"]:
            add("    The gap EXCEEDS the scatter it sits in. Read as "
                "path-model error:")
            add("    a positive gap means diffraction loss was "
                "overestimated. Confirm by")
            add("    repeating at a different antenna height before "
                "acting on it.")
        else:
            add("    The gap is SMALLER than the scatter it sits in, so it "
                "is not")
            add("    evidence of anything. Consistent with the diffraction "
                "model, which")
            add("    is not the same as support for it.")
    add("")

    intermod = intermod or {"supplied": False, "bands": {}}
    add("FRONT-END INTERMOD -- the error that FLATTERS this comparison")
    if not intermod.get("supplied"):
        add("  NO INTERMOD CHECK SUPPLIED. Products land at frequencies set "
            "by the")
        add("  STRONG emitters, so they correlate with the channels this "
            "prediction")
        add("  calls strong, and they read as carriers. An overloaded front "
            "end")
        add("  therefore RAISES measured occupancy on predicted-strong "
            "channels --")
        add("  the direction that makes the model look right. Nothing "
            "below rules")
        add("  that out. Run `--intermod-check --intermod-json FILE` and "
            "pass it as")
        add("  --intermod-report.")
    else:
        for band, b in sorted(intermod["bands"].items()):
            add(f"  {band:<14} {b['steps']} step(s), "
                f"{b['assessable']} assessable, {b['suspect']} suspect, "
                f"{b['unexplained']} too steep, {b['unstable']} unstable")
            if b["suspect"] or b["unexplained"]:
                add("    NOT shown clean. Occupancy in this band may be "
                    "inflated by products.")
        covered = set(intermod["bands"])
        bands_here = {r.get("band") for r in matched if r.get("band")}
        missing = sorted(bands_here - covered)
        if missing:
            add(f"  NOT CHECKED: {', '.join(missing)} -- those bands' "
                f"numbers are unverified.")
    add("")

    add("MEASURED DUTY CYCLE BY CONTINUITY CLASS")
    add("  These are candidate entries for the empty `measured` column of")
    add("  hp8935_sinad_serial/docs/reference/duty_cycles.md. They carry")
    add("  their acquisition conditions above and are not valid without "
        "them.")
    for cont in ("continuous", "intermittent", "unknown"):
        vals = [r["occupancy"] for r in matched if r["continuity"] == cont]
        if not vals:
            continue
        a = np.asarray(vals, dtype=float)
        assumed = {"continuous": "100%", "intermittent": "10-25%"}.get(
            cont, "n/a")
        add(f"  {cont:<13} n={a.size:3d}  median {100*np.median(a):5.1f}%  "
            f"mean {100*a.mean():5.1f}%  "
            f"IQR {100*np.percentile(a,25):.1f}-"
            f"{100*np.percentile(a,75):.1f}%   (assumed {assumed})")
    add("")
    add("WHICH STEP IS MOST LIKELY TO BE WRONG")
    add("  1. Schedule aliasing against traffic periodicity. The catch-")
    add("     probability bounds assume visit phases are independent of what")
    add("     the transmitter is doing. Settle it by rerunning with a")
    add("     different jitter seed and nominal revisit period; the")
    add("     estimates should agree within the error bars.")
    if floor.kind == "dbm":
        add("  2. The floor's transfer from broadcast FM to land mobile. "
            "p_dbm_ref")
        add("     is frequency-aware but computed at a reference gain, not "
            "this")
        add("     antenna's, and the controls are high broadcast sites. "
            "Settle it")
        add("     by adding a second control on a different band.")
    else:
        add("  2. The uncalibrated ERP/d^2 proxy. It has no terrain, "
            "antenna")
        add("     pattern or feedline loss in it, so the left panel's x "
            "axis is")
        add("     an ordering and a weak one. A predicted-strong channel "
            "reading")
        add("     0% may be a terrain shadow rather than a bad licence "
            "record.")
    add("  3. Channel grid offsets. A carrier offset from the uniform grid")
    add("     is split across two channels and both read low.")
    for w in conditions.get("warnings", []):
        add(f"  ACQUISITION WARNING: {w}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate measured occupancy against sota-rf's "
                    "prediction for the same fixed location.")
    ap.add_argument("--prediction", required=True,
                    help="prediction.json from tools/qth_predict.py")
    ap.add_argument("datasets", nargs="+", help="occ_*.json.gz files")
    ap.add_argument("--threshold-db", type=float,
                    help="re-threshold the stored histograms instead of "
                         "using each file's default")
    ap.add_argument("--min-decided", type=int, default=100)
    ap.add_argument("--plot", default="./out/qth_validation.png")
    ap.add_argument("--report", default="./out/qth_validation.txt")
    ap.add_argument("--intermod-report", action="append", default=[],
                    metavar="FILE",
                    help="JSON from `python -m occupancy_survey "
                         "--intermod-check --intermod-json FILE`. Repeatable. "
                         "Without it the figure says so, because products "
                         "correlate with predicted-strong emitters and "
                         "inflate occupancy in the direction that flatters "
                         "the model.")
    args = ap.parse_args(argv)

    measured, controls, conditions = load_measured(
        args.datasets, args.threshold_db, args.min_decided)
    prediction = load_prediction(args.prediction)
    rows, busy = join(prediction, measured)

    thr = args.threshold_db
    if thr is None:
        thr = next((m["threshold_db"] for m in measured.values()), 0.0)
    conditions["threshold_db"] = thr

    intermod = load_intermod_reports(args.intermod_report)

    text = report(rows, busy, controls, conditions, thr,
                  prediction=prediction, intermod=intermod)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)

    png = plot(rows, busy, conditions, thr, args.plot,
               prediction=prediction, intermod=intermod)
    print(f"\nwrote {png}\nwrote {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
