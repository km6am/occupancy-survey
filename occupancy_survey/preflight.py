"""Is this host ready to run a survey?

The framework is developed on one machine and run on another, so the
question "will this work here" cannot be answered by the machine that wrote
it. This answers it on the host that matters, before a long unattended run
rather than after it.

WHAT IT DELIBERATELY DOES NOT DO: open the SDR. Device access is the thing
most likely to be wrong on a new host, but opening a radio is the
operator's call, so the device check is a separate opt-in step
(`--check --device`) and the summary says plainly that it was not tested.
Everything else -- imports, binaries, config, disk, clock, permissions --
is free to check and is checked.

Each result carries a REMEDY, not just a verdict. "numpy missing" on an
unknown host is not actionable; "pip install numpy, or conda install -c
conda-forge numpy if this is a conda environment" is.
"""

from __future__ import annotations

import importlib
import os
import shutil
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Measured: one hourly interval of the default five-band schedule is about
# 57 kB gzipped. Histograms compress ~66x because most channels are empty.
BYTES_PER_INTERVAL = 57_000


@dataclass
class Check:
    name: str
    ok: bool | None          # None == not tested
    detail: str = ""
    remedy: str = ""

    @property
    def symbol(self) -> str:
        return {True: "ok  ", False: "FAIL", None: "--  "}[self.ok]


@dataclass
class Preflight:
    checks: list = field(default_factory=list)

    def add(self, name, ok, detail="", remedy=""):
        self.checks.append(Check(name, ok, detail, remedy))

    @property
    def failures(self):
        return [c for c in self.checks if c.ok is False]

    @property
    def untested(self):
        return [c for c in self.checks if c.ok is None]

    def report(self) -> str:
        width = max(len(c.name) for c in self.checks)
        lines = []
        for c in self.checks:
            lines.append(f"  [{c.symbol}] {c.name:<{width}}  {c.detail}")
            if c.ok is False and c.remedy:
                for rl in c.remedy.splitlines():
                    lines.append(f"         {rl}")
        return "\n".join(lines)


def _check_python(pf: Preflight) -> None:
    v = sys.version_info
    ok = v >= (3, 10)
    pf.add("python", ok, f"{platform.python_version()} on {platform.system()}",
           "this package needs Python 3.10 or newer")


def _check_imports(pf: Preflight) -> None:
    for mod, why, remedy in (
        ("numpy", "required", "pip install numpy"),
        ("yaml", "required for config", "pip install PyYAML"),
    ):
        try:
            m = importlib.import_module(mod)
            pf.add(mod, True, getattr(m, "__version__", "present"))
        except ImportError as e:
            pf.add(mod, False, f"{why}: {e}", remedy)


def _check_backend(pf: Preflight, backend: str) -> None:
    """Can this backend be reached at all? Does not open the device."""
    backend = (backend or "").lower().strip()

    if backend == "synthetic":
        pf.add("backend", True, "synthetic (no hardware; produces no "
                                "measurements)")
        return

    if backend in ("rtl_sdr", "rtlsdr", "rtl_sdr_pipe"):
        path = shutil.which("rtl_sdr")
        pf.add("rtl_sdr binary", path is not None, path or "not on PATH",
               "macOS:  brew install librtlsdr\n"
               "Debian: sudo apt install rtl-sdr\n"
               "then check the dongle with: rtl_test -t")
        return

    if backend in ("gnuradio", "gr", "rspdx"):
        try:
            importlib.import_module("gnuradio")
            pf.add("gnuradio", True, "importable")
        except ImportError as e:
            pf.add("gnuradio", False, str(e)[:90],
                   "GNU Radio is not pip-installable. Install radioconda, or "
                   "your distribution's gnuradio package, and run this from "
                   "that environment.\n"
                   "Verify with: python -c 'from gnuradio import gr'")
            return
        try:
            importlib.import_module("gnuradio.sdrplay3")
            pf.add("gr-sdrplay3", True, "importable")
        except ImportError as e:
            msg = str(e)
            remedy = ("gr-sdrplay3 is present but cannot load. If the error "
                      "mentions libsdrplay_api, the SDRplay API library is "
                      "not on the loader path for this environment.\n"
                      "Install the SDRplay API v3, then make it visible to "
                      "the GNU Radio environment (on some installs this "
                      "means a symlink into <env>/lib).")
            if "libsdrplay_api" not in msg:
                remedy = ("install gr-sdrplay3 in this environment "
                          "(radioconda ships it)")
            pf.add("gr-sdrplay3", False, msg[:90], remedy)
        return

    pf.add("backend", False, f"unknown backend '{backend}'",
           "one of: gnuradio, rtl_sdr, synthetic")


