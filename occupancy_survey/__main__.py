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

    from occupancy_survey.detector import required_snr_db
    print()
    print("detection edge (SNR a continuous carrier needs):")
    for b in bands:
        from occupancy_survey.schedule import tune_steps
        bin_hz = cfg.sdr.sample_rate_hz / cfg.sdr.fft_size
        n_bins = max(1, int(b.channel_bandwidth_hz / bin_hz))
        print(f"  {b.name:<14} {n_bins:4d} bins/channel -> "
              f"{required_snr_db(cfg.detection.threshold_db, n_bins, cfg.detection.baseline_percentile):5.1f} dB")

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
    ap.add_argument("--out-dir", help="override output.dir")
    ap.add_argument("--cycles", type=int, help="stop after N full passes")
    ap.add_argument("--duration", type=float, metavar="S",
                    help="stop after S seconds")
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

    with backend:
        survey = Survey(cfg.band_plans(), backend, cfg.site, cfg.sampling,
                        cfg.detection, cfg.histogram, out_dir=cfg.out_dir)
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
