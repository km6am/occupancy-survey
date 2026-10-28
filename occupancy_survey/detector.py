"""Turning a power spectrum into a per-channel carrier-present decision.

This file is the measurement. Everything else is scheduling and bookkeeping.

THE CHANNEL LEVEL STATISTIC is the maximum power bin inside the channel,
expressed in dB. Not the mean. A mean over the channel's bins dilutes a
narrow carrier by the ratio of channel width to carrier width -- about 18 dB
for a 3 kHz carrier sitting in a 180 kHz broadcast channel -- and that
dilution varies with the signal, not with anything we control.

Max-bin has a known bias: the maximum of N noise bins sits above their mean
by roughly 10*log10(ln N) dB, and it grows with N. That bias is *cancelled*
here rather than corrected, because the baseline is computed from the same
max-bin statistic over the same channel width. The bias appears in both the
level and the baseline and drops out of the difference. This is the reason
the design works, and it is also the reason `bins_per_channel` is recorded
in every dataset: a dataset whose channels were a different number of bins
wide has a different bias, and the two are only comparable after the
baseline subtraction, not before it.

THE BASELINE is a low percentile pooled over (the band's channels) x (recent
sweeps). Two alternatives were considered and both fail in a way that
matters here:

  - A purely *spectral* baseline -- median of neighbouring channels in the
    same sweep -- is pulled up by spectral leakage from a strong adjacent
    carrier, and a broadcast FM band has a strong adjacent carrier nearly
    everywhere.
  - A purely *temporal* baseline -- a low percentile of the channel's own
    history -- reads 0% occupancy on a channel that is occupied 100% of the
    time, because the carrier *becomes* the channel's own 25th percentile.
    That is a catastrophic failure on precisely the NOAA Weather Radio
    control band, whose whole job is to read ~100%.

Pooling over both axes avoids both. Its own load-bearing assumption, stated
so it can be pushed back on:

    AT ANY INSTANT, FEWER THAN `baseline_percentile` PERCENT OF A BAND'S
    CHANNELS ARE OCCUPIED.

For 400-odd VHF land-mobile grid channels with a handful of active carriers
this is comfortable. It is least comfortable on broadcast FM in a dense
market, which is one more reason the FM band is a control and not a target.
A band that violates it reports occupancy biased DOWN, because the baseline
rises into the signal. The control bands are how that shows up.

WHAT IS NOT DONE HERE: nothing in this file demodulates anything. The input
is a power spectrum and the output is a level and a boolean. No audio, no
IQ, no content. That is a structural property of the interface, not a
policy, and it is what makes this safe to ask a stranger to run.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np


# --------------------------------------------------------------------------- #
# Threshold policy
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ThresholdPolicy:
    """How "carrier present" is decided. Recorded in every dataset.

    A dataset whose threshold policy is not recorded cannot be compared with
    anything, including itself next month. Every field here goes into the
    acquisition metadata.
    """
    threshold_db: float = 10.0
    baseline_percentile: float = 25.0
    baseline_window_sweeps: int = 64
    min_baseline_samples: int = 64
    estimator: str = "band_pooled_percentile"

    def as_metadata(self) -> dict:
        return {
            "estimator": self.estimator,
            "threshold_db": self.threshold_db,
            "baseline_percentile": self.baseline_percentile,
            "baseline_window_sweeps": self.baseline_window_sweeps,
            "min_baseline_samples": self.min_baseline_samples,
            "level_statistic": "max_bin_in_channel_db",
            "assumption": (
                "fewer than baseline_percentile% of a band's channels are "
                "occupied at any instant"
            ),
        }


def max_bin_noise_bias_db(bins_per_channel: int) -> float:
    """Expected dB offset of max-of-N over mean-of-N for Rayleigh noise.

    Reported for the record only -- the detector cancels this bias rather
    than correcting it (see the module docstring). Useful when comparing a
    dataset's absolute baseline against another receiver's.

    Status: *inferred*, from the asymptotic expectation of the maximum of N
    exponential variates, E[max] ~ ln(N) + gamma. Accurate to better than
    0.5 dB for N >= 4; not to be relied on for N < 4.
    """
    n = max(1, int(bins_per_channel))
    if n == 1:
        return 0.0
    return 10.0 * math.log10(math.log(n) + 0.5772156649)


# --------------------------------------------------------------------------- #
# The threshold is not an SNR
# --------------------------------------------------------------------------- #
#
# This is the easiest thing to get wrong when reading a dataset from this
# tool, so it is computed rather than left as a remark.
#
# `threshold_db` is dB above the BASELINE, and the baseline is a low
# percentile of the max-bin statistic. The max of N noise bins is already
# above the mean noise power, so the baseline sits above the mean noise power
# too -- by a few dB -- and a channel's "excess_db" is therefore smaller than
# its SNR by that amount.
#
# Measured against the synthetic simulation in tests/test_detector.py, a
# 12 dB carrier in a 12-bin channel reads about 9.3 dB of excess, not 12.
# Setting threshold_db = 10 and expecting to catch 10 dB signals would
# silently miss them.

def baseline_elevation_db(bins_per_channel: int,
                          percentile: float = 25.0) -> float:
    """How far the baseline sits above the mean noise power, in dB.

    For N bins of independent exponential (unit-mean) noise power, the max
    has CDF (1 - exp(-x))^N, so its p-th percentile is

        x_p = -ln(1 - p^(1/N))

    in units of mean noise power. This returns 10*log10(x_p).

    Status: *inferred*, analytically, under two assumptions worth naming:
    that the channel's bins are independent (an FFT window with overlap or
    strong leakage correlates adjacent bins, which reduces the effective N
    and lowers this figure), and that the noise is circularly-symmetric
    Gaussian in the time domain. Verified against simulation to within
    0.3 dB in tests/test_detector.py; the independence assumption is the
    weak link and it fails worst for a heavily-windowed narrow channel.
    """
    n = max(1, int(bins_per_channel))
    p = min(max(float(percentile), 1e-6), 100.0 - 1e-6) / 100.0
    x_p = -math.log(1.0 - p ** (1.0 / n))
    return 10.0 * math.log10(x_p)


def required_snr_db(threshold_db: float, bins_per_channel: int,
                    percentile: float = 25.0) -> float:
    """SNR at which a continuous carrier sits on the decision boundary.

    SNR here is carrier power per bin over MEAN noise power per bin, which
    is the convention a link budget or a receiver spec uses -- so this is
    the number to compare against a predicted signal level.

    Derivation. The channel's level is the max over its N bins of
    (noise + carrier). With the carrier adding power s to every bin, that
    max is s + max(noise), whose typical value is s + (ln N + gamma) in
    units of mean noise power. The baseline is x_p = -ln(1 - p^(1/N)). The
    decision is level/baseline >= 10^(T/10), so the boundary is at

        s = x_p * 10^(T/10) - (ln N + gamma)

    The second term is what a naive "SNR = threshold + baseline elevation"
    gets wrong: it ignores the noise power sitting in the carrier's own
    bins, and so overstates the required SNR by around 0.6 dB for a 12-bin
    channel at a 10 dB threshold. Small, but it is the difference between a
    formula that locates the detection edge and one that merely orders it.

    Status: *inferred*, analytically. Assumes the carrier adds power
    incoherently and uniformly across the channel's bins, and that the bins
    are independent. Verified against simulation to within 1 dB of the
    measured 50% crossing in tests/test_detector.py.

    Returns -inf when the threshold is low enough that noise alone clears
    it, which means the configuration detects nothing but its own noise.
    """
    n = max(1, int(bins_per_channel))
    x_p = 10.0 ** (baseline_elevation_db(n, percentile) / 10.0)
    noise_max = math.log(n) + 0.5772156649 if n > 1 else 1.0
    s = x_p * 10.0 ** (float(threshold_db) / 10.0) - noise_max
    if s <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(s)


def false_alarm_rate(threshold_db: float, bins_per_channel: int,
                     percentile: float = 25.0) -> float:
    """Probability that NOISE ALONE clears the threshold, per sample.

    This is the occupancy floor of a configuration: a channel carrying
    nothing still reads this much occupancy, so any duty cycle near it is
    indistinguishable from an empty channel.

    For N independent exponential bins and a baseline at x_p times mean
    noise power, a sample is a false alarm when the max bin exceeds
    y = x_p * 10^(T/10):

        P = 1 - (1 - exp(-y))^N

    IT IS DOMINATED BY BINS PER CHANNEL, and catastrophically so at the
    bottom. A 12-bin channel at a 10 dB threshold gives about 3e-9 --
    nothing. A ONE-BIN channel at the same threshold gives 5.6%, because
    the max of one bin is just that bin and single-bin exponential noise
    has a heavy tail. A 5.6% floor would swamp the 10-25% conventional-LMR
    duty cycles this survey exists to measure.

    Found by ingesting an rtl_power CSV whose 12.5 kHz bins made every
    12.5 kHz channel exactly one bin wide: ninety noise-only channels
    reported 1.7-15% occupancy. The fix is a finer FFT bin, not a higher
    threshold -- raising the threshold to bury the false alarms also buries
    the weak real signals, while more bins per channel improves both at
    once.

    Status: *inferred*, analytically, under the same independence
    assumption as baseline_elevation_db(), and verified against simulation
    in tests/test_detector.py.
    """
    n = max(1, int(bins_per_channel))
    y = 10.0 ** (baseline_elevation_db(n, percentile) / 10.0) * \
        10.0 ** (float(threshold_db) / 10.0)
    if y > 700.0:          # exp(-y) underflows; the rate is unobservably small
        return 0.0
    p_one = math.exp(-y)
    return 1.0 - (1.0 - p_one) ** n


def occupancy_floor_warning(threshold_db: float, bins_per_channel: int,
                            percentile: float = 25.0,
                            concern_at: float = 0.01) -> str | None:
    """A sentence to carry in the dataset when the noise floor is high.

    Returned rather than logged so it travels with the data: a reader six
    months from now has to know that this file's 3% occupancies might be
    nothing at all.
    """
    rate = false_alarm_rate(threshold_db, bins_per_channel, percentile)
    if rate < concern_at:
        return None
    return (
        f"NOISE FLOOR {100 * rate:.2f}%: with {bins_per_channel} bin(s) per "
        f"channel at a {threshold_db:.0f} dB threshold, an EMPTY channel "
        f"reads about {100 * rate:.2f}% occupancy. Any duty cycle below "
        f"roughly {100 * rate * 3:.1f}% in this dataset is not "
        f"distinguishable from noise. Use a finer FFT bin so each channel "
        f"spans several bins; raising the threshold would bury weak real "
        f"signals along with the false alarms."
    )


# --------------------------------------------------------------------------- #
# Channel level extraction
# --------------------------------------------------------------------------- #

class ChannelLevelExtractor:
    """Maps a power spectrum onto per-channel levels.

    Channels that fall outside the spectrum's frequency coverage yield NaN,
    which propagates as "not observed" rather than as "observed quiet". The
    distinction matters: a band wider than the receiver's usable bandwidth
    is covered in several tune steps, and a channel absent from this step
    must not count as an idle sample.
    """

    def __init__(self, channels, freq_axis_hz: np.ndarray,
                 usable_fraction: float = 0.8):
        """
        Args:
            channels: iterable of Channel.
            freq_axis_hz: ascending frequency of each spectrum bin.
            usable_fraction: central fraction of the tuned span to trust.
                The edges of an SDR's passband roll off and carry the
                tuner's own artefacts (DC spur, images), so channels near
                the edge are treated as not observed rather than measured
                through an unknown filter slope.
        """
        self._channels = list(channels)
        self._freq_axis = np.asarray(freq_axis_hz, dtype=np.float64)
        if self._freq_axis.size < 2:
            raise ValueError("freq_axis_hz needs at least 2 bins")

        self._bin_hz = float(self._freq_axis[1] - self._freq_axis[0])
        lo, hi = float(self._freq_axis[0]), float(self._freq_axis[-1])
        center = 0.5 * (lo + hi)
        half_usable = 0.5 * usable_fraction * (hi - lo)
        self._usable_lo = center - half_usable
        self._usable_hi = center + half_usable

        self._slices: list[tuple[int, int] | None] = []
        for ch in self._channels:
            half_bw = 0.5 * ch.bandwidth_hz
            c_lo, c_hi = ch.freq_hz - half_bw, ch.freq_hz + half_bw
            if c_lo < self._usable_lo or c_hi > self._usable_hi:
                self._slices.append(None)
                continue
            i0 = int(np.searchsorted(self._freq_axis, c_lo, side="left"))
            i1 = int(np.searchsorted(self._freq_axis, c_hi, side="right"))
            if i1 - i0 < 1:
                # Channel narrower than one bin: take the single nearest bin
                # and say so via bins_per_channel, which will read 1.
                i0 = int(np.argmin(np.abs(self._freq_axis - ch.freq_hz)))
                i1 = i0 + 1
            self._slices.append((i0, i1))

        observed = [s for s in self._slices if s is not None]
        self._bins_per_channel = (
            int(round(float(np.mean([b - a for a, b in observed]))))
            if observed else 0
        )

    @property
    def bins_per_channel(self) -> int:
        """Mean bins per observed channel. Governs the max-bin noise bias."""
        return self._bins_per_channel

    @property
    def bin_hz(self) -> float:
        return self._bin_hz

    @property
    def n_observed(self) -> int:
        return sum(1 for s in self._slices if s is not None)

    def levels_db(self, spectrum_db: np.ndarray) -> np.ndarray:
        """Per-channel level in dB; NaN where the channel was not observed."""
        spec = np.asarray(spectrum_db, dtype=np.float64)
        if spec.size != self._freq_axis.size:
            raise ValueError(
                f"spectrum has {spec.size} bins, frequency axis has "
                f"{self._freq_axis.size}"
            )
        out = np.full(len(self._channels), np.nan, dtype=np.float64)
        for i, sl in enumerate(self._slices):
            if sl is None:
                continue
            a, b = sl
            out[i] = float(np.max(spec[a:b]))
        return out


def baseline_grid(center_hz: float, span_hz: float, bandwidth_hz: float,
                  n_cells: int = 160):
    """A uniform grid of pseudo-channels for estimating the noise floor.

    THE BASELINE MUST NOT BE COMPUTED FROM THE CHANNELS BEING RECORDED when
    those channels were chosen for being likely to carry traffic.

    The pooled baseline assumes fewer than `baseline_percentile`% of the
    channels it pools over are occupied at once. A uniform grid across a
    band satisfies that comfortably -- most grid cells have no licensee. A
    TARGETED list inverts it: every channel in it is there because an FCC
    record says a transmitter exists on it, so the occupied fraction can
    easily exceed the percentile, the baseline rides up into the signals,
    and occupancy is biased DOWN with nothing to show for it.

    So the noise floor is measured from this grid -- cells spread evenly
    across the whole usable passband, most of which is empty whatever the
    target list looks like -- while only the targeted channels are
    recorded. The cells use the same width as the real channels, because
    the max-bin bias the baseline has to cancel depends on bins per channel.

    Returns objects with the freq_hz/bandwidth_hz attributes
    ChannelLevelExtractor needs.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Cell:
        freq_hz: float
        bandwidth_hz: float
        service: str = ""
        label: str = ""

    n = max(8, int(n_cells))
    lo = center_hz - 0.5 * span_hz + 0.5 * bandwidth_hz
    hi = center_hz + 0.5 * span_hz - 0.5 * bandwidth_hz
    if hi <= lo:
        return [_Cell(center_hz, bandwidth_hz)]
    step = (hi - lo) / (n - 1)
    return [_Cell(lo + i * step, bandwidth_hz) for i in range(n)]


