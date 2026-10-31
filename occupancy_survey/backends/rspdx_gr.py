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

GNU Radio is not pip-installable, so this backend's availability is a
property of the host rather than of this package. Run

    python -m occupancy_survey --check

on the machine that will do the surveying; it reports what is missing and
what to do about it, without opening the radio. Add --device to also take
one sweep.

The development Mac this package was written on has two unrelated breakages
of its own, written up in docs/rspdx-on-macos.md. That document is a
troubleshooting note for ONE host, not a prerequisite for the project.
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
            f"GNU Radio is not importable: {e}. GNU Radio is not "
            f"pip-installable -- install radioconda or your distribution's "
            f"gnuradio package and run from that environment. "
            f"`python -m occupancy_survey --check` reports what is missing "
            f"on this host."
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


def _make_clip_sink(gr):
    """Build the clip-counting sink against the imported gr module."""

    class ClipSink(gr.sync_block):
        """Counts samples at or near full scale on the raw IQ stream.

        WHY THIS EXISTS. Until this block, the GNU Radio backend yielded
        `clip_fraction=None` on every sweep, so `Sweep`'s docstring promise
        -- "a backend that can measure it should" -- was unmet, and the
        preflight front-end check could never fire. The SDRplay API was
        printing `overload corrected` to stderr while the tool reported a
        clean bill of health.

        WHAT IT MEASURES AND WHAT IT DOES NOT. The ADC clips inside the
        RSPdx, before gr-sdrplay3 scales its samples into fc32, so this
        counts samples arriving at the scaled full-scale value: a proxy for
        clipping, not a count of it. Worse, the API's automatic gain
        correction can relieve an overload before the samples reach us, so a
        corrected overload may leave no trace here at all.

        The evidence is therefore ASYMMETRIC: a non-zero fraction is strong
        evidence the front end is sick; a zero is weak evidence it is
        healthy. The authoritative signal is the API's own overload message,
        which this tool cannot yet capture -- it is written to stderr by the
        C++ library, not exposed through gr-sdrplay3. Watch the console.
        """

        def __init__(self, threshold: float = 0.98):
            gr.sync_block.__init__(self, name="clip_sink",
                                   in_sig=[np.complex64], out_sig=None)
            self._threshold = float(threshold)
            self._clipped = 0
            self._total = 0
            self._lock = threading.Lock()

        def work(self, input_items, output_items):
            x = input_items[0]
            n = len(x)
            hits = int(np.count_nonzero(
                (np.abs(x.real) >= self._threshold)
                | (np.abs(x.imag) >= self._threshold)))
            with self._lock:
                self._clipped += hits
                self._total += n
            return n

        def take(self):
            """Fraction since the last call; None if no samples arrived.

            The window this covers is only approximately the integration
            window of the sweep it is attached to -- the two blocks are
            scheduled independently. Close enough to diagnose a front end,
            not close enough to attribute clipping to one FFT frame.
            """
            with self._lock:
                total, clipped = self._total, self._clipped
                self._total = self._clipped = 0
            return None if total == 0 else clipped / total

    return ClipSink


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

        # Second branch off the SAME source: the FFT path cannot see
        # clipping, because clipping is a property of the raw samples.
        self._clip = _make_clip_sink(self._gr)(
            threshold=float(getattr(sdr_config, "clip_threshold", 0.98)))
        self._tb.connect(self._source, self._clip)

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
        self._clip.take()
        # The retune settle is discarded rather than integrated: a PLL that
        # has not locked produces a spectrum that would enter the baseline
        # as a level step across the whole span.
        time.sleep(self._settle_s)
        self._sink.reset()
        self._clip.take()

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

    def antenna_ports(self) -> list[str]:
        """Antenna ports this device reports, straight from the hardware.

        The authoritative list. The GRC block definition shipped with
        gr-sdrplay3 declares three for the RSPdx, but only the device knows
        what this build actually accepts.
        """
        fn = getattr(self._source, "get_antennas", None)
        if fn is None:
            return []
        try:
            return [str(p) for p in fn()]
        except Exception as e:                           # pragma: no cover
            logger.warning("could not query antenna ports: %s", e)
            return []

    def set_antenna_port(self, name: str) -> str:
        """Switch port and return the port the device reports afterwards.

        Returns the device's answer rather than the requested name, because
        a silently ignored switch would otherwise look like a successful
        one -- and a port comparison built on that would be meaningless.
        """
        self._source.set_antenna(str(name))
        self._sink.reset()
        self._clip.take()
        time.sleep(self._settle_s)
        self._sink.reset()
        self._clip.take()
        getter = getattr(self._source, "get_antenna", None)
        if getter is None:
            return str(name)
        try:
            return str(getter())
        except Exception:                                # pragma: no cover
            return str(name)

    def set_filters(self, front_end) -> dict:
        """Actuate the RSPdx's notch filters for this band."""
        applied, missing = {}, []
        for attr, setter in (("rf_notch", "set_rf_notch_filter"),
                             ("dab_notch", "set_dab_notch_filter")):
            want = bool(getattr(front_end, attr))
            fn = getattr(self._source, setter, None)
            if fn is None:
                missing.append(attr)
                continue
            try:
                fn(want)
                applied[attr] = want
            except Exception as e:                       # pragma: no cover
                logger.warning("could not set %s: %s", attr, e)
                missing.append(attr)
        # A notch change alters the passband shape, so the noise floor
        # moves; the caller resets the baseline after this for the same
        # reason it does after a retune.
        meta = {**front_end.as_metadata(), "actuated": not missing,
                "applied": applied}
        if missing:
            meta["not_actuated"] = missing
            meta["actuated_note"] = (
                f"this build of gr-sdrplay3 exposes no setter for "
                f"{', '.join(missing)}; the plan is recorded, not applied")
        return meta

    def set_gain_offset_db(self, offset_db: float) -> bool:
        """Shift IF gain reduction for the paired-gain intermod check."""
        cfg = self._config
        if getattr(cfg, "device_type", "").lower() != "rspdx":
            fn = getattr(self._source, "set_gain", None)
            if fn is None:
                return False
            try:
                fn(0, float(cfg.gain) + offset_db)
                return True
            except Exception:                            # pragma: no cover
                return False
        try:
            # SDRplay gain is a REDUCTION, so less gain is MORE reduction.
            self._source.set_gain(
                -(cfg.if_gain_reduction_db - offset_db), "IF")
            return True
        except Exception:                                # pragma: no cover
            return False

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
                        clip_fraction=self._clip.take())

    def close(self) -> None:
        if self._running:
            self._tb.stop()
            self._tb.wait()
            self._running = False
        if self._sink.dropped:
            logger.warning("dropped %d sweeps: the consumer could not keep "
                           "up with the integration rate", self._sink.dropped)
