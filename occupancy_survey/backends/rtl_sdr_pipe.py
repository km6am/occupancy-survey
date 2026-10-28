"""RTL-SDR via the `rtl_sdr` binary, piped as raw IQ.

Why this and not `rtl_power`: rtl_power integrates over its whole interval
(10 s by default) and reports the AVERAGE level per bin. Averaging and then
thresholding is the §1 mistake of duty_cycles.md moved from the model into
the measurement -- a channel keyed 20% of a 10 s window reads as one
continuous carrier 7 dB weaker, and a 2 s burst may not clear the threshold
at all. The integration time is the measurement, so it has to belong to this
tool rather than to the acquisition binary.

`rtl_power` is still supported, in rtl_power_csv.py, as a zero-install
fallback with that bias documented in the dataset.

Dependencies: the `rtl_sdr` binary and numpy. No SoapySDR, no GNU Radio.
That is what makes this the volunteer path -- an RTL-SDR dongle plus one
binary that is already packaged nearly everywhere.

AGC: `rtl_sdr -g <n>` sets manual gain; gain 0 means automatic, so a gain of
0 is REFUSED here rather than passed through. The tuner AGC would move the
noise floor and take the occupancy number with it.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

import numpy as np

from occupancy_survey.backends.base import Backend, Sweep, spectrum_db_from_iq

logger = logging.getLogger(__name__)

DEFAULT_BINARY = "rtl_sdr"


class RtlSdrPipeBackend(Backend):
    """Spawns `rtl_sdr` per tuning and reads uint8 IQ from its stdout."""

    def __init__(self, sample_rate_hz: float = 2_048_000.0,
                 gain_db: float = 40.0, fft_size: int = 4096,
                 integration_s: float = 0.1, device_index: int = 0,
                 ppm: int = 0, binary: str | None = None,
                 settle_s: float = 0.25):
        if gain_db <= 0:
            raise ValueError(
                "gain_db must be positive and explicit. `rtl_sdr -g 0` means "
                "automatic gain, and an AGC moves the noise floor that every "
                "threshold in this tool is relative to. Pick a fixed gain "
                "(try 40) and keep it for the whole campaign."
            )
        self._binary = binary or shutil.which(DEFAULT_BINARY) or DEFAULT_BINARY
        self._sample_rate = float(sample_rate_hz)
        self._gain = float(gain_db)
        self._fft_size = int(fft_size)
        self._integration_s = float(integration_s)
        self._device_index = int(device_index)
        self._ppm = int(ppm)
        self._settle_s = float(settle_s)
        self._center = 0.0
        self._proc: subprocess.Popen | None = None
        self._window = np.blackman(self._fft_size).astype(np.float32)

    # ----------------------------------------------------------------- #

    def receiver_metadata(self) -> dict:
        return {
            "backend": "rtl_sdr_pipe",
            "binary": self._binary,
            "gain": self._gain,
            "gain_units": "rtl_sdr tuner gain, dB (manual)",
            "agc_enabled": False,
            "sample_rate_hz": self._sample_rate,
            "fft_size": self._fft_size,
            "integration_s": self._integration_s,
            "ppm": self._ppm,
            "sample_format": "uint8 IQ, 8-bit",
            "caveat": "8-bit samples give roughly 50 dB of usable dynamic "
                      "range, so a strong local carrier can push weaker "
                      "channels in the same tuning toward the noise floor. "
                      "Reach matters more than sensitivity for occupancy, "
                      "but a channel reading 0% next to a broadcast "
                      "transmitter deserves suspicion.",
        }

    def freq_axis(self) -> np.ndarray:
        return (self._center - 0.5 * self._sample_rate
                + self._sample_rate * np.arange(self._fft_size)
                / self._fft_size)

    def tune(self, center_hz: float, span_hz: float) -> None:
        if abs(center_hz - self._center) < 1.0 and self._proc is not None:
            return
        self._stop()
        self._center = float(center_hz)
        cmd = [self._binary, "-f", str(int(self._center)),
               "-s", str(int(self._sample_rate)), "-g", f"{self._gain:.1f}",
               "-d", str(self._device_index)]
        if self._ppm:
            cmd += ["-p", str(self._ppm)]
        cmd += ["-"]
        logger.info("spawning: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,
                                      bufsize=0)
        # Discard the first samples: the tuner PLL and the AGC-off gain
        # setting take a moment, and samples taken during the settle would
        # land in the baseline as a spurious level step.
        self._read_exact(int(self._sample_rate * self._settle_s) * 2)

    def _read_exact(self, n_bytes: int) -> bytes | None:
        if self._proc is None or self._proc.stdout is None:
            return None
        buf = bytearray()
        while len(buf) < n_bytes:
            chunk = self._proc.stdout.read(n_bytes - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def sweeps(self, dwell_s: float):
        if self._proc is None:
            raise RuntimeError("tune() before sweeps()")
        n_samples = max(self._fft_size,
                        int(self._sample_rate * self._integration_s))
        n_bytes = n_samples * 2
        deadline = time.monotonic() + float(dwell_s)

        while time.monotonic() < deadline:
            raw = self._read_exact(n_bytes)
            if raw is None:
                logger.error("rtl_sdr stopped producing samples; is the "
                             "dongle still attached?")
                return
            u8 = np.frombuffer(raw, dtype=np.uint8)
            # Clip fraction before conversion: 0 and 255 are the rails.
            clipped = int(np.count_nonzero((u8 == 0) | (u8 == 255)))
            iq = (u8.astype(np.float32) - 127.5) / 127.5
            iq = iq[0::2] + 1j * iq[1::2]
            yield Sweep(
                t_unix=time.time(),
                spectrum_db=spectrum_db_from_iq(iq.astype(np.complex64),
                                                self._fft_size, self._window),
                clip_fraction=clipped / max(1, u8.size),
            )

    def _stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2.0)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        finally:
            self._proc = None

    def close(self) -> None:
        self._stop()
