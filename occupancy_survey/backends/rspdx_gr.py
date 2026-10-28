"""SDRplay RSPdx (and other GNU Radio devices) with a true integrating FFT.

WHY NOT REUSE cw_recorder's FLOWGRAPH SHAPE. That one averages the power
spectrum with `single_pole_iir_filter_ff` and polls a `probe_signal_vf`.
An IIR average is a perfectly good display smoother and the wrong thing
here: it has no defined integration window, its effective averaging time
depends on alpha and on how often you happen to poll, and two polls in quick
succession return correlated values that this tool would count as
independent samples. Since the integration time sets the shortest detectable
burst -- see docs/sampling-design.md -- it has to be exact and it has to be
known.

So this flowgraph accumulates a counted number of FFT frames and publishes
the mean, which is the primitive the measurement needs. `IntegratingFFTSink`
is the only custom block.

The IQ ring buffer, the narrowband extractor and the recorder from
cw_recorder are all absent, deliberately: no path here can produce audio or
IQ. See backends/base.py.

ON THIS MAC, as of 2026-10-28, this backend does not import. Two independent
breakages, both user-fixable without sudo, with the exact commands in
docs/rspdx-on-macos.md:
  1. radioconda's numpy is broken, so scipy and matplotlib go with it.
  2. gnuradio.sdrplay3 looks for @rpath/libsdrplay_api.so.3 with rpath
     ~/radioconda/lib, while the API lives in /usr/local/lib.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from occupancy_survey.backends.base import Backend, Sweep

logger = logging.getLogger(__name__)


def _gr():
    """Import GNU Radio, with an actionable message when it is broken."""
    try:
        from gnuradio import blocks, fft, gr
        return gr, blocks, fft
    except ImportError as e:
        raise ImportError(
            f"GNU Radio is not importable: {e}. On this Mac, see "
            f"docs/rspdx-on-macos.md -- radioconda's numpy and the "
            f"gr-sdrplay3 library path both need repairing, and neither "
            f"needs sudo."
        ) from e


def _make_integrating_sink(gr):
    """Build the integrating sink class against the imported gr module."""

    class IntegratingFFTSink(gr.sync_block):
        """Accumulates a counted number of power-spectrum frames.

        Publishes the MEAN of exactly `frames_per_integration` frames onto a
        queue, so each published sweep is an independent integration over a
        known time -- unlike an IIR average, whose window is implicit.

        The queue is bounded and drops the OLDEST sweep when full. Dropping
        is correct here: a consumer that has fallen behind should be given
        current data, and the drop count is reported so a run that dropped
        heavily is visible in the metadata rather than silently thinned.
        """

        def __init__(self, fft_size: int, frames_per_integration: int,
                     maxsize: int = 64):
            gr.sync_block.__init__(self, name="integrating_fft_sink",
                                   in_sig=[(np.float32, fft_size)],
                                   out_sig=None)
            self._fft_size = int(fft_size)
            self._n = max(1, int(frames_per_integration))
            self._acc = np.zeros(self._fft_size, dtype=np.float64)
            self._count = 0
            self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
            self.dropped = 0
            self._lock = threading.Lock()

        def work(self, input_items, output_items):
            frames = input_items[0]
            with self._lock:
                for frame in frames:
                    self._acc += frame
                    self._count += 1
                    if self._count >= self._n:
                        mean = self._acc / self._count
                        self._acc[:] = 0.0
                        self._count = 0
                        self._publish(mean)
            return len(frames)

        def _publish(self, mean: np.ndarray) -> None:
            item = (time.time(), mean)
            try:
                self.queue.put_nowait(item)
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass
                try:
                    self.queue.put_nowait(item)
                except queue.Full:
                    self.dropped += 1

        def reset(self) -> None:
            """Discard partial accumulation and anything queued.

            Called on retune: a sweep straddling a frequency change is half
            one band and half another, and a queued pre-retune sweep would
            be attributed to the new tuning.
            """
            with self._lock:
                self._acc[:] = 0.0
                self._count = 0
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break

    return IntegratingFFTSink


class GnuRadioBackend(Backend):
    """FFT-only flowgraph over any device create_source() supports."""

    def __init__(self, sdr_config, fft_size: int = 8192,
                 integration_s: float = 0.1, settle_s: float = 0.3):
        self._gr, self._blocks, self._fft = _gr()
        from occupancy_survey.backends.gr_sdr_sources import create_source

        self._config = sdr_config
        self._fft_size = int(fft_size)
        self._integration_s = float(integration_s)
        self._settle_s = float(settle_s)
        self._sample_rate = float(sdr_config.sample_rate_hz)
        self._center = float(sdr_config.center_freq_hz)

        frames = max(1, int(self._integration_s * self._sample_rate
                            / self._fft_size))
        self._frames_per_integration = frames
        self._actual_integration_s = frames * self._fft_size / self._sample_rate
        if abs(self._actual_integration_s - self._integration_s) > \
                0.2 * self._integration_s:
            logger.warning(
                "integration_s %.4f s is not a whole number of %d-point "
                "FFTs at %.0f Hz; using %.4f s. The dataset records the "
                "actual value, since it sets the shortest detectable burst.",
                self._integration_s, self._fft_size, self._sample_rate,
                self._actual_integration_s)

        sink_cls = _make_integrating_sink(self._gr)
        gr, blocks, fftmod = self._gr, self._blocks, self._fft

        self._tb = gr.top_block("occupancy_survey")
        self._source = create_source(sdr_config)
        s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, self._fft_size)
        window = fftmod.window.blackmanharris(self._fft_size)
        fft_blk = fftmod.fft_vcc(self._fft_size, True, window, True)
        mag2 = blocks.complex_to_mag_squared(self._fft_size)
        self._sink = sink_cls(self._fft_size, frames)
        self._tb.connect(self._source, s2v, fft_blk, mag2, self._sink)

        self._running = False

    # ----------------------------------------------------------------- #

    def receiver_metadata(self) -> dict:
        cfg = self._config
        meta = {
            "backend": "gnuradio",
            "device_type": getattr(cfg, "device_type", "unknown"),
            "agc_enabled": False,
            "sample_rate_hz": self._sample_rate,
            "bandwidth_hz": getattr(cfg, "bw_hz", None),
            "fft_size": self._fft_size,
            "integration_s": self._actual_integration_s,
            "integration_s_requested": self._integration_s,
            "frames_per_integration": self._frames_per_integration,
            "fft_window": "blackman-harris",
            "antenna_port": getattr(cfg, "antenna_port", None),
            "dropped_sweeps": self._sink.dropped,
        }
        if getattr(cfg, "device_type", "").lower() == "rspdx":
            meta["gain"] = {
                "if_reduction_db": cfg.if_gain_reduction_db,
                "rf_reduction_db": cfg.rf_gain_reduction_db,
            }
            meta["gain_units"] = ("SDRplay gain REDUCTION in dB; larger means "
                                 "less gain")
            meta["hdr_mode"] = getattr(cfg, "hdr_mode", False)
        else:
            meta["gain"] = getattr(cfg, "gain", None)
            meta["gain_units"] = "device gain, dB"
        return meta

    def freq_axis(self) -> np.ndarray:
        return (self._center - 0.5 * self._sample_rate
                + self._sample_rate * np.arange(self._fft_size)
                / self._fft_size)

    def _start(self) -> None:
        if not self._running:
            self._tb.start()
            self._running = True
            time.sleep(self._settle_s)

    def tune(self, center_hz: float, span_hz: float) -> None:
        self._start()
        if abs(center_hz - self._center) < 1.0:
            return
        self._center = float(center_hz)
        self._set_center(self._center)
        self._sink.reset()
        # The retune settle is discarded rather than integrated: a PLL that
        # has not locked produces a spectrum that would enter the baseline
        # as a level step across the whole span.
        time.sleep(self._settle_s)
        self._sink.reset()

    def _set_center(self, hz: float) -> None:
        for setter in ("set_center_freq", "set_frequency"):
            fn = getattr(self._source, setter, None)
            if fn is None:
                continue
            try:
                fn(hz)
                return
            except TypeError:
                fn(0, hz)
                return
        raise RuntimeError(
            f"{type(self._source).__name__} exposes no retune method; a "
            f"multi-step band cannot be surveyed with this device."
        )

    def sweeps(self, dwell_s: float):
        self._start()
        deadline = time.monotonic() + float(dwell_s)
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                t, power = self._sink.queue.get(
                    timeout=max(0.05, min(remaining, 5.0)))
            except queue.Empty:
                continue
            with np.errstate(divide="ignore"):
                spectrum_db = 10.0 * np.log10(np.maximum(power, 1e-20))
            # fft_vcc was built with shift=True, so the output is already in
            # ascending frequency order. Do not fftshift again.
            yield Sweep(t_unix=t, spectrum_db=spectrum_db,
                        clip_fraction=None)

    def close(self) -> None:
        if self._running:
            self._tb.stop()
            self._tb.wait()
            self._running = False
        if self._sink.dropped:
            logger.warning("dropped %d sweeps: the consumer could not keep "
                           "up with the integration rate", self._sink.dropped)