# --------------------------------------------------------------------------- #
# Rolling pooled baseline
# --------------------------------------------------------------------------- #

class BandBaseline:
    """Rolling low percentile of channel levels, pooled over channel x time.

    `update()` returns the baseline to use for the sweep just supplied. The
    sweep is included in its own baseline: a channel is judged against the
    band's quiet channels in the same sweep, which is what makes a
    permanently-keyed channel measurable.

    Before `min_baseline_samples` pooled samples have accumulated the
    baseline is None and no occupancy decision is made. Those early sweeps
    are counted as not-observed, not as idle -- otherwise every run would
    begin with a stretch of spuriously quiet data whose length depended on
    the window size.
    """

    def __init__(self, policy: ThresholdPolicy):
        self._policy = policy
        self._window: deque[np.ndarray] = deque(
            maxlen=max(1, policy.baseline_window_sweeps))
        self._pooled_count = 0

    def update(self, levels_db: np.ndarray) -> float | None:
        finite = np.asarray(levels_db, dtype=np.float64)
        finite = finite[np.isfinite(finite)]
        if finite.size:
            self._window.append(finite)
        self._pooled_count = sum(a.size for a in self._window)
        if self._pooled_count < self._policy.min_baseline_samples:
            return None
        pooled = np.concatenate(list(self._window))
        return float(np.percentile(pooled, self._policy.baseline_percentile))

    @property
    def pooled_count(self) -> int:
        return self._pooled_count

    def reset(self) -> None:
        """Forget the window.

        Called whenever the receiver's gain, tuned frequency or antenna
        changes, because the baseline is only meaningful for one fixed
        configuration. Carrying a baseline across a gain change would read
        the gain change as a band-wide change in activity.
        """
        self._window.clear()
        self._pooled_count = 0


