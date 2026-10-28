"""Synthetic power spectra with known channel occupancy.

Everything here is deterministic given a seed, so a test that fails fails
reproducibly. The generator produces what the detector consumes -- a power
spectrum in dB and its frequency axis -- and nothing upstream of that, so
these tests exercise the measurement without needing an SDR.

The noise model is exponential power per bin (Rayleigh magnitude), which is
what an FFT of complex Gaussian noise gives. That matters for the max-bin
statistic: the detector's noise bias comes from the tail of this
distribution, so a test on Gaussian-in-dB noise would understate it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SyntheticEmitter:
    """A carrier keyed on a fixed periodic pattern.

    duty is the fraction of sweeps the carrier is on. period_sweeps is the
    on/off cycle length. phase_sweeps offsets the cycle so that several
    emitters are not synchronised with each other or with the sweep index,
    which is the aliasing failure the real scheduler jitters against.
    """
    freq_hz: float
    snr_db: float
    duty: float = 1.0
    period_sweeps: int = 10
    phase_sweeps: int = 0
    bandwidth_hz: float = 8_000.0

    def is_on(self, sweep: int) -> bool:
        if self.duty >= 1.0:
            return True
        if self.duty <= 0.0:
            return False
        pos = (sweep + self.phase_sweeps) % self.period_sweeps
        return pos < self.duty * self.period_sweeps


class SyntheticBand:
    """Generates spectra for a span with a fixed set of emitters."""

    def __init__(self, freq_min_hz: float, freq_max_hz: float,
                 bin_hz: float, emitters=(), noise_floor_db: float = -100.0,
                 seed: int = 1):
        self.freq_axis = np.arange(freq_min_hz, freq_max_hz, bin_hz,
                                   dtype=np.float64)
        self.bin_hz = bin_hz
        self.emitters = list(emitters)
        self.noise_floor_db = noise_floor_db
        self._rng = np.random.default_rng(seed)

    def sweep(self, sweep_index: int) -> np.ndarray:
        """One power spectrum in dB."""
        n = self.freq_axis.size
        noise_lin = self._rng.exponential(1.0, size=n)
        power_lin = noise_lin.copy()

        for em in self.emitters:
            if not em.is_on(sweep_index):
                continue
            half = 0.5 * em.bandwidth_hz
            mask = (self.freq_axis >= em.freq_hz - half) & \
                   (self.freq_axis <= em.freq_hz + half)
            if not mask.any():
                continue
            # SNR is defined against the mean noise power per bin (1.0 in
            # linear units here), so snr_db maps straight onto added power.
            power_lin[mask] += 10.0 ** (em.snr_db / 10.0)

        return self.noise_floor_db + 10.0 * np.log10(
            np.maximum(power_lin, 1e-12))
