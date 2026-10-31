"""Compare the receiver's antenna ports, so a dead port is found before a run.

WHY THIS TOOL EXISTS. An antenna port with nothing on it does not produce an
error. It produces a quiet band -- and a quiet band is this project's whole
output, so the failure is indistinguishable from the finding. The same is
true of a port that cannot reach the frequency being surveyed. This tool
turns that silent failure into a loud one by comparing the ports against
each other at the frequencies actually being surveyed.

WHAT IT CAN CONCLUDE. Given two probe frequencies far enough apart, which
port has an antenna on it: a connected port's noise floor falls with
frequency and an open port's does not. Given one frequency, only the weaker
statement that a port sees structure -- which an open connector in a strong
signal environment does too, so do not probe one frequency and expect an
answer about cabling.

WHAT IT CANNOT CONCLUDE. Which port is BEST -- that needs a known reference,
which a port comparison does not have. Nor can it distinguish a good antenna
on a lossy cable from a poor antenna on a good one, and it cannot certify a
quiet port as faulty: a genuinely quiet band looks the same. It ranks; it
does not certify.

It also cannot tell you a port's FREQUENCY COVERAGE, and the reason is worth
stating because the output invites the mistake. What the floor reading
measures on an open port is the receiver's own noise, generated downstream of
the antenna switch -- so it reads the same whether that port has a
brick-wall filter on it or nothing at all. An RSPdx Port C that is widely
understood to stop below ~200 MHz looked identical at 162 and 465 MHz here,
which is not evidence either way. Only an antenna on the port settles it.

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

# Above this clip fraction the port's spectrum cannot be assessed at all.
# Matches the threshold the device preflight uses.
#
# WHY THIS GATE EXISTS, learned the hard way: a SATURATED port looks exactly
# like an UNTERMINATED one by peak excess. Both are flat. The first run of
# this tool against a real antenna reported "unterminated, or it does not
# cover this frequency" for a port that was in fact being flattened by
# broadcast FM at 3.8% clipping -- and printed the contradicting clip figure
# on the very next line. The two diagnoses have OPPOSITE remedies (connect
# something vs. reduce gain), so guessing between them is worse than
# refusing to.
SATURATION_CLIP_FRACTION = 1e-4

# --------------------------------------------------------------------------- #
# The discriminator that actually answers "which port has the antenna"
# --------------------------------------------------------------------------- #
# Peak excess, above, answers a narrower question than it looks like it does.
# At one QTH all three ports showed 11-15 dB of excess at 162 MHz and read
# "structure" -- correct, and useless, because an open connector picks up
# strong local signals perfectly well.
#
# What separated them was the NOISE FLOOR ACROSS FREQUENCY. External noise
# falls steeply from VHF to UHF; the receiver's own floor does not move. So
# a port whose floor is the same at 162 MHz and 465 MHz is listening to the
# receiver, and a port whose floor drops 10 dB over that span is listening
# to the world.
#
# Measured at one site, two runs, one antenna:
#     A  -3.7 -> -13.6 dB   spread 9.9 dB    (feedline attached)
#     B  -11.2 -> -12.0 dB  spread 0.8 dB    (open)
#     C  -14.2 -> -14.3 dB  spread 0.1 dB    (open)
# Floors reproduced within 0.6 dB between runs, so the spread is signal and
# not scatter.
#
# THRESHOLD CALIBRATED ON n=1 SITE. The gap it sits in is wide (0.8 vs 9.9)
# but one QTH is one QTH: a quiet rural site, or a port feeding a narrowband
# filtered antenna, could compress a real port's spread toward this bound.
# Revisit this constant before trusting it anywhere else.
FLAT_FLOOR_SPREAD_DB = 3.0

# The spread test is meaningless over a narrow span -- a real antenna's floor
# barely moves between 153 and 162 MHz either. Needs a genuine lever arm.
MIN_FREQ_RATIO_FOR_SPREAD = 1.5


def _structure_cell(value) -> str:
    return {True: "yes", False: "NO", None: "SATURATED"}[value]


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
    clip_max = max(clip_vals) if clip_vals else None
    saturated = clip_max is not None and clip_max > SATURATION_CLIP_FRACTION
    excess = p999 - floor
    return {
        "sweeps": len(spectra),
        "floor_db": floor,
        "p99_9_db": p999,
        # The discriminator. An unterminated port is FLAT: its floor can sit
        # anywhere, but nothing sticks out of it.
        "peak_excess_db": excess,
        "bins_above_floor": n_peaks,
        "bins": int(per_bin.size),
        "clip_fraction_max": clip_max,
        "saturated": saturated,
        # TRI-STATE, and the None matters: a saturated front end is flat for
        # a reason that has nothing to do with what is connected, so the
        # honest answer is "cannot tell", not "no".
        #
        # Note the order. Saturation gates only the NEGATIVE answer, because
        # it is only the negative answer that saturation can counterfeit.
        # Structure that survives a clipping front end is still structure:
        # the port is demonstrably connected, even though every level on it
        # is suspect. Gating the positive answer too would have thrown away
        # a conclusion the data fully supports.
        "sees_structure": (True if excess >= STRUCTURE_MIN_EXCESS_DB
                           else (None if saturated else False)),
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
              f"{_structure_cell(r['sees_structure'])}")
        if r.get("warning"):
            print(f"{'':12}  !! {r['warning']}")

    print()
    by_freq: dict[float, list[dict]] = {}
    for r in rows:
        by_freq.setdefault(r["freq_hz"], []).append(r)

    for hz, group in sorted(by_freq.items()):
        usable = [r for r in group if r.get("sees_structure") is True]
        dead = [r for r in group if r.get("sweeps") and
                r.get("sees_structure") is False]
        blind = [r for r in group if r.get("sees_structure") is None
                 and r.get("sweeps")]
        print(f"{hz/1e6:.3f} MHz:")
        for r in blind:
            print(f"  {r['port_reported']}: SATURATED "
                  f"(clip {r['clip_fraction_max']:.1e}, floor "
                  f"{r['floor_db']:.1f} dB) -- flat because the front end is "
                  f"compressed, NOT because nothing is connected. Fit the "
                  f"band's bandpass, enable the notches, then reduce gain -- "
                  f"in that order. Re-probe before judging this port.")
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
        for r in group:
            # Clipping while still resolving structure: the levels are
            # already suspect even though the port is clearly alive.
            if r.get("saturated") and r.get("sees_structure") is not None:
                print(f"  {r['port_reported']}: clipping "
                      f"({r['clip_fraction_max']:.1e}) -- every level on "
                      f"this port is suspect until the gain is fixed.")
        print()



def port_verdicts(rows) -> dict:
    """Per-port judgement across frequencies: is anything connected?

    Returns {} when the probed frequencies are too close together to tell,
    rather than returning a confident answer from no lever arm.
    """
    freqs = sorted({r["freq_hz"] for r in rows if r.get("sweeps")})
    if len(freqs) < 2 or freqs[0] <= 0:
        return {}
    ratio = freqs[-1] / freqs[0]
    if ratio < MIN_FREQ_RATIO_FOR_SPREAD:
        return {}

    out = {}
    for r in rows:
        if not r.get("sweeps") or r.get("saturated"):
            # A saturated row's floor is a gain artefact, not a noise floor;
            # including it would invent a spread out of the compression.
            continue
        out.setdefault(r["port_reported"], []).append(r)

    verdicts = {}
    for port, group in out.items():
        floors = [r["floor_db"] for r in group]
        if len(floors) < 2:
            continue
        spread = max(floors) - min(floors)
        verdicts[port] = {
            "floor_spread_db": spread,
            "floor_min_db": min(floors),
            "floor_max_db": max(floors),
            "freq_ratio": ratio,
            "n_frequencies": len(floors),
            "likely_connected": spread >= FLAT_FLOOR_SPREAD_DB,
        }
    return verdicts


def report_ports(verdicts, configured) -> None:
    if not verdicts:
        print("port verdict: not attempted -- needs two probe frequencies at "
              f"least {MIN_FREQ_RATIO_FOR_SPREAD:g}x apart. Re-run with e.g. "
              "--freq-mhz 162.475 --freq-mhz 465")
        return

    print("port verdict, from noise floor across frequency:")
    connected = [p for p, v in verdicts.items() if v["likely_connected"]]
    for port, v in sorted(verdicts.items()):
        verdict = ("probably CONNECTED" if v["likely_connected"]
                   else "probably OPEN (receiver noise only)")
        print(f"  {port:<12} floor {v['floor_max_db']:6.1f} -> "
              f"{v['floor_min_db']:6.1f} dB, spread "
              f"{v['floor_spread_db']:5.1f} dB over {v['freq_ratio']:.1f}x "
              f"-- {verdict}")
    print("  Reasoning: external noise falls steeply with frequency; the "
          "receiver's own floor does not. A flat floor is the receiver "
          "listening to itself.")
    print(f"  Inferred, not certified, and the {FLAT_FLOOR_SPREAD_DB:g} dB "
          f"bound is calibrated on one site. A lossy or narrowband-filtered "
          f"feedline could flatten a real port too.")

    if len(connected) == 1 and connected[0] != configured:
        print(f"\n  !! config is set to {configured}, but {connected[0]} is "
              f"the one that looks connected. Nothing here changes your "
              f"config; check the cabling and edit antenna_port yourself.")
    elif configured in verdicts and not verdicts[configured]["likely_connected"]:
        print(f"\n  !! the configured port {configured} looks OPEN. A survey "
              f"on it would report quiet bands that are not quiet.")
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
    verdicts = port_verdicts(rows)
    report_ports(verdicts, cfg.sdr.antenna_port)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "schema": "occupancy-survey/antenna-probe/1",
            "configured_port": cfg.sdr.antenna_port,
            "peak_threshold_db": PEAK_THRESHOLD_DB,
            "structure_min_excess_db": STRUCTURE_MIN_EXCESS_DB,
            "caveat": "Ranks ports against each other at one moment. Cannot "
                      "identify the best antenna, and cannot distinguish a "
                      "dead port from a quiet band.",
            "flat_floor_spread_db": FLAT_FLOOR_SPREAD_DB,
            "rows": rows,
            "port_verdicts": verdicts,
        }, indent=1), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