@dataclass(frozen=True)
class SweepDecision:
    """One sweep's worth of carrier-present decisions for one band."""
    baseline_db: float | None
    levels_db: np.ndarray            # absolute, NaN where not observed
    excess_db: np.ndarray            # level - baseline, NaN where not decided
    present: np.ndarray              # bool; False where not decided
    decided: np.ndarray              # bool; True where a decision was made

    @property
    def n_active(self) -> int:
        """Channels judged carrier-present in this sweep."""
        return int(np.count_nonzero(self.present))

    @property
    def n_decided(self) -> int:
        return int(np.count_nonzero(self.decided))


def decide(levels_db: np.ndarray, baseline_db: float | None,
           policy: ThresholdPolicy) -> SweepDecision:
    """Apply the threshold to one sweep of channel levels."""
    levels = np.asarray(levels_db, dtype=np.float64)
    observed = np.isfinite(levels)

    if baseline_db is None:
        return SweepDecision(
            baseline_db=None,
            levels_db=levels,
            excess_db=np.full(levels.shape, np.nan),
            present=np.zeros(levels.shape, dtype=bool),
            decided=np.zeros(levels.shape, dtype=bool),
        )

    excess = np.full(levels.shape, np.nan)
    excess[observed] = levels[observed] - baseline_db
    present = np.zeros(levels.shape, dtype=bool)
    present[observed] = excess[observed] >= policy.threshold_db
    return SweepDecision(baseline_db=float(baseline_db), levels_db=levels,
                         excess_db=excess, present=present, decided=observed)
