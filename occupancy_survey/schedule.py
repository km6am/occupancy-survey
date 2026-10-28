"""The sampling design: dwell, revisit, and what it cannot see.

A channel keyed for 3 s every 5 minutes is invisible to a 1 s dwell every
10 minutes. That bound belongs on every result, so it is computed here
rather than described, and the computed values go into each dataset's
metadata beside the gain and the threshold.

Four parameters, and what each one trades:

  integration_s     how long one FFT sweep integrates. Sets the SHORTEST
                    DETECTABLE BURST: a burst shorter than this is averaged
                    against noise and loses 10*log10(burst/integration) dB.
  dwell_s           how long the receiver stays on one tune step. Sets how
                    many samples one visit contributes, and with the burst
                    length it sets how many of them are independent.
  revisit_target_s  how often the whole schedule comes round. Sets the
                    CATCH PROBABILITY for a periodic transmission.
  jitter_fraction   how much the visit timing is randomised.

THE JITTER IS NOT A REFINEMENT. It is the design's defence against its
worst failure, and that failure is quiet. A fixed revisit period can alias
against real periodicity in the traffic -- shift changes, hourly nets, the
top-of-hour weather cycle, a telemetry site that reports every 15 minutes --
and an aliased schedule does not return a noisy answer, it returns a
confident wrong one: always sampling during the transmission, or never. The
bounds below all assume visits are uniformly distributed in phase relative
to whatever the transmitter is doing, and jitter is what buys that
assumption. If one number from this survey turns out to be wrong, this is
where I would look first.

Status of everything in this file: *inferred*, analytically, from the
stated sampling model. None of it is measured. The catch-probability model
in particular assumes a burst of fixed length recurring on a fixed period,
which real dispatch traffic is not.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class SamplingDesign:
    """The sampling parameters, and the bounds they imply.

    Defaults are chosen for the stated purpose -- measuring conventional
    land-mobile duty cycles in the 10-25% range, where transmissions are
    dispatch-length, a few seconds to a minute. They are deliberately
    dwell-heavy rather than revisit-heavy: for a duty-cycle statistic, time
    on channel is worth more than frequency of return.
    """
    integration_s: float = 0.1
    dwell_s: float = 20.0
    revisit_target_s: float = 600.0
    jitter_fraction: float = 0.25
    usable_bandwidth_hz: float = 6_000_000.0
    usable_fraction: float = 0.8
    interval_s: float = 3600.0

    def __post_init__(self):
        if self.integration_s <= 0 or self.dwell_s <= 0:
            raise ValueError("integration_s and dwell_s must be positive")
        if self.dwell_s < self.integration_s:
            raise ValueError("dwell_s must be at least one integration period")
        if not 0.0 <= self.jitter_fraction < 1.0:
            raise ValueError("jitter_fraction must be in [0, 1)")

    @property
    def sweeps_per_visit(self) -> int:
        return max(1, int(self.dwell_s / self.integration_s))

    def as_metadata(self) -> dict:
        return {
            "integration_s": self.integration_s,
            "dwell_s": self.dwell_s,
            "revisit_target_s": self.revisit_target_s,
            "jitter_fraction": self.jitter_fraction,
            "usable_bandwidth_hz": self.usable_bandwidth_hz,
            "usable_fraction": self.usable_fraction,
            "interval_s": self.interval_s,
            "sweeps_per_visit": self.sweeps_per_visit,
        }


# --------------------------------------------------------------------------- #
# Tune-step decomposition
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TuneStep:
    """One receiver tuning within a band.

    A 20 MHz band cannot be observed in one tuning by a receiver with 6 MHz
    of usable bandwidth, so bands are decomposed. Every extra tune step
    multiplies the cycle time, which is the real cost of a wide band: the
    UHF land-mobile band at 20 MHz costs four times the airtime of the
    5.2 MHz VHF band for the same dwell.
    """
    band_name: str
    center_hz: float
    span_hz: float
    index: int
    of: int

    @property
    def freq_min_hz(self) -> float:
        return self.center_hz - 0.5 * self.span_hz

    @property
    def freq_max_hz(self) -> float:
        return self.center_hz + 0.5 * self.span_hz


def tune_steps(band, design: SamplingDesign) -> list[TuneStep]:
    """Decompose a band into receiver tunings.

    Steps overlap by the unusable edge fraction, so that a channel falling
    in one step's rolled-off edge lands in the middle of its neighbour
    rather than being measured through an unknown filter slope -- or missed.
    """
    usable = design.usable_bandwidth_hz * design.usable_fraction
    if usable <= 0:
        raise ValueError("usable bandwidth is zero")

    n = max(1, math.ceil(band.span_hz / usable))
    step_usable = band.span_hz / n
    steps = []
    for i in range(n):
        lo = band.freq_min_hz + i * step_usable
        center = lo + 0.5 * step_usable
        steps.append(TuneStep(band_name=band.name, center_hz=center,
                              span_hz=design.usable_bandwidth_hz,
                              index=i, of=n))
    return steps


def cycle_time_s(bands, design: SamplingDesign,
                 retune_overhead_s: float = 0.5) -> float:
    """Wall-clock seconds for one pass over every tune step of every band.

    This, not `revisit_target_s`, is the real revisit interval when the
    schedule cannot keep up. A design whose cycle time exceeds its revisit
    target is not wrong, but it is not doing what its configuration says,
    and `Schedule.warnings()` says so rather than letting the metadata
    claim a revisit the run never achieved.
    """
    total = 0
    for b in bands:
        total += len(tune_steps(b, design))
    return total * (design.dwell_s + retune_overhead_s)


# --------------------------------------------------------------------------- #
# The blind spot, as numbers
# --------------------------------------------------------------------------- #

def min_detectable_burst_s(integration_s: float,
                           snr_excess_db: float) -> float:
    """Shortest burst a sweep can still see, given dB of SNR to spare.

    A burst shorter than the integration period is averaged over the whole
    period, so its contribution is attenuated by 10*log10(burst/integration)
    dB. It is still detected while that loss is smaller than the margin the
    signal has above threshold. So

        burst_min = integration_s * 10^(-snr_excess_db/10)

    `snr_excess_db` is dB ABOVE the detection threshold, not SNR -- a signal
    exactly at threshold has zero margin and cannot survive any averaging
    loss, which this correctly reports as the full integration period.

    Assumes the burst falls entirely inside one integration period. A burst
    straddling two periods splits its energy and is harder to see than this
    says, by up to 3 dB.
    """
    if snr_excess_db <= 0:
        return float(integration_s)
    return float(integration_s) * 10.0 ** (-float(snr_excess_db) / 10.0)


def catch_probability(burst_s: float, period_s: float, dwell_s: float,
                      n_visits: int) -> float:
    """Probability that at least one visit overlaps a periodic burst.

    A single visit of length D overlaps a burst of length L somewhere in a
    cycle of period P with probability min(1, (L + D) / P) -- the burst is
    caught if the visit starts any time in a window of width L + D. Over n
    visits whose phases are independent,

        P(at least one catch) = 1 - (1 - min(1, (L+D)/P))^n

    THE INDEPENDENCE IS THE ASSUMPTION, and it is bought by jitter. With a
    fixed revisit period commensurate with the burst period the phases are
    identical every time and this formula does not apply: the real answer is
    then 0 or 1, and which one is luck.

    Note this is the probability of seeing the channel AT ALL, which is a
    much weaker thing than estimating its duty cycle. A channel caught once
    in an hour is known to be active and its occupancy is not yet measured.
    """
    if period_s <= 0 or n_visits <= 0:
        return 0.0
    q = min(1.0, (max(0.0, burst_s) + max(0.0, dwell_s)) / period_s)
    return 1.0 - (1.0 - q) ** int(n_visits)


def effective_independent_samples(dwell_s: float, integration_s: float,
                                  burst_s: float, n_visits: int) -> float:
    """Independent samples behind an occupancy estimate.

    Sweeps within a dwell are NOT independent: a transmission lasting longer
    than the integration period produces a run of correlated present-sweeps.
    Treating each sweep as independent overstates the precision by roughly
    sqrt(burst/integration) -- a factor of 7 for a 5 s transmission sampled
    every 100 ms -- so a naive binomial error bar on occupancy from this
    survey would be about seven times too small.

    The effective count per visit is the number of distinct transmission
    opportunities the dwell spans, floored at one: a dwell shorter than one
    transmission contributes one sample, not many.
    """
    if integration_s <= 0:
        return 0.0
    chunk = max(float(integration_s), float(burst_s))
    per_visit = max(1.0, float(dwell_s) / chunk)
    return per_visit * max(0, int(n_visits))


def occupancy_standard_error(p: float, n_eff: float) -> float:
    """Binomial standard error on an occupancy estimate.

    Fed with `effective_independent_samples`, not with the raw sweep count.
    Returns NaN for a nonpositive effective count rather than zero, because
    "no uncertainty" is the one answer that is certainly wrong.
    """
    if n_eff <= 0:
        return float("nan")
    p = min(max(float(p), 0.0), 1.0)
    return math.sqrt(p * (1.0 - p) / float(n_eff))


def blind_spot_table(design: SamplingDesign, n_visits: int,
                     burst_lengths_s=(0.05, 0.5, 3.0, 10.0, 30.0),
                     periods_s=(60.0, 300.0, 900.0, 3600.0)) -> list[dict]:
    """Tabulate catch probability over burst length and recurrence period.

    This is the table that belongs beside every result from a given
    configuration. Rows where `detectable` is False are the blind spot
    proper: the burst is too short for the integration period to register it
    at a typical 6 dB margin, so the catch probability is moot.
    """
    rows = []
    floor = min_detectable_burst_s(design.integration_s, snr_excess_db=6.0)
    for burst in burst_lengths_s:
        for period in periods_s:
            if burst >= period:
                continue
            rows.append({
                "burst_s": burst,
                "period_s": period,
                "implied_duty": burst / period,
                "detectable": burst >= floor,
                "catch_probability": catch_probability(
                    burst, period, design.dwell_s, n_visits),
                "n_eff": effective_independent_samples(
                    design.dwell_s, design.integration_s, burst, n_visits),
            })
    return rows


# --------------------------------------------------------------------------- #
# The schedule itself
# --------------------------------------------------------------------------- #

class Schedule:
    """Produces the visit order, with jitter.

    Visits are emitted band by band in the configured order. The jitter is
    applied to the DWELL of each visit rather than to a sleep between
    visits, because idle time on a receiver that could be listening is
    wasted airtime; varying the dwell decorrelates the visit phases just as
    well and keeps the duty of observation up.
    """

    def __init__(self, bands, design: SamplingDesign, seed: int | None = None):
        self._bands = list(bands)
        self._design = design
        self._rng = random.Random(seed)
        self._steps: list[tuple[object, TuneStep]] = []
        for b in self._bands:
            for st in tune_steps(b, design):
                self._steps.append((b, st))
        if not self._steps:
            raise ValueError("schedule has no tune steps")

    @property
    def steps(self):
        return list(self._steps)

    def jittered_dwell_s(self) -> float:
        f = self._design.jitter_fraction
        if f <= 0:
            return self._design.dwell_s
        return self._design.dwell_s * (1.0 + self._rng.uniform(-f, f))

    def visits(self, cycles: int):
        """Yield (band, tune_step, dwell_s) for `cycles` full passes."""
        for _ in range(int(cycles)):
            for band, step in self._steps:
                yield band, step, self.jittered_dwell_s()

    def warnings(self, retune_overhead_s: float = 0.5) -> list[str]:
        """Honest complaints about the configuration.

        Returned rather than logged so they can be written into the dataset:
        a reader six months from now needs to know the run could not meet
        its own revisit target, and a log file will not be there.
        """
        out = []
        cycle = cycle_time_s(self._bands, self._design, retune_overhead_s)
        if cycle > self._design.revisit_target_s:
            out.append(
                f"cycle time {cycle:.0f} s exceeds revisit_target_s "
                f"{self._design.revisit_target_s:.0f} s: the real revisit "
                f"interval is {cycle:.0f} s. Reduce dwell, drop a band, or "
                f"raise the target so the metadata states what happened."
            )
        if cycle > self._design.interval_s:
            out.append(
                f"cycle time {cycle:.0f} s exceeds interval_s "
                f"{self._design.interval_s:.0f} s: some bands will have no "
                f"samples at all in some intervals, and their occupancy will "
                f"be null rather than zero."
            )
        if self._design.jitter_fraction == 0.0:
            out.append(
                "jitter_fraction is 0: a fixed revisit period can alias "
                "against periodic traffic and return a confidently wrong "
                "occupancy rather than a noisy one. See schedule.py."
            )
        if self._design.sweeps_per_visit < 20:
            out.append(
                f"only {self._design.sweeps_per_visit} sweeps per visit: "
                f"occupancy from a visit this short is dominated by whether "
                f"a single transmission happened to overlap it."
            )
        return out

    def design_summary(self, visits_per_interval: int | None = None) -> dict:
        """The sampling design and its bounds, for the dataset metadata."""
        cycle = cycle_time_s(self._bands, self._design)
        if visits_per_interval is None:
            visits_per_interval = max(
                1, int(self._design.interval_s / cycle)) if cycle > 0 else 1
        floor_6db = min_detectable_burst_s(self._design.integration_s, 6.0)
        return {
            **self._design.as_metadata(),
            "tune_steps": len(self._steps),
            "cycle_time_s": cycle,
            "visits_per_band_per_interval": visits_per_interval,
            "observed_time_fraction": min(
                1.0, (len(self._steps) * self._design.dwell_s) / max(
                    cycle, 1e-9)),
            "min_detectable_burst_s_at_6db_margin": floor_6db,
            "blind_spot": {
                "shorter_than_s": floor_6db,
                "meaning": "a burst shorter than this loses more than its "
                           "6 dB margin to integration averaging and is not "
                           "reliably registered, whatever its duty cycle",
            },
            "warnings": self.warnings(),
        }
