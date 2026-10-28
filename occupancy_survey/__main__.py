"""`python -m occupancy_survey` -- run the survey.

Deliberately does not open a device as a side effect of anything. The
backend is constructed only when a run starts, and `--plan` prints the
sampling design and its bounds without touching hardware at all, which is
the right first command on a new configuration.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from occupancy_survey.channels import resolve_bands
from occupancy_survey.config import AppConfig
from occupancy_survey.schedule import Schedule, blind_spot_table
from occupancy_survey.survey import Survey

logger = logging.getLogger("occupancy_survey")


def _build_backend(cfg: AppConfig):
    backend = cfg.sdr.backend.lower().strip()

    if backend == "synthetic":
        from occupancy_survey.backends.synthetic import SyntheticBackend
        logger.warning(
            "synthetic backend: NO HARDWARE. The dataset is marked "
            "synthetic and must not be reported as occupancy for this site.")
        return SyntheticBackend(sample_rate_hz=cfg.sdr.sample_rate_hz,
                                fft_size=cfg.sdr.fft_size,
                                integration_s=cfg.sampling.integration_s)

    if backend in ("rtl_sdr", "rtlsdr", "rtl_sdr_pipe"):
        from occupancy_survey.backends.rtl_sdr_pipe import RtlSdrPipeBackend
        return RtlSdrPipeBackend(sample_rate_hz=cfg.sdr.sample_rate_hz,
                                 gain_db=cfg.sdr.gain,
                                 fft_size=cfg.sdr.fft_size,
                                 integration_s=cfg.sampling.integration_s,
                                 device_index=cfg.sdr.device_index,
                                 ppm=cfg.sdr.ppm)

    if backend in ("gnuradio", "gr", "rspdx"):
        from occupancy_survey.backends.rspdx_gr import GnuRadioBackend
        return GnuRadioBackend(cfg.sdr, fft_size=cfg.sdr.fft_size,
                               integration_s=cfg.sampling.integration_s)

    raise ValueError(
        f"unknown backend '{cfg.sdr.backend}'. One of: synthetic, rtl_sdr, "
        f"gnuradio. (rtl_power CSV is ingested with "
        f"tools/ingest_rtl_power.py, not run live.)"
    )


def _predicted_strongest(path) -> dict:
    """Per band, the channel the prediction says should be loudest.

    Ranks on the uncalibrated ERP/d^2 proxy, which is an ORDERING and not a
    level -- which is all this check needs, since it compares ranks.
    """
    import json
    from pathlib import Path as _Path

    data = json.loads(_Path(path).read_text(encoding="utf-8"))
    if not str(data.get("schema", "")).startswith(
            "occupancy-survey/qth-prediction"):
        raise ValueError(f"{path}: not a qth_predict.py prediction")

    best: dict[str, tuple[float, float]] = {}
    for ch in data.get("channels", []):
        proxy = ch.get("field_proxy_w_per_m2")
        if proxy is None:
            continue
        band = ch.get("band")
        if band not in best or proxy > best[band][1]:
            best[band] = (float(ch["freq_hz"]), float(proxy))
    return {b: hz for b, (hz, _p) in best.items()}


def _print_plan(cfg: AppConfig) -> None:
    bands = cfg.band_plans()
    sched = Schedule(bands, cfg.sampling)
    summary = sched.design_summary()

    print(f"site           {cfg.site.site_id}  "
          f"({cfg.site.grid_square or 'location withheld'})")
    print(f"receiver       {cfg.site.receiver_model} / {cfg.site.antenna}")
    print(f"backend        {cfg.sdr.backend}")
    print(f"threshold      {cfg.detection.threshold_db:.1f} dB above a "
          f"{cfg.detection.baseline_percentile:.0f}th-percentile baseline")
    print()
    print(f"{'band':<14} {'role':<8} {'span/MHz':>9} {'steps':>6} "
          f"{'channels':>9}")
    for b in bands:
        from occupancy_survey.schedule import tune_steps
        print(f"{b.name:<14} {b.role:<8} {b.span_hz/1e6:9.3f} "
              f"{len(tune_steps(b, cfg.sampling)):6d} "
              f"{len(b.channels()):9d}")
    print()
    print(f"cycle time     {summary['cycle_time_s']:.0f} s "
          f"({summary['tune_steps']} tune steps x "
          f"{cfg.sampling.dwell_s:.0f} s dwell)")
    print(f"visits/interval {summary['visits_per_band_per_interval']} per "
          f"tune step per {cfg.sampling.interval_s/60:.0f} min")
    print(f"blind spot     bursts shorter than "
          f"{summary['min_detectable_burst_s_at_6db_margin']*1000:.0f} ms "
          f"are not reliably seen")

    from occupancy_survey.detector import (false_alarm_rate,
                                            occupancy_floor_warning,
                                            required_snr_db)
    print()
    print("per band: SNR a continuous carrier needs, and the occupancy an "
          "EMPTY channel reads")
    warnings_seen = []
    for b in bands:
        bin_hz = cfg.sdr.sample_rate_hz / cfg.sdr.fft_size
        n_bins = max(1, int(b.channel_bandwidth_hz / bin_hz))
        edge = required_snr_db(cfg.detection.threshold_db, n_bins,
                               cfg.detection.baseline_percentile)
        floor = false_alarm_rate(cfg.detection.threshold_db, n_bins,
                                 cfg.detection.baseline_percentile)
        print(f"  {b.name:<14} {n_bins:4d} bins/channel -> "
              f"{edge:5.1f} dB SNR, noise floor {100*floor:7.4f}%")
        w = occupancy_floor_warning(cfg.detection.threshold_db, n_bins,
                                    cfg.detection.baseline_percentile)
        if w:
            warnings_seen.append(f"{b.name}: {w}")

    # Bands whose absence would indict the receiver need enough looks for
    # that to mean anything, and how many is a property of THIS schedule.
    presence_bands = [b for b in bands
                      if getattr(b, "presence", None)
                      and b.presence.enabled]
    if presence_bands:
        from occupancy_survey.presence import min_detectable_duty
        from occupancy_survey.schedule import tune_steps as _ts
        print()
        print("presence reference (absence here indicts the RECEIVER):")
        for b in presence_bands:
            steps = len(_ts(b, cfg.sampling))
            per_cycle = steps * cfg.sampling.dwell_s
            per_hour = (per_cycle * 3600.0 / summary["cycle_time_s"]
                        if summary["cycle_time_s"] else 0.0)
            visits_h = (steps * 3600.0 / summary["cycle_time_s"]
                        if summary["cycle_time_s"] else 0.0)
            print(f"  {b.name}: {per_hour/60:.1f} min of coverage per hour "
                  f"({visits_h:.0f} visits/h)")
            print(f"    {'run':>6}  {'visits':>7}  "
                  f"detectable duty cycle (95% conf)")
            for hours in (1, 2, 4, 8, 12):
                n = max(1, int(visits_h * hours))
                d = min_detectable_duty(n, cfg.sampling.dwell_s,
                                        b.presence.cycle_s_max)
                verdict = ("absence proves nothing" if d >= 1.0
                           else f">= {100*d:.1f}%")
                print(f"    {hours:4d} h  {n:7d}  {verdict}")
            print("    Until that figure is small, 'nothing seen' is "
                  "ambiguous between a")
            print("    dead receive path and a carrier that was simply off "
                  "when you looked.")

    print()
    print("catch probability for a periodic transmission:")
    rows = blind_spot_table(cfg.sampling,
                            summary["visits_per_band_per_interval"])
    print(f"  {'burst':>7} {'period':>8} {'duty':>7} {'P(seen)':>8}  note")
    for r in rows:
        note = "" if r["detectable"] else "below the integration floor"
        print(f"  {r['burst_s']:6.2f}s {r['period_s']:7.0f}s "
              f"{r['implied_duty']*100:6.2f}% {r['catch_probability']:8.3f}  "
              f"{note}")

    for w in warnings_seen:
        print(f"\nWARNING: {w}")
    for w in summary["warnings"]:
        print(f"\nWARNING: {w}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m occupancy_survey",
        description="Log per-channel carrier occupancy, band by band.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--backend", help="override sdr.backend")
    ap.add_argument("--bands", help="comma-separated band names to override "
                                    "the configured schedule")
    ap.add_argument("--prediction", metavar="JSON",
                    help="qth_predict.py prediction, used to name the "
                         "channel that should be STRONGEST in each "
                         "presence-reference band. Turns 'nothing seen' "
                         "into three distinguishable outcomes.")
    ap.add_argument("--channels", metavar="JSON",
                    help="targeted channel list from "
                         "tools/make_channel_list.py, used instead of the "
                         "uniform grids. Exact FCC frequencies, far smaller "
                         "files -- but it cannot find a busy channel with "
                         "no FCC record, so keep a grid run alongside it.")
    ap.add_argument("--out-dir", help="override output.dir")
    ap.add_argument("--cycles", type=int, help="stop after N full passes")
    ap.add_argument("--duration", type=float, metavar="S",
                    help="stop after S seconds")
    ap.add_argument("--intermod-check", action="store_true",
                    help="paired-gain diagnostic: does this front end "
                         "generate its own signals? Opens the SDR.")
    ap.add_argument("--gain-step-db", type=float, default=6.0,
                    help="gain reduction for --intermod-check (default 6)")
    ap.add_argument("--check", action="store_true",
                    help="is this host ready? Verifies imports, binaries, "
                         "config, disk and clock. Does NOT open the SDR "
                         "unless --device is also given.")
    ap.add_argument("--device", action="store_true",
                    help="with --check, also open the SDR and take one "
                         "sweep")
    ap.add_argument("--plan", action="store_true",
                    help="print the sampling design and its bounds, then "
                         "exit. Touches no hardware.")
    ap.add_argument("--json", action="store_true",
                    help="with --plan, emit the design as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s")

    try:
        cfg = AppConfig.from_yaml(args.config)
    except FileNotFoundError:
        print(f"no config at {args.config}. Copy config.example.yaml and "
              f"edit the site block.", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.backend:
        cfg.sdr.backend = args.backend
    if args.bands:
        cfg.bands = tuple(b.strip() for b in args.bands.split(",") if b.strip())
        try:
            resolve_bands(cfg.bands)
        except ValueError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
    if args.out_dir:
        cfg.out_dir = args.out_dir
    if args.channels:
        from occupancy_survey.channels import load_channel_list
        try:
            cfg.targeted_bands = load_channel_list(args.channels)
        except (ValueError, OSError) as e:
            print(f"channel list error: {e}", file=sys.stderr)
            return 2
        logger.warning(
            "targeted channel list: %d channels. This CANNOT find a busy "
            "channel with no FCC record -- run a grid schedule alongside it "
            "if that matters.",
            sum(len(b.channels()) for b in cfg.targeted_bands))

    if args.check:
        from occupancy_survey.preflight import run_preflight
        pf = run_preflight(cfg, probe_device=args.device)
        print(f"preflight on {cfg.site.site_id} "
              f"({cfg.sdr.backend} backend)")
        print(pf.report())
        print()
        if pf.failures:
            print(f"NOT READY: {len(pf.failures)} check(s) failed.")
            return 6
        if pf.untested:
            print("Ready as far as this can tell. Not tested: "
                  + ", ".join(c.name for c in pf.untested) + ".")
        else:
            print("Ready.")
        return 0

    if args.plan:
        if args.json:
            print(json.dumps(
                Schedule(cfg.band_plans(), cfg.sampling).design_summary(),
                indent=2))
        else:
            _print_plan(cfg)
        return 0

    if args.cycles is None and args.duration is None:
        logger.info("no --cycles or --duration: running until interrupted "
                    "(Ctrl-C finalises the interval in progress)")

    try:
        backend = _build_backend(cfg)
    except (ValueError, ImportError) as e:
        print(f"backend error: {e}", file=sys.stderr)
        return 3

    predicted_strongest = {}
    if args.prediction:
        try:
            predicted_strongest = _predicted_strongest(args.prediction)
        except (ValueError, OSError, KeyError) as e:
            print(f"prediction error: {e}", file=sys.stderr)
            return 2
        for band, hz in sorted(predicted_strongest.items()):
            logger.info("predicted strongest in %s: %.4f MHz", band, hz / 1e6)

    with backend:
        survey = Survey(cfg.band_plans(), backend, cfg.site, cfg.sampling,
                        cfg.detection, cfg.histogram, out_dir=cfg.out_dir,
                        predicted_strongest=predicted_strongest)

        if args.intermod_check:
            from occupancy_survey.schedule import tune_steps
            any_run = False
            for band in cfg.band_plans():
                for step in tune_steps(band, cfg.sampling):
                    out = survey.intermod_check(
                        band, step, gain_step_db=args.gain_step_db)
                    if out is None:
                        print(f"{band.name}: backend cannot change gain; "
                              f"diagnostic not run", file=sys.stderr)
                        continue
                    any_run = True
                    print(f"\n{band.name} @ {step.center_hz/1e6:.3f} MHz "
                          f"({out['channels_assessable']} assessable, "
                          f"{out['channels_suspect']} suspect)")
                    for c in out["suspect_channels"][:10]:
                        print(f"    {c['freq_hz']/1e6:10.4f} MHz  order "
                              f"{c['implied_order']:4.2f}  {c['verdict']}")
                    print(f"  {out['advice']}")
            return 0 if any_run else 5

        try:
            written = survey.run(cycles=args.cycles, duration_s=args.duration)
        except ValueError as e:
            print(f"refused to run: {e}", file=sys.stderr)
            return 4

    if not written:
        logger.warning("no interval files written -- the run was shorter "
                       "than one logging interval, or no band was observed")
        return 1
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
