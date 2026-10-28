"""The acquisition interface: power spectra in, nothing else out.

THIS INTERFACE IS THE "LOG ENERGY, NOT CONTENT" GUARANTEE, and it is
structural rather than a policy that could be forgotten. A backend hands up
`Sweep` objects carrying a power spectrum in dB and a clip fraction. There
is no method that returns IQ, no method that returns audio, and nothing
above this layer could demodulate anything if it wanted to.

That matters most for the volunteer build. "This program records channel
statistics, not transmissions" is a claim somebody should be able to check,
and the shortest honest check is that the only thing crossing this boundary
is a power spectrum.

A backend is also responsible for reporting its own configuration through
`receiver_metadata()`, including the gain it actually set and that AGC is
off. That dictionary goes straight into the dataset, where the writer
refuses it if AGC is on.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Sweep:
    """One integrated power spectrum.

    `clip_fraction` is the fraction of raw samples at or near full scale, or
    None if the backend cannot tell. It is not decoration: with a fixed gain
    the front end can be overloaded, and an overloaded front end raises the
    noise floor across the whole span, which moves every
    noise-floor-relative threshold and quietly lowers every occupancy
    figure. A backend that can measure it should.
    """
    t_unix: float
    spectrum_db: np.ndarray
    clip_fraction: float | None = None


class Backend(abc.ABC):
    """Tune, then yield integrated power spectra."""

    @abc.abstractmethod
    def receiver_metadata(self) -> dict:
        """Backend name, gain, sample rate, FFT size, and agc_enabled.

        Must include `gain` and `agc_enabled`; the dataset writer refuses a
        file without them, and refuses one where AGC is on.
        """

    @abc.abstractmethod
    def tune(self, center_hz: float, span_hz: float) -> None:
        """Retune. The caller resets its baseline after this."""

    @abc.abstractmethod
    def freq_axis(self) -> np.ndarray:
        """Ascending bin frequencies for the current tuning."""

    @abc.abstractmethod
    def sweeps(self, dwell_s: float):
        """Yield Sweep objects for about `dwell_s` seconds."""

    def close(self) -> None:
        """Release the device. Safe to call twice."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def spectrum_db_from_iq(iq: np.ndarray, fft_size: int,
                        window: np.ndarray | None = None) -> np.ndarray:
    """Integrate |FFT|^2 over as many whole FFTs as `iq` contains.

    Integration is an average of power across the whole dwell segment, which
    is the primitive the measurement needs: a mean over a SHORT window that
    is then thresholded per sample. The mistake to avoid -- and the reason
    this tool does not simply read rtl_power's output -- is averaging over a
    long window and thresholding the average, which turns a 20% duty cycle
    into a continuous carrier 7 dB weaker and loses the occupancy entirely.

    Returns bins in ascending frequency order (fftshifted).
    """
    n = (len(iq) // fft_size) * fft_size
    if n == 0:
        raise ValueError(
            f"{len(iq)} samples is less than one {fft_size}-point FFT"
        )
    frames = np.asarray(iq[:n], dtype=np.complex64).reshape(-1, fft_size)
    if window is None:
        window = np.blackman(fft_size).astype(np.float32)
    spec = np.fft.fft(frames * window, axis=1)
    power = np.mean(np.abs(spec) ** 2, axis=0)
    power = np.fft.fftshift(power)
    # Normalise by the window's power gain so the noise floor does not move
    # when the window changes. Occupancy is threshold-relative so this does
    # not change any result, but it keeps baseline_dbfs comparable between
    # runs of this tool that used different windows.
    norm = float(np.sum(window ** 2)) or 1.0
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(np.maximum(power / norm, 1e-20))
