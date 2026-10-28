"""The runner: schedule a band, measure it, write an interval file.

Holds the pieces together and owns two invariants that are easy to break
and silent when broken.

ONE POLICY OBJECT. The threshold policy that builds the histograms is the
same object whose values are written into the metadata. A dataset whose
stated threshold does not match the data it describes is wrong in a way
nothing in the file can reveal, so the policy is passed in once and never
rebuilt.

A BASELINE BELONGS TO ONE CONFIGURATION. Baselines are keyed by (band, tune
step) and reset on every retune. Carrying a baseline across a retune or a
gain change would read the receiver's own configuration change as a
band-wide change in activity.

THE DESIGN'S BANDWIDTH MUST MATCH THE RECEIVER'S. `tune_steps()` decomposes
a wide band assuming `design.usable_bandwidth_hz`, and the backend delivers
whatever its sample rate gives. If the design assumes more than the receiver
provides, the steps are spaced too far apart and channels fall into the gaps
between them -- and they do not appear as errors, they appear as channels
that are quiet. That is the worst failure this tool could have, so it is
checked on the first tune and raised, not warned about.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from occupancy_survey.channels import BandPlan
from occupancy_survey.dataset import AcquisitionMetadata, IntervalDataset
from occupancy_survey.detector import (BandBaseline, ChannelLevelExtractor,
                                       ThresholdPolicy, decide)
from occupancy_survey.occupancy import BandAccumulator, HistogramSpec
from occupancy_survey.schedule import SamplingDesign, Schedule
from occupancy_survey.site import SiteMetadata

logger = logging.getLogger(__name__)


@dataclass
class _BandState:
    """One band's accumulator for the interval in progress."""
    accumulator: BandAccumulator
    bins_per_channel: int = 0
    channels_observed: int = 0


