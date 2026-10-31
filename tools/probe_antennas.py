"""Compare the receiver's antenna ports, so a dead port is found before a run.

WHY THIS TOOL EXISTS. An antenna port with nothing on it does not produce an
error. It produces a quiet band -- and a quiet band is this project's whole
output, so the failure is indistinguishable from the finding. The same is
true of a port that cannot reach the frequency being surveyed. This tool
turns that silent failure into a loud one by comparing the ports against
each other at the frequencies actually being surveyed.

WHAT IT CAN CONCLUDE. That one port sees structure and another does not, at
a given frequency, right now. That is enough to catch an unplugged cable, a
port that does not cover the band, and a switch that is being ignored.

WHAT IT CANNOT CONCLUDE. Which port is BEST -- that needs a known reference,
which a port comparison does not have. Nor can it distinguish a good antenna
on a lossy cable from a poor antenna on a good one, and it cannot certify a
quiet port as faulty: a genuinely quiet band looks the same. It ranks; it
does not certify.

WHY IT DOES NOT PICK FOR YOU. A survey's antenna port belongs in the config
and in the dataset's receiver metadata, where it is recorded once and stays
put. A tool that chose a port per run would make two runs of the same band
incomparable while reporting both as the same site -- which is the objection
to AGC wearing a different hat. So this prints a recommendation and changes
nothing.

LOGS ENERGY, NOT CONTENT: power spectra only, as everywhere else here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# How far above the floor a bin must sit to be counted as structure. Well
# clear of the ~5 dB excursions a Blackman-Harris noise floor produces on
# its own at these integration times.
PEAK_THRESHOLD_DB = 10.0

# Below this much peak excess, a port is reported as showing no structure.
# Inferred from the noise-floor statistics above, not measured against a
# known-dead port -- the number to revisit first if this tool misjudges.
STRUCTURE_MIN_EXCESS_DB = 8.0


def describe(spectra: list[np.ndarray], clips: list[float]) -> dict:
    """Reduce a port's sweeps to the few numbers that discriminate."""
    if not spectra:
        return {"sweeps": 0}
    stack = np.vstack(spectra)
    # Per-bin mean across sweeps, then statistics across bins: the floor is
    # a property of the band, not of one moment in it.
    per_bin = stack.mean(axis=0)
    floor = float(np.median(per_bin))
    p999 = float(np.percentile(per_bin, 99.9))
    n_peaks = int(np.count_nonzero(per_bin > floor + PEAK_THRESHOLD_DB))
    clip_vals = [c for c in clips if c is not None]
    return {
        "sweeps": len(spectra),
        "floor_db": floor,
        "p99_9_db": p999,
        # The discriminator. An unterminated port is FLAT: its floor can sit
        # anywhere, but nothing sticks out of it.
        "peak_excess_db": p999 - floor,
        "bins_above_floor": n_peaks,
        "bins": int(per_bin.size),
        "clip_fraction_max": (max(clip_vals) if clip_vals else None),
        "sees_structure": (p999 - floor) >= STRUCTURE_MIN_EXCESS_DB,
    }


def probe(backend, ports, freqs_hz, span_hz, dwell_s) -> list[dict]:
    rows = []
    for hz in freqs_hz:
        backend.tune(hz, span_hz)
        for want in ports:
            got = backend.set_antenna_port(want)
            spectra, clips = [], []
            for sweep in backend.sweeps(dwell_s):
                spectra.append(np.asarray(sweep.spectrum_db, dtype=np.float64))
                clips.append(sweep.clip_fraction)
            row = {"freq_hz": float(hz), "port_requested": want,
                   "port_reported": got, **describe(spectra, clips)}
            if got != want:
                row["warning"] = ("the device reports a different port than "
                                  "was requested; the switch was ignored")
            rows.append(row)
    return rows


