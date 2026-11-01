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

    def set_filters(self, front_end) -> dict:
        """Apply a band's front-end filter plan; return what was applied.

        The default records the request and actuates nothing, which is the
        honest answer for a receiver with no switchable filters. The
        returned dict goes into the dataset either way, because a reader
        needs to know whether the FM notch was ON, OFF, or simply absent.
        """
        return {**front_end.as_metadata(), "actuated": False,
                "actuated_note": "this receiver has no switchable filters; "
                                 "the plan is recorded, not applied"}

    def set_band_gain(self, band_name: str) -> dict:
        """Apply a band's RF gain after tuning into it; return what applied.

        The default actuates nothing and says so. A backend whose gain is
        one fixed setting for the whole run has nothing per band to apply.
        """
        return {"actuated": False,
                "note": "this backend has one gain for the whole run"}

    def overload_reports(self) -> dict:
        """Front-end overload the device itself reported during this run.

        Overload is the error that FLATTERS this measurement -- intermod
        products read as carriers and raise occupancy -- so a backend that
        can observe it should, and one that cannot must say so rather than
        report zero.
        """
        return {"supported": False,
                "verdict": "not_observed",
                "note": "this backend cannot observe front-end overload"}

    def gain_in_effect(self) -> dict:
        """Gain the device reports now, against what was configured.

        Fixed gain is load-bearing here, so a backend that can check it
        should. The default says it cannot rather than claiming the gain
        held, because "not checked" and "verified stable" must not read
        the same way in a dataset.
        """
        return {"queryable": False,
                "note": "this backend cannot read gain back"}

    def antenna_ports(self) -> list[str]:
        """Selectable antenna ports, or empty for a receiver with one input.

        Empty means "nothing to choose", not "unknown": a caller may treat
        an empty list as a settled question.
        """
        return []

    def set_antenna_port(self, name: str) -> str:
        """Switch port; returns the port in effect afterwards.

        The default refuses rather than pretending, so a port sweep over a
        single-input receiver fails loudly instead of reporting the same
        port three times under three names.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no selectable antenna ports")

    def set_gain_offset_db(self, offset_db: float) -> bool:
        """Shift gain by `offset_db` for the intermod check; False if fixed.

        Used only by the paired-gain diagnostic, which needs to change gain
        deliberately and put it back. A backend that cannot do this returns
        False and the diagnostic is skipped rather than faked.
        """
        return False

    def achieved_gain_offset_db(self) -> float | None:
        """The gain step the last set_gain_offset_db() actually produced.

        None when the backend cannot measure it. The paired-gain diagnostic
        divides by this step, so a backend that can verify it should: a
        request the attenuator rounds off otherwise propagates straight into
        every implied order.
        """
        return None

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