class Survey:
    """Runs the schedule and emits one dataset per interval."""

    def __init__(self, bands, backend, site: SiteMetadata,
                 design: SamplingDesign | None = None,
                 policy: ThresholdPolicy | None = None,
                 histogram: HistogramSpec | None = None,
                 out_dir="./data", seed: int | None = None):
        self._bands: list[BandPlan] = list(bands)
        self._backend = backend
        self._site = site
        self._design = design or SamplingDesign()
        self._policy = policy or ThresholdPolicy()
        self._histogram = histogram or HistogramSpec()
        self._out_dir = out_dir
        self._schedule = Schedule(self._bands, self._design, seed=seed)

        self._baselines: dict[tuple[str, int], BandBaseline] = {}
        self._state: dict[str, _BandState] = {}
        self._interval_start: datetime | None = None
        self._written: list = []
        self._bandwidth_checked = False

        for w in self._schedule.warnings():
            logger.warning("sampling design: %s", w)

    # ----------------------------------------------------------------- #

    def _metadata(self) -> AcquisitionMetadata:
        return AcquisitionMetadata(
            site=self._site.as_metadata(),
            sampling=self._schedule.design_summary(),
            detection=self._policy.as_metadata(),
            histogram=self._histogram.as_metadata(),
            receiver=dict(self._backend.receiver_metadata()),
        )

    def _begin_interval(self, now: datetime) -> None:
        self._interval_start = now
        self._state = {
            b.name: _BandState(BandAccumulator(b, b.channels(),
                                               self._histogram, self._policy))
            for b in self._bands
        }
        # A new interval gets fresh baselines. The alternative -- carrying
        # them -- would make the first minutes of each interval depend on
        # the last minutes of the previous one, which is harmless until an
        # interval boundary happens to land on a gain change.
        self._baselines.clear()

    def _end_interval(self, now: datetime):
        if self._interval_start is None:
            return None
        ds = IntervalDataset(metadata=self._metadata(),
                             interval_start=self._interval_start,
                             interval_end=now)
        for band in self._bands:
            st = self._state.get(band.name)
            if st is None or st.channels_observed == 0:
                # A band with no samples this interval is omitted rather
                # than written as all-zero. Occupancy null, not zero.
                continue
            ds.add_band(band, st.accumulator, st.bins_per_channel,
                        st.channels_observed)
        if not ds.bands:
            logger.warning("interval produced no observed bands; nothing "
                           "written")
            self._interval_start = None
            return None
        path = ds.write(self._out_dir)
        self._written.append(path)
        logger.info("wrote %s", path)
        self._interval_start = None
        return path

    # ----------------------------------------------------------------- #

    def run(self, cycles: int | None = None, duration_s: float | None = None,
            now_fn=None):
        """Run the schedule, writing one file per interval.

        Stops after `cycles` full passes, or after `duration_s`, whichever
        comes first; with neither it runs until interrupted.
        """
        clock = now_fn or (lambda: datetime.now(timezone.utc))
        started = time.monotonic()
        self._begin_interval(clock())

        cycle = 0
        try:
            while True:
                if cycles is not None and cycle >= cycles:
                    break
                for band, step, dwell in self._schedule.visits(cycles=1):
                    self._visit(band, step, dwell)

                    now = clock()
                    if self._interval_start is not None and \
                            (now - self._interval_start) >= timedelta(
                                seconds=self._design.interval_s):
                        self._end_interval(now)
                        self._begin_interval(now)

                    if duration_s is not None and \
                            time.monotonic() - started >= duration_s:
                        raise _Done
                cycle += 1
        except (_Done, KeyboardInterrupt):
            logger.info("stopping")
        finally:
            self._end_interval(clock())

        return list(self._written)

    def _check_bandwidth(self, axis) -> None:
        """Refuse a design that assumes more bandwidth than the receiver has.

        Spacing tune steps by an assumed 6 MHz while the receiver delivers
        2 MHz leaves two thirds of every band unobserved, and the symptom is
        not an error but a band full of quiet channels. Checked once, on the
        first tune, against the axis the backend actually returned.
        """
        actual = float(axis[-1] - axis[0]) + float(axis[1] - axis[0])
        assumed = self._design.usable_bandwidth_hz
        if actual < assumed * 0.98:
            raise ValueError(
                f"sampling design assumes usable_bandwidth_hz="
                f"{assumed/1e6:.3f} MHz but the backend delivers "
                f"{actual/1e6:.3f} MHz. Tune steps would be spaced "
                f"{assumed/1e6:.3f} MHz apart and "
                f"{100*(1-actual/assumed):.0f}% of every band would never be "
                f"observed -- appearing as quiet channels, not as an error. "
                f"Set usable_bandwidth_hz to {actual/1e6:.3f} MHz, or raise "
                f"the backend's sample rate."
            )
        self._bandwidth_checked = True

    def _visit(self, band: BandPlan, step, dwell_s: float) -> None:
        self._backend.tune(step.center_hz, step.span_hz)
        axis = self._backend.freq_axis()
        if not self._bandwidth_checked:
            self._check_bandwidth(axis)

        channels = self._state[band.name].accumulator.channels
        extractor = ChannelLevelExtractor(
            channels, axis, usable_fraction=self._design.usable_fraction)

        key = (band.name, step.index)
        baseline = self._baselines.get(key)
        if baseline is None:
            baseline = BandBaseline(self._policy)
            self._baselines[key] = baseline

        st = self._state[band.name]
        st.bins_per_channel = max(st.bins_per_channel,
                                  extractor.bins_per_channel)
        st.channels_observed = max(st.channels_observed, extractor.n_observed)

        if extractor.n_observed == 0:
            logger.warning(
                "%s step %d/%d: no channel fits the usable passband at "
                "%.3f MHz -- check usable_bandwidth_hz against the band's "
                "channel widths", band.name, step.index + 1, step.of,
                step.center_hz / 1e6)
            return

        for sweep in self._backend.sweeps(dwell_s):
            levels = extractor.levels_db(sweep.spectrum_db)
            st.accumulator.add_sweep(
                decide(levels, baseline.update(levels), self._policy),
                clip_fraction=sweep.clip_fraction)


class _Done(Exception):
    """Internal: duration reached."""