def _check_output(pf: Preflight, out_dir, interval_s: float) -> None:
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".preflight"
        probe.write_text("x")
        probe.unlink()
        writable = True
        detail = str(out.resolve())
    except OSError as e:
        writable = False
        detail = f"{out}: {e}"
    pf.add("output dir", writable, detail,
           f"create {out} and make it writable by the survey user")

    if not writable:
        return
    try:
        free = shutil.disk_usage(out).free
    except OSError:
        return
    per_day = BYTES_PER_INTERVAL * (86400.0 / max(interval_s, 1.0))
    days = free / per_day if per_day else float("inf")
    # 513 MB/year measured, so this is almost never the binding constraint;
    # it is checked because an unattended run that fills a disk fails
    # silently and loses the tail of the campaign.
    pf.add("disk space", days > 30,
           f"{free/1e9:.1f} GB free, ~{per_day/1e6:.1f} MB/day "
           f"-> {days:.0f} days of headroom",
           "free space, or shorten the campaign")


def _check_clock(pf: Preflight) -> None:
    """Datasets merge across sites on timestamps, so the clock matters.

    This cannot verify NTP portably. It checks the cheap things -- that the
    clock is not obviously unset and that UTC is derivable -- and says
    plainly that it did not verify synchronisation, rather than implying it
    did.
    """
    now = datetime.now(timezone.utc)
    plausible = datetime(2025, 1, 1, tzinfo=timezone.utc) < now < \
        datetime(2100, 1, 1, tzinfo=timezone.utc)
    pf.add("clock", plausible,
           f"{now.isoformat(timespec='seconds')} (local tz "
           f"{time.tzname[0]}); NTP sync NOT verified",
           "set the system clock; cross-site merging joins on timestamps")


def run_preflight(cfg, probe_device: bool = False) -> Preflight:
    pf = Preflight()
    _check_python(pf)
    _check_imports(pf)

    try:
        bands = cfg.band_plans()
        pf.add("config", True,
               f"{len(bands)} band(s), site '{cfg.site.site_id}'")
    except Exception as e:                                # pragma: no cover
        pf.add("config", False, str(e)[:90], "fix config.yaml")
        bands = []

    _check_backend(pf, cfg.sdr.backend)
    _check_output(pf, cfg.out_dir, cfg.sampling.interval_s)
    _check_clock(pf)

    if probe_device:
        _probe_device(pf, cfg)
    else:
        pf.add("device access", None, "not tested",
               "re-run with --device to open the SDR and take one sweep")
    return pf



def _check_gain_lock(pf: Preflight, backend) -> None:
    """Confirm the device has not moved the gain out from under us."""
    try:
        g = backend.gain_in_effect()
    except Exception as e:                                # pragma: no cover
        pf.add("gain lock", None, f"could not query: {str(e)[:60]}", "")
        return

    if not g.get("queryable"):
        pf.add("gain lock", None, "not checkable on this backend",
               "fixed gain cannot be verified here; it is assumed, and the "
               "dataset records that it was not checked")
        return

    if g.get("agc_mode"):
        pf.add("gain lock", False, "the device reports AGC ENABLED",
               "every threshold here is noise-floor-relative and an AGC "
               "moves the floor. Do not collect until this reads off.")
        return

    def _stage(name, v) -> str:
        s = f"{name} {v['actual_db']:+.0f} dB"
        if abs(v.get("drift_db", 0.0)) > g["tolerance_db"]:
            s += f" (was {v.get('accepted_db', v['configured_db']):+.0f})"
        # A request the attenuator could not land on exactly is the hardware
        # working, not a fault -- shown, not warned about.
        q = v.get("quantisation_db")
        if q is not None and abs(q) > g["tolerance_db"]:
            s += f" (asked {v['configured_db']:+.0f}, step to "
            s += f"{v.get('accepted_db', v['actual_db']):+.0f})"
        # The settable range is what makes moving attenuation between
        # stages a measured choice instead of a guess, so print it rather
        # than only filing it in the dataset.
        rng = v.get("settable_db")
        if rng:
            s += f" [{rng[0]:+.0f}..{rng[1]:+.0f}]"
        return s

    detail = ", ".join(_stage(k, v)
                       for k, v in sorted(g.get("stages", {}).items())
                       if "actual_db" in v)

    if g.get("locked"):
        pf.add("gain lock", True, f"{detail}; matches config")
    else:
        pf.add("gain lock", False,
               f"gain moved {g['worst_drift_db']:.1f} dB since setup "
               f"({detail})",
               "the device changed gain by itself -- most likely its own "
               "overload correction. A moving gain moves the noise floor "
               "and every threshold with it. Reduce gain until the device "
               "stops correcting, then re-check.")


