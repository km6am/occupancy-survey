"""Per summit, which listed channels are used, and what fraction of the time.

THE QUESTION. Not "how busy is VHF around here" but, for the channels a
summit's RF list names, two separate things:

  TIER 1  Is this channel used at all?
  TIER 2  If so, what fraction of the time, with an honest interval?

They are separate because they fail differently. For an activator a channel
busy 5% of the time is something, and a single pooled percentage hides it
among hundreds of empty grid cells. And "not heard" is only a finding when
the observation could have heard it: a weak distant licence that this
receiver cannot reach says nothing about the summit, which is not this QTH.

TIER 1 VERDICTS

  spur          within half a channel of a known receiver spur. Measured
                2026-11-01: 151.200, 152.400, 157.200 and 456.000 MHz read
                the same with no antenna. Cannot be assessed here.
  used          occupancy >= USED_MIN_OCC, in >= USED_MIN_RUNS separate
                transmissions, for >= USED_MIN_SWEEPS sweeps. The occupancy
                bar is ~14x what empty channels read at this site (7e-5 at
                10 dB) and far above the thermal model, so a channel used 1%
                of the time clears it. Flagged `level-marginal` when most
                detections are within --robust-db of the threshold -- the
                channel is used, but its duty is threshold-sensitive.
  trace         seen, but not enough to call.
  not heard     nothing over V independent looks. Each look catches a
                channel with probability >= its duty whatever the burst
                length, so duty < 1 - 0.05^(1/V) at 95%. Stated per channel.
  inconclusive  not heard, and predicted weaker than the weakest FM control
                this receiver demonstrably heard (an order-of-magnitude
                floor; see validate_occupancy.empirical_audibility_floor).

TIER 2 is duty over the selected hours with a Wilson 95% interval, using
schedule.effective_independent_samples(): sweeps inside a transmission are
not independent, and a naive binomial interval would be several times too
narrow.

Duty transfers between locations; field strength does not. A channel used
18% of the time here is used 18% at the summit, if it is the same
transmitter. That is the premise this report rests on.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

USED_MIN_OCC = 1e-3
USED_MIN_RUNS = 2
USED_MIN_SWEEPS = 10
ROBUST_DB = 4.0
MARGINAL_RATIO = 0.5
# Receiver spurs measured on the CM87 RSPdx, 2026-11-01 (open-port and
# RF-step tests, out/lab/PLAN.md F5/F13). Receiver-specific: pass --spurs
# for another receiver, or --spurs none.
DEFAULT_SPURS_MHZ = (151.2, 152.4, 157.2, 456.0)


def _expand(patterns):
    out = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        out.extend(hits if hits else [p])
    return out


def wilson(p: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def not_heard_bound(visits: float, confidence: float = 0.95) -> float:
    """Largest duty that V looks could all have missed."""
    if visits < 1:
        return 1.0
    return 1.0 - (1.0 - confidence) ** (1.0 / visits)


class Pooled:
    """One listed channel pooled over the intervals that observed it."""

    def __init__(self, n_bins: int, lo: float, step: float):
        self.hist = np.zeros(n_bins)
        self.under = self.over = 0
        self.n_decided = 0
        self.runs = 0
        self.run_sweeps = 0.0
        self.visits = 0.0
        self.lo, self.step = lo, step
        self.match = None
        self.band = None

    def add(self, rec: dict, sweeps_per_visit: float):
        self.hist += np.asarray(rec["hist"], float)
        self.under += rec.get("underflow", 0)
        self.over += rec.get("overflow", 0)
        self.n_decided += rec["n_decided"]
        runs = rec.get("runs") or {}
        self.runs += runs.get("n", 0)
        self.run_sweeps += runs.get("n", 0) * runs.get("mean_sweeps", 0.0)
        self.visits += rec["n_decided"] / max(sweeps_per_visit, 1.0)

    def occ_at(self, t: float) -> float:
        if not self.n_decided:
            return float("nan")
        k = max(0, int(math.ceil((t - self.lo) / self.step)))
        return (self.hist[k:].sum() + self.over) / self.n_decided

    def present_sweeps(self, t: float) -> float:
        return self.occ_at(t) * self.n_decided

    def p95_db(self) -> float | None:
        tot = self.hist.sum() + self.under + self.over
        if not tot:
            return None
        cum = self.under + np.cumsum(self.hist)
        i = int(np.searchsorted(cum, 0.95 * tot))
        return self.lo + (min(i, len(self.hist) - 1) + 0.5) * self.step


def load_list(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not str(data.get("schema", "")).startswith("occupancy-survey/channel-list"):
        raise SystemExit(f"{path}: not a tools/make_channel_list.py channel list")
    rows = []
    for band, entry in data["bands"].items():
        for c in entry["channels"]:
            rows.append({**c, "band": band, "band_role": entry.get("role")})
    return rows


def pool(datasets, listed, threshold_db):
    """Match every listed channel to measured records and pool them."""
    spec = datasets[0]["acquisition"]["histogram"]
    out = []
    for ch in listed:
        p = Pooled(spec["n_bins"], spec["lo_db"], spec["bin_db"])
        for d in datasets:
            samp = d["acquisition"]["sampling"]
            spv = samp.get("sweeps_per_visit") or (samp["dwell_s"] / samp["integration_s"])
            band = d["bands"].get(ch["band"])
            if band is None:
                continue
            tol = 0.5 * band.get("channel_step_hz", 12_500.0)
            best, bd = None, None
            for rec in band["channels"]:
                dist = abs(rec["freq_hz"] - ch["freq_hz"])
                if dist <= tol and (bd is None or dist < bd):
                    best, bd = rec, dist
            if best is None:
                continue
            p.add(best, spv)
            p.band = ch["band"]
            p.match = "exact" if bd <= 1.0 else f"grid cell {bd/1e3:.2f} kHz off"
        out.append((ch, p))
    return out


def audibility_floor(pooled, threshold_db):
    heard = [ch.get("field_proxy_w_per_m2") for ch, p in pooled
             if ch.get("band_role") == "control" and p.n_decided
             and p.occ_at(threshold_db) > 0.5 and ch.get("field_proxy_w_per_m2")]
    return min(heard) if heard else None


def tune_centres(band_name, sampling) -> list[float]:
    """Tuning centres a grid band was measured at, mirroring tune_steps().

    The receiver's DC spike sits at the centre of each tuning. It is not at
    every one -- measured 2026-11-01, 456.000 had one and 452/460/464/468
    did not -- so this only annotates; the spur list is what excludes.
    """
    from occupancy_survey.channels import BAND_PLANS
    plan = BAND_PLANS.get(band_name)
    usable = (sampling.get("usable_bandwidth_hz") or 0) * (sampling.get("usable_fraction") or 0)
    if plan is None or usable <= 0:
        return []
    n = max(1, math.ceil(plan.span_hz / usable))
    w = plan.span_hz / n
    return [plan.freq_min_hz + (i + 0.5) * w for i in range(n)]


def verdict(ch, p, threshold_db, spurs_hz, floor, integration_s, dwell_s,
            centres=()):
    tol = 0.5 * 12_500.0
    if any(abs(ch["freq_hz"] - s) <= tol for s in spurs_hz):
        return {"tier1": "spur", "why": "within half a channel of a known receiver spur"}
    if not p.n_decided:
        return {"tier1": "not observed", "why": "no dataset covered this channel"}
    out = _verdict(ch, p, threshold_db, floor, integration_s, dwell_s)
    if any(abs(ch["freq_hz"] - c) <= tol for c in centres):
        out["at_tuning_centre"] = True
    return out


def _verdict(ch, p, threshold_db, floor, integration_s, dwell_s):
    occ = p.occ_at(threshold_db)
    present = p.present_sweeps(threshold_db)
    out = {"visits": round(p.visits, 1), "occupancy": occ,
           "present_sweeps": int(round(present)), "transmissions": p.runs,
           "p95_excess_db": p.p95_db(), "match": p.match}
    # Two separate looks saw it. Complete transmissions prove that, but a
    # carrier that never drops HAS no complete transmission -- every run is
    # cut by a retune and discarded -- so presence spanning more sweeps
    # than one visit holds proves it too. Without the second test the
    # report called continuous carriers "trace".
    spv = dwell_s / integration_s
    repeated = p.runs >= USED_MIN_RUNS or present > spv
    if occ >= USED_MIN_OCC and repeated and present >= USED_MIN_SWEEPS:
        hi = p.occ_at(threshold_db + ROBUST_DB)
        ratio = hi / occ if occ else float("nan")
        # Present time per transmission, capped at the dwell. Complete runs
        # alone are biased short (the long ones are cut by retunes), and a
        # short burst inflates the effective sample count and narrows the
        # interval -- so err long.
        mean_burst_s = min(dwell_s, present * integration_s / max(p.runs, 1))
        from occupancy_survey.schedule import effective_independent_samples
        n_eff = effective_independent_samples(dwell_s, integration_s, mean_burst_s,
                                              int(round(p.visits)))
        lo, up = wilson(occ, n_eff)
        out.update(tier1="used", duty=occ, duty_ci95=[lo, up], n_eff=round(n_eff, 1),
                   mean_transmission_s=round(mean_burst_s, 2),
                   robust_ratio=ratio, level_marginal=bool(ratio < MARGINAL_RATIO))
        return out
    if present > 0:
        out.update(tier1="trace",
                   why=f"{int(round(present))} sweep(s) in {p.runs} transmission(s); "
                       "below the bar to call it used")
        return out
    bound = not_heard_bound(p.visits)
    proxy = ch.get("field_proxy_w_per_m2")
    if floor is not None and proxy is not None and proxy < floor:
        out.update(tier1="inconclusive", duty_upper95=bound,
                   why="predicted weaker than the weakest control this receiver heard")
    else:
        out.update(tier1="not heard", duty_upper95=bound)
    return out


def _fmt_pct(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{100*x:.2f}%" if x < 0.1 else f"{100*x:.1f}%"


def render_md(groups, meta):
    lines = [f"# Summit channel occupancy",
             "",
             f"{meta['n_intervals']} interval(s), {meta['first']} to {meta['last']} UTC"
             f"{', local hours ' + meta['hours'] if meta['hours'] else ', all hours'}; "
             f"threshold {meta['threshold_db']:g} dB; spurs excluded: {meta['spurs']}.",
             "",
             "Tier 1 says whether a listed channel is used at all; tier 2 how much, with a "
             "95% interval. `not heard` carries the largest duty the looks could have missed.",
             ""]
    for name, rows in groups:
        lines += [f"## {name}", "",
                  "| MHz | service | owner | tier 1 | tier 2 duty (95% CI) | level p95 | notes |",
                  "|---:|---|---|---|---|---:|---|"]
        for ch, v in rows:
            t1 = v["tier1"]
            if t1 == "used":
                lo, up = v["duty_ci95"]
                t2 = f"{_fmt_pct(v['duty'])} ({_fmt_pct(lo)}–{_fmt_pct(up)})"
                notes = []
                if v["level_marginal"]:
                    notes.append(f"level-marginal ({v['robust_ratio']:.2f} survive +{ROBUST_DB:g} dB)")
                if v["transmissions"]:
                    notes.append(f"{v['transmissions']} tx, mean {v['mean_transmission_s']} s")
                else:
                    notes.append("continuous: never seen to drop within a look")
            elif t1 in ("not heard", "inconclusive"):
                t2 = f"< {_fmt_pct(v['duty_upper95'])}"
                notes = [v.get("why", "")] if v.get("why") else []
                notes.append(f"{v['visits']} looks")
            else:
                t2 = "-"
                notes = [v.get("why", "")]
            if v.get("match") and v["match"] != "exact":
                notes.append(v["match"] + " (may be split)")
            if v.get("at_tuning_centre"):
                notes.append("at a tuning centre: check for the receiver's DC spur")
            lvl = v.get("p95_excess_db")
            lines.append(f"| {ch['freq_hz']/1e6:.4f} | {ch.get('service','')} | "
                         f"{(ch.get('owner') or '')[:28]} | {t1} | {t2} | "
                         f"{'' if lvl is None else f'{lvl:.0f} dB'} | {'; '.join(n for n in notes if n)} |")
        lines.append("")
    return "\n".join(lines)


def build(datasets, listed, threshold_db=None, hours=None, spurs_mhz=DEFAULT_SPURS_MHZ,
          include_all=False):
    from occupancy_survey.dataset import filter_by_local_hour
    if hours:
        a, b = (float(x) for x in hours.split("-"))
        datasets = filter_by_local_hour(datasets, a, b)
        if not datasets:
            raise SystemExit(f"no intervals fall in local hours {hours}")
    ts = {d["acquisition"]["detection"]["threshold_db"] for d in datasets}
    if threshold_db is None:
        if len(ts) != 1:
            raise SystemExit(f"datasets use thresholds {sorted(ts)}; pass --threshold-db")
        threshold_db = ts.pop()
    samp = datasets[0]["acquisition"]["sampling"]
    integ, dwell = samp["integration_s"], samp["dwell_s"]
    spurs_hz = [s * 1e6 for s in spurs_mhz]

    pooled = pool(datasets, listed, threshold_db)
    floor = audibility_floor(pooled, threshold_db)
    by_summit = defaultdict(list)
    for ch, p in pooled:
        tag = ch.get("near_summit")
        if tag:
            key = f"{tag.get('summit')} {tag.get('summit_name') or ''}".strip()
        elif include_all and ch.get("band_role") != "control":
            key = "not near a summit"
        else:
            continue
        by_summit[key].append((ch, verdict(
            ch, p, threshold_db, spurs_hz, floor, integ, dwell,
            tune_centres(ch["band"], samp) if p.band else ())))
    groups = sorted(by_summit.items(), key=lambda kv: (kv[0] == "not near a summit", kv[0]))
    for _, rows in groups:
        rows.sort(key=lambda r: r[0]["freq_hz"])
    meta = {"n_intervals": len(datasets),
            "first": min(d["interval"]["start_utc"] for d in datasets)[:16],
            "last": max(d["interval"]["end_utc"] for d in datasets)[:16],
            "hours": hours, "threshold_db": threshold_db,
            "spurs": ", ".join(f"{s:g}" for s in spurs_mhz) or "none",
            "audibility_floor_proxy": floor}
    return groups, meta


def main(argv=None) -> int:
    from occupancy_survey.dataset import read_dataset

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("datasets", nargs="+", help="occ_*.json.gz files")
    ap.add_argument("--channels", required=True,
                    help="channel list from tools/make_channel_list.py (near_summit tags)")
    ap.add_argument("--hours", help="local hours, e.g. 08-18 for the daytime figure")
    ap.add_argument("--threshold-db", type=float)
    ap.add_argument("--spurs", default=",".join(f"{s:g}" for s in DEFAULT_SPURS_MHZ),
                    help="receiver spur frequencies in MHz, comma-separated, or 'none'")
    ap.add_argument("--all", action="store_true",
                    help="also report listed channels not near any summit")
    ap.add_argument("--out", default="out/summit_report",
                    help="output path stem (.md and .json are written)")
    args = ap.parse_args(argv)

    datasets = [read_dataset(p) for p in _expand(args.datasets)]
    if any(d["acquisition"]["receiver"].get("synthetic") for d in datasets):
        raise SystemExit("refusing synthetic datasets: this report claims measurement")
    spurs = () if args.spurs.strip().lower() == "none" else tuple(
        float(s) for s in args.spurs.split(",") if s.strip())
    groups, meta = build(datasets, load_list(args.channels), args.threshold_db,
                         args.hours, spurs, args.all)
    if not groups:
        print("no listed channel carries a near_summit tag; pass --all to report "
              "every listed channel", file=sys.stderr)
        return 1
    stem = Path(args.out)
    stem.parent.mkdir(parents=True, exist_ok=True)
    md = render_md(groups, meta)
    stem.with_suffix(".md").write_text(md, encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps(
        {"meta": meta, "summits": {k: [{"channel": c, "result": v} for c, v in rows]
                                   for k, rows in groups}},
        indent=1, default=float), encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
