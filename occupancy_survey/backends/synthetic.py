"""A backend with no hardware, for exercising the whole pipeline.

Two real uses, neither of them a toy:

  1. A volunteer can verify their install end to end, and see what a
     dataset looks like, before plugging anything in. "Run this and check
     you get a file" is a much better first instruction than "attach an
     SDR".
  2. The orchestration, the file format and the plots can be developed and
     regression-tested without opening a device -- which this project does
     not do unprompted.

Datasets it produces carry `synthetic: true` in their receiver metadata,
alongside the nominal gain the writer requires. Nothing from this backend
should ever reach a result that claims to be measured, and that flag is how
it gets caught -- tools/validate_occupancy.py refuses a synthetic dataset
outright.
"""

from __future__ import annotations

import time

import numpy as np

from occupancy_survey.backends.base import Backend, Sweep


class SyntheticBackend(Backend):
    """Noise plus a fixed set of keyed carriers.

    The emitter list is given in absolute frequency, so the same emitters
    appear and disappear as the schedule retunes across them -- which makes
    this a real test of the tune-step decomposition, not just of the
    detector.
    """

    def __init__(self, emitters=(), sample_rate_hz: float = 2_000_000.0,
                 fft_size: int = 4096, noise_floor_db: float = -100.0,
                 seed: int = 0, realtime: bool = False,
                 integration_s: float = 0.1):
        self._emitters = list(emitters)
        self._sample_rate = float(sample_rate_hz)
        self._fft_size = int(fft_size)
        self._noise_floor_db = float(noise_floor_db)
        self._rng = np.random.default_rng(seed)
        self._realtime = realtime
        self._integration_s = float(integration_s)
        self._center = 0.0
        self._span = self._sample_rate
        self._sweep_index = 0

    def receiver_metadata(self) -> dict:
        return {
            "backend": "synthetic",
            "synthetic": True,
            "gain": 0.0,
            "agc_enabled": False,
            "sample_rate_hz": self._sample_rate,
            "fft_size": self._fft_size,
            "note": "NO HARDWARE. Numbers from this backend are not "
                    "measurements of anything and must not be reported as "
                    "occupancy for a real site.",
        }

    def tune(self, center_hz: float, span_hz: float) -> None:
        self._center = float(center_hz)
        self._span = float(span_hz)

    def freq_axis(self) -> np.ndarray:
        return (self._center - 0.5 * self._sample_rate
                + self._sample_rate * np.arange(self._fft_size)
                / self._fft_size)

    def sweeps(self, dwell_s: float):
        n = max(1, int(dwell_s / self._integration_s))
        axis = self.freq_axis()
        for _ in range(n):
            yield Sweep(t_unix=time.time(),
                        spectrum_db=self._make_spectrum(axis),
                        clip_fraction=0.0)
            self._sweep_index += 1
            if self._realtime:
                time.sleep(self._integration_s)

    def _make_spectrum(self, axis: np.ndarray) -> np.ndarray:
        power = self._rng.exponential(1.0, size=axis.size)
        for em in self._emitters:
            if not em.is_on(self._sweep_index):
                continue
            half = 0.5 * em.bandwidth_hz
            mask = (axis >= em.freq_hz - half) & (axis <= em.freq_hz + half)
            if mask.any():
                power[mask] += 10.0 ** (em.snr_db / 10.0)
        return self._noise_floor_db + 10.0 * np.log10(
            np.maximum(power, 1e-12))