def _check_overload_reports(pf: Preflight, backend) -> None:
    """Report what the device said about its own front end."""
    try:
        o = backend.overload_reports()
    except Exception as e:                                # pragma: no cover
        pf.add("front-end overload", None, f"not observed: {str(e)[:60]}", "")
        return

    if not o.get("supported"):
        pf.add("front-end overload", None, "not observable on this backend",
               "the zero count means 'not observed', not 'none occurred'")
        return

    # Episodes, not `detected` lines: the API reports an onset from inside
    # its own startup, where no watcher can be listening, so a bare
    # `corrected` still means an overload happened.
    n = o.get("overload_events", 0)
    if not n:
        # NOT a clean bill of health. The fd 2 capture has been observed
        # missing RSPdx overload messages that reached the console in the
        # same run -- see overload_watch.py. The gain-lock check above is
        # the instrument that actually sees the API intervening.
        pf.add("front-end overload", None, "none captured (see gain lock)",
               "fd 2 capture is known to miss SDRplay's own reports; a zero "
               "here is not evidence the front end is healthy")
        return

    early = o.get("onset_before_watching", 0)
    detail = f"the API reported overload {n} time(s)"
    if early:
        detail += f" ({early} beginning before the device was opened)"
    pf.add("front-end overload", False, detail,
           "intermod from a compressed front end looks like carriers and "
           "RAISES occupancy. Back off RF gain, not IF: the IF attenuator "
           "is downstream of the stage that compresses. See docs/front-end.md")

def _probe_device(pf: Preflight, cfg) -> None:
    """Open the SDR and take a single sweep. Opt-in only."""
    from occupancy_survey.__main__ import _build_backend
    try:
        backend = _build_backend(cfg)
    except Exception as e:
        pf.add("device access", False, str(e)[:90],
               "check the device is connected and not claimed by other "
               "software")
        return
    try:
        with backend:
            band = cfg.band_plans()[0]
            # Apply the band's filter plan before sweeping. Without this the
            # probe measured a configuration the survey never uses -- and
            # since the notches are the FIRST remedy the overload advice
            # below recommends, probing with them off overstates the
            # problem it is reporting.
            fe = backend.set_filters(band.front_end)
            if fe.get("not_actuated"):
                pf.add("front-end filters", None,
                       f"not actuated: {', '.join(fe['not_actuated'])}",
                       "this receiver cannot switch them; fit an external "
                       "filter or accept the overload risk")
            backend.tune(band.center_hz, cfg.sampling.usable_bandwidth_hz)
            axis = backend.freq_axis()
            got = next(iter(backend.sweeps(2.0)), None)
            if got is None:
                pf.add("device access", False, "no sweep returned in 2 s",
                       "the device opened but produced no samples")
                return
            span = (float(axis[-1] - axis[0]) if len(axis) > 1 else 0.0)
            clip = got.clip_fraction
            pf.add("device access", True,
                   f"one sweep of {band.name}, {len(got.spectrum_db)} bins "
                   f"over {span/1e6:.3f} MHz"
                   + (f", clip {clip:.1e}" if clip is not None else ""))
            if clip is not None and clip > 1e-4:
                pf.add("front end", False,
                       f"clip fraction {clip:.1e} at the configured gain",
                       "the front end is being overloaded: fit the band's "
                       "bandpass, enable the notches, or reduce gain -- in "
                       "that order. See docs/front-end.md")

            # Fixed gain is the premise every threshold here rests on, and
            # the SDRplay API corrects overloads on its own. Ask the device
            # whether the gain we set is still the gain in effect.
            _check_gain_lock(pf, backend)
            _check_overload_reports(pf, backend)
    except Exception as e:                                # pragma: no cover
        pf.add("device access", False, str(e)[:90],
               "see docs/front-end.md and your backend's install notes")
