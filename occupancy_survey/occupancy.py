"""Accumulating sweep decisions into occupancy statistics.

THE POINT OF THIS FILE is that the dataset stays re-thresholdable. The task
that motivates this survey warns that "the detection threshold IS the
result", and the obvious reading of that is to document the threshold
carefully. The stronger move is to store enough that the threshold can be
*changed* afterwards without re-running the survey.

So each channel accumulates, per interval, a histogram of
(level - baseline) in 1 dB bins -- not a boolean. Occupancy at any threshold
is then the histogram mass at or above that threshold, recoverable offline
and comparable between sites that chose different thresholds. The dataset
still carries a default threshold and the occupancy at it, because a file
nobody can read at a glance is not much use, but the default is a label on
the data rather than a property of it.

Cost of this choice: about 70 numbers per channel per interval instead of
two. For the default schedule (roughly 2500 grid channels) that is a few
hundred kB per interval before compression. Accepted -- it is the difference
between a dataset that answers one question and one that answers the
question somebody asks in six months.

WHAT THE HISTOGRAM CANNOT RECOVER is time structure. Two channels with
identical histograms can be one long transmission and many short ones, and
the duty-cycle distinction in duty_cycles.md section 1 -- depth versus
episode length -- needs the second. So a burst-length summary is accumulated
alongside: run lengths of consecutive present-sweeps at the default
threshold. Those ARE tied to the default threshold and cannot be
re-derived at another one, which is stated in the data format document.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from occupancy_survey.detector import SweepDecision, ThresholdPolicy


# --------------------------------------------------------------------------- #
# Histogram specification
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HistogramSpec:
    """Fixed dB bins for (level - baseline).

    The range has to cover the whole usable dynamic range of the receiver
    relative to its noise floor, because anything that lands in the overflow
    bin loses its level. 70 dB spans an RSPdx's practical range on a fixed
    gain setting with room to spare; broadcast FM at a close-in site is the
    case that will test the top end.
    """
    lo_db: float = -10.0
    hi_db: float = 60.0
    bin_db: float = 1.0

    @property
    def n_bins(self) -> int:
        return int(round((self.hi_db - self.lo_db) / self.bin_db))

    @property
    def edges_db(self) -> np.ndarray:
        return self.lo_db + self.bin_db * np.arange(self.n_bins + 1)

    def as_metadata(self) -> dict:
        return {"lo_db": self.lo_db, "hi_db": self.hi_db,
                "bin_db": self.bin_db, "n_bins": self.n_bins,
                "units": "dB above the sweep's band baseline",
                "underflow_bin": "counts below lo_db; never carrier-present",
                "overflow_bin": "counts at or above hi_db; carrier-present "
                                "at any threshold <= hi_db"}


# --------------------------------------------------------------------------- #
# Per-channel accumulation
# --------------------------------------------------------------------------- #

class ChannelAccumulator:
    """Excess-dB histogram and run lengths for one channel, one interval."""

    __slots__ = ("hist", "underflow", "overflow", "n_decided", "n_present",
                 "_run", "runs", "_spec", "_threshold_db")

    def __init__(self, spec: HistogramSpec, threshold_db: float):
        self._spec = spec
        self._threshold_db = threshold_db
        self.hist = np.zeros(spec.n_bins, dtype=np.int64)
        self.underflow = 0
        self.overflow = 0
        self.n_decided = 0
        self.n_present = 0
        self._run = 0
        self.runs: list[int] = []

    def add(self, excess_db: float, present: bool, decided: bool) -> None:
        if not decided:
            # A gap in observation ends any run in progress, but the run is
            # recorded as a *lower bound* only in the sense that it may have
            # continued unseen. That ambiguity is why run lengths are
            # reported with the dwell length beside them.
            self._close_run()
            return

        self.n_decided += 1
        idx = int(np.floor((excess_db - self._spec.lo_db) / self._spec.bin_db))
        if idx < 0:
            self.underflow += 1
        elif idx >= self._spec.n_bins:
            self.overflow += 1
        else:
            self.hist[idx] += 1

        if present:
            self.n_present += 1
            self._run += 1
        else:
            self._close_run()

    def _close_run(self) -> None:
        if self._run:
            self.runs.append(self._run)
            self._run = 0

    def finalise(self) -> None:
        self._close_run()

    def occupancy_at(self, threshold_db: float) -> float | None:
        """Fraction of decided samples at or above `threshold_db`.

        None when nothing was decided -- distinct from 0.0, which means
        observed and quiet.
        """
        if self.n_decided == 0:
            return None
        spec = self._spec
        if threshold_db >= spec.hi_db:
            above = self.overflow
        else:
            first = int(np.ceil((threshold_db - spec.lo_db) / spec.bin_db))
            first = max(0, first)
            above = int(self.hist[first:].sum()) + self.overflow
        return above / self.n_decided

    def to_record(self) -> dict:
        self.finalise()
        runs = np.asarray(self.runs, dtype=np.int64)
        return {
            "n_decided": int(self.n_decided),
            "n_present": int(self.n_present),
            "occupancy": self.occupancy_at(self._threshold_db),
            "hist": self.hist.tolist(),
            "underflow": int(self.underflow),
            "overflow": int(self.overflow),
            "runs": {
                "n": int(runs.size),
                "max_sweeps": int(runs.max()) if runs.size else 0,
                "mean_sweeps": float(runs.mean()) if runs.size else 0.0,
                "at_threshold_db": self._threshold_db,
            },
        }


# --------------------------------------------------------------------------- #
# Per-band accumulation over one logging interval
# --------------------------------------------------------------------------- #

@dataclass
class BandIntervalStats:
    """Band-level summary for one interval.

    `active_per_sweep` is the "count of active channels per band per
    interval" the survey is asked for. It is kept as a distribution rather
    than a mean because the mean of a count that is usually 2 and
    occasionally 40 describes neither state.
    """
    sweeps: int = 0
    sweeps_decided: int = 0
    active_per_sweep: list[int] = field(default_factory=list)
    baselines_db: list[float] = field(default_factory=list)
    clip_fractions: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        act = np.asarray(self.active_per_sweep, dtype=np.float64)
        base = np.asarray(self.baselines_db, dtype=np.float64)
        clip = np.asarray(self.clip_fractions, dtype=np.float64)
        out = {
            "sweeps": self.sweeps,
            "sweeps_decided": self.sweeps_decided,
            "active_channels": {
                "mean": float(act.mean()) if act.size else None,
                "median": float(np.median(act)) if act.size else None,
                "p95": float(np.percentile(act, 95)) if act.size else None,
                "max": int(act.max()) if act.size else None,
            },
            "baseline_dbfs": {
                "mean": float(base.mean()) if base.size else None,
                "min": float(base.min()) if base.size else None,
                "max": float(base.max()) if base.size else None,
                "note": "uncalibrated dBFS, not dBm; see data-format.md",
            },
        }
        if clip.size:
            out["clip_fraction"] = {
                "mean": float(clip.mean()), "max": float(clip.max()),
                "note": "fraction of IQ samples at or near full scale. "
                        "Anything above ~1e-4 means the front end is being "
                        "overloaded and the noise floor -- hence every "
                        "threshold -- is suspect.",
            }
        return out


class BandAccumulator:
    """Accumulates sweeps for one band over one logging interval."""

    def __init__(self, band, channels, spec: HistogramSpec,
                 policy: ThresholdPolicy):
        self.band = band
        self.channels = list(channels)
        self.spec = spec
        self.policy = policy
        self._chan = [ChannelAccumulator(spec, policy.threshold_db)
                      for _ in self.channels]
        self.stats = BandIntervalStats()

    def add_sweep(self, decision: SweepDecision,
                  clip_fraction: float | None = None) -> None:
        if len(decision.levels_db) != len(self.channels):
            raise ValueError("sweep width does not match the channel list")

        self.stats.sweeps += 1
        if decision.baseline_db is not None:
            self.stats.sweeps_decided += 1
            self.stats.baselines_db.append(decision.baseline_db)
            self.stats.active_per_sweep.append(decision.n_active)
        if clip_fraction is not None:
            self.stats.clip_fractions.append(float(clip_fraction))

        for acc, exc, pres, dec in zip(self._chan, decision.excess_db,
                                       decision.present, decision.decided,
                                       strict=True):
            acc.add(float(exc) if np.isfinite(exc) else 0.0,
                    bool(pres), bool(dec) and decision.baseline_db is not None)

    def channel_records(self, min_decided: int = 1) -> list[dict]:
        """Per-channel records, skipping channels never observed.

        A grid channel that was never inside a usable passband produces no
        record at all rather than a record full of zeros, which keeps the
        file size proportional to what was actually measured.
        """
        out = []
        for ch, acc in zip(self.channels, self._chan, strict=True):
            if acc.n_decided < min_decided:
                continue
            rec = acc.to_record()
            rec["freq_hz"] = ch.freq_hz
            rec["bandwidth_hz"] = ch.bandwidth_hz
            rec["service"] = ch.service
            out.append(rec)
        out.sort(key=lambda r: r["freq_hz"])
        return out

    def control_check(self) -> dict | None:
        """For a control band, compare measured occupancy to the expectation.

        Returns None for a target band. For a control the answer is the
        maximum occupancy over the band's channels, because the expectation
        is that *at least one* receivable continuous carrier exists -- not
        that every channel in the band has one.
        """
        if self.band.expected_occupancy is None:
            return None
        occs = [a.occupancy_at(self.policy.threshold_db) for a in self._chan]
        occs = [o for o in occs if o is not None]
        if not occs:
            return {"expected_occupancy": self.band.expected_occupancy,
                    "max_measured_occupancy": None,
                    "verdict": "no_channels_observed"}
        best = max(occs)
        # 0.9 rather than 0.99: a continuous carrier can still drop below
        # threshold on fading, and the FFT sweep can land during a brief
        # transmitter fault. A control reading below 0.9 is a red flag for
        # the threshold or the receive path, not proof of one.
        verdict = "pass" if best >= 0.9 else "fail"
        return {"expected_occupancy": self.band.expected_occupancy,
                "max_measured_occupancy": best,
                "pass_floor": 0.9,
                "verdict": verdict,
                "meaning": "a control band below the floor means the "
                           "threshold or the receive path is wrong, and the "
                           "target bands from the same run inherit the fault"}