def report(rows) -> None:
    hdr = (f"{'freq':>10}  {'port':<12} {'floor':>8} {'peak-excess':>12} "
           f"{'bins>fl':>8} {'clip':>9}  structure")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if not r.get("sweeps"):
            print(f"{r['freq_hz']/1e6:10.3f}  {r['port_requested']:<12} "
                  f"{'NO SWEEPS -- port produced no samples':>40}")
            continue
        clip = r["clip_fraction_max"]
        print(f"{r['freq_hz']/1e6:10.3f}  {r['port_reported']:<12} "
              f"{r['floor_db']:8.1f} {r['peak_excess_db']:12.1f} "
              f"{r['bins_above_floor']:8d} "
              f"{('n/a' if clip is None else f'{clip:.1e}'):>9}  "
              f"{'yes' if r['sees_structure'] else 'NO'}")
        if r.get("warning"):
            print(f"{'':12}  !! {r['warning']}")

    print()
    by_freq: dict[float, list[dict]] = {}
    for r in rows:
        by_freq.setdefault(r["freq_hz"], []).append(r)

    for hz, group in sorted(by_freq.items()):
        usable = [r for r in group if r.get("sees_structure")]
        dead = [r for r in group if r.get("sweeps") and
                not r.get("sees_structure")]
        print(f"{hz/1e6:.3f} MHz:")
        if not usable:
            print("  no port showed structure. Either nothing is connected, "
                  "or this band really is quiet -- this tool cannot tell "
                  "those apart. Check the cable before believing the band.")
        else:
            best = max(usable, key=lambda r: r["peak_excess_db"])
            print(f"  {len(usable)} port(s) see structure; most is "
                  f"{best['port_reported']} at "
                  f"{best['peak_excess_db']:.1f} dB of peak excess.")
            print("  'Most structure' is not 'best antenna': that needs a "
                  "reference this comparison does not have.")
        for r in dead:
            print(f"  {r['port_reported']}: flat "
                  f"({r['peak_excess_db']:.1f} dB excess) -- unterminated, "
                  f"or it does not cover this frequency.")
        hot = [r for r in group if (r.get("clip_fraction_max") or 0.0) > 1e-4]
        for r in hot:
            print(f"  {r['port_reported']}: clipping "
                  f"({r['clip_fraction_max']:.1e}). Fit the band's bandpass, "
                  f"enable the notches, then reduce gain -- in that order.")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compare antenna ports at the surveyed frequencies. "
                    "Opens the SDR; changes no configuration.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--freq-mhz", action="append", type=float, default=None,
                    help="probe frequency; repeatable. Default: each "
                         "configured band's centre.")
    ap.add_argument("--port", action="append", default=None,
                    help="port to test; repeatable. Default: every port the "
                         "device reports.")
    ap.add_argument("--dwell", type=float, default=2.0,
                    help="seconds per (frequency, port) (default 2)")
    ap.add_argument("--json", dest="json_out", default="",
                    help="also write the raw numbers here")
    args = ap.parse_args(argv)

    from occupancy_survey.config import AppConfig
    from occupancy_survey.__main__ import _build_backend

    cfg = AppConfig.from_yaml(args.config)
    bands = cfg.band_plans()
    freqs = ([f * 1e6 for f in args.freq_mhz] if args.freq_mhz
             else [b.center_hz for b in bands])

    backend = _build_backend(cfg)
    with backend:
        ports = args.port or backend.antenna_ports()
        if not ports:
            print("this receiver reports no selectable antenna ports; "
                  "nothing to compare.", file=sys.stderr)
            return 0
        print(f"device reports ports: {', '.join(ports)}")
        print(f"configured for survey: {cfg.sdr.antenna_port}\n")
        rows = probe(backend, ports, freqs,
                     cfg.sampling.usable_bandwidth_hz, args.dwell)

    report(rows)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "schema": "occupancy-survey/antenna-probe/1",
            "configured_port": cfg.sdr.antenna_port,
            "peak_threshold_db": PEAK_THRESHOLD_DB,
            "structure_min_excess_db": STRUCTURE_MIN_EXCESS_DB,
            "caveat": "Ranks ports against each other at one moment. Cannot "
                      "identify the best antenna, and cannot distinguish a "
                      "dead port from a quiet band.",
            "rows": rows,
        }, indent=1), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
