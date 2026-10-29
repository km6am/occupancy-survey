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

import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from occupancy_survey.channels import BandPlan
from occupancy_survey.dataset import AcquisitionMetadata, IntervalDataset
from occupancy_survey.detector import (BandBaseline, ChannelLevelExtractor,
                                       ThresholdPolicy, baseline_grid,
                                       decide)
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
    front_end: dict | None = None
    # Visits contributing to this interval. The presence check needs it:
    # what absence proves depends on how many independent looks there were.
    visits: int = 0
    dwell_total_s: float = 0.0


class Survey:
    """Runs the schedule and emits one dataset per interval."""

    def __init__(self, bands, backend, site: SiteMetadata,
                 design: SamplingDesign | None = None,
                 policy: ThresholdPolicy | None = None,
                 histogram: HistogramSpec | None = None,
                 out_dir="./data", seed: int | None = None,
                 predicted_strongest: dict | None = None):
        self._bands: list[BandPlan] = list(bands)
        self._backend = backend
        self._site = site
        self._design = design or SamplingDesign()
        self._policy = policy or ThresholdPolicy()
        self._histogram = histogram or HistogramSpec()
        self._out_dir = out_dir
        # band name -> the channel the prediction says should be loudest.
        # Turns a presence check's one outcome into three: nothing at all
        # (receive path), wrong channel strongest (prediction or antenna
        # pattern), right channel (both agree).
        self._predicted_strongest = dict(predicted_strongest or {})
        self._schedule = Schedule(self._bands, self._design, seed=seed)

        self._baselines: dict[tuple[str, int], BandBaseline] = {}
        self._state: dict[str, _BandState] = {}
        self._interval_start: datetime | None = None
        self._written: list = []
        self._bandwidth_checked = False
        self._applied_front_end: str | None = None
        self._clock = lambda: datetime.now(timezone.utc)
        self._deadline: float | None = None
        self._status_interval_s = 0.0
        self._last_status = 0.0
        self._display = None
        self._current_visit: tuple[str, float] | None = None
        self._started_monotonic = time.monotonic()

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

    def _begin_interval(self, now: datetime, carry=None) -> None:
        self._interval_start = now
        self._state = {
            b.name: _BandState(BandAccumulator(b, b.channels(),
                                               self._histogram, self._policy))
            for b in self._bands
        }
        # An interval boundary is bookkeeping, not an observation break: the
        # receiver kept listening across it, so a transmission in progress
        # is carried into the new interval rather than discarded. Without
        # this a continuous-dwell keying measurement would lose one cycle
        # at every interval boundary.
        #
        # The per-visit band metadata is carried for a blunter reason: it is
        # established once when a visit starts, and a roll that happens
        # mid-visit would otherwise leave the new interval reporting zero
        # observed channels, so its band would be omitted and the file
        # never written at all.
        for name, carried in (carry or {}).items():
            st = self._state.get(name)
            if st is None:
                continue
            try:
                st.accumulator.adopt_partials(carried["partials"])
            except ValueError:
                pass
            st.bins_per_channel = carried["bins_per_channel"]
            st.channels_observed = carried["channels_observed"]
            st.front_end = carried["front_end"]
            # Visits are NOT carried: they are a property of the interval
            # being written, and the presence check must describe the looks
            # that produced the data in that file.
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
                        st.channels_observed, front_end=st.front_end,
                        visits=st.visits, dwell_total_s=st.dwell_total_s,
                        predicted_strongest_hz=self._predicted_strongest.get(
                            band.name))
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
            now_fn=None, status_interval_s: float = 0.0):
        """Run the schedule, writing one file per interval.

        Stops after `cycles` full passes, or after `duration_s`, whichever
        comes first; with neither it runs until interrupted.

        Both the duration limit and the interval boundary are enforced
        INSIDE a visit, not only between visits. A single-band continuous
        dwell -- which is how a keying period is measured, see
        docs/nwr-measurement.md -- has one visit that can last hours, and a
        check that only ran between visits would ignore --duration entirely
        and write one enormous file at the end.
        """
        self._clock = now_fn or (lambda: datetime.now(timezone.utc))
        self._deadline = (time.monotonic() + duration_s
                          if duration_s is not None else None)
        self._status_interval_s = float(status_interval_s)
        self._started_monotonic = time.monotonic()
        self._last_status = self._started_monotonic
        if self._status_interval_s > 0:
            from occupancy_survey.display import (StatusDisplay,
                                                  attach_logging_guard)
            self._display = StatusDisplay()
            # Printed before the first frame and before any data exists, so
            # the opening block scrolls away above a status that then
            # redraws in place below it.
            print(self.initial_statement(), flush=True)
            print(flush=True)
            attach_logging_guard(self._display)
        self._begin_interval(self._clock())

        cycle = 0
        try:
            while True:
                if cycles is not None and cycle >= cycles:
                    break
                for band, step, dwell in self._schedule.visits(cycles=1):
                    self._visit(band, step, dwell)
                    self._maybe_roll_interval()
                    self._maybe_status()
                    if self._past_deadline():
                        raise _Done
                cycle += 1
        except (_Done, KeyboardInterrupt):
            logger.info("stopping")
        finally:
            self._maybe_status(force=True)
            if self._display is not None:
                self._display.finish()
            self._end_interval(self._clock())

        return list(self._written)

    def status_snapshot(self) -> dict:
        """What has been detected so far, across the interval in progress.

        Deliberately reads live accumulator state rather than written
        files: with an hourly interval, a file-based status would tell an
        operator nothing for an hour, which is exactly the window in which
        they want to know the receiver is working.
        """
        bands, rows, controls = [], [], []
        for band in self._bands:
            st = self._state.get(band.name)
            if st is None or not st.visits:
                continue
            acc = st.accumulator
            bands.append(band.name)
            for r in acc.active_channels():
                rows.append({**r, "band": band.name})
            chk = acc.control_check()
            if chk is not None:
                controls.append((band.name, chk))
        rows.sort(key=lambda r: r["occupancy"], reverse=True)
        return {
            "elapsed_s": time.monotonic() - self._started_monotonic,
            "bands_visited": bands,
            "n_active": len(rows),
            "channels": rows,
            "controls": controls,
        }

    def initial_statement(self) -> str:
        """What is about to happen, printed before any data exists.

        An operator starting an unattended multi-day run should be able to
        see, in one block, that the thing they configured is the thing
        about to run -- site, receiver, bands, threshold and where the
        output goes -- rather than inferring it from the first status
        frame several minutes later.
        """
        from occupancy_survey.schedule import tune_steps

        summary = self._schedule.design_summary()
        n_channels = sum(len(b.channels()) for b in self._bands)
        steps = sum(len(tune_steps(b, self._design)) for b in self._bands)
        site = self._site
        recv = self._backend.receiver_metadata()
        where = site.grid_square or "location withheld"

        lines = [
            "Starting occupancy survey.",
            f"  site        {site.site_id} ({where})",
            f"  receiver    {site.receiver_model} / {site.antenna}",
            f"  backend     {recv.get('backend', '?')}"
            + ("   NO HARDWARE - produces no measurements"
               if recv.get("synthetic") else ""),
            f"  bands       {', '.join(b.name for b in self._bands)}",
            f"              {n_channels:,} channels, {steps} tune step(s)",
            f"  threshold   {self._policy.threshold_db:.1f} dB above a "
            f"{self._policy.baseline_percentile:.0f}th-percentile baseline",
            f"  schedule    {self._design.dwell_s:g} s dwell, "
            f"{summary['cycle_time_s']:.0f} s cycle, "
            f"{self._design.jitter_fraction:.0%} jitter",
            f"  blind spot  bursts shorter than "
            f"{summary['min_detectable_burst_s_at_6db_margin']*1000:.0f} ms",
            f"  output      {self._out_dir}  "
            f"(one file per {self._design.interval_s/60:.0f} min)",
        ]
        for w in summary.get("warnings", []):
            lines.append(f"  WARNING     {w}")
        lines.append("  Ctrl-C stops and finalises the interval in progress.")
        return "\n".join(lines)

    def format_status(self, max_rows: int = 6) -> str:
        """Per-band summary, with the busiest channels under it."""
        snap = self.status_snapshot()
        el = int(snap["elapsed_s"])
        clock = f"{el//3600:02d}:{(el//60)%60:02d}:{el%60:02d}"

        per_band = {b.name: {"visits": 0, "channels": 0, "active": 0,
                             "top": None, "control": None}
                    for b in self._bands}
        for band in self._bands:
            st = self._state.get(band.name)
            if st is None:
                continue
            e = per_band[band.name]
            e["visits"] = st.visits
            e["channels"] = len(st.accumulator.channels)
            rows = st.accumulator.active_channels()
            e["active"] = len(rows)
            e["top"] = rows[0] if rows else None
            # Only once the band has actually been visited: a verdict on
            # no data ("no_channels_observed") is noise next to a row that
            # already says "not yet visited".
            if st.visits:
                chk = st.accumulator.control_check()
                if chk is not None:
                    e["control"] = chk["verdict"]
            pres = getattr(band, "presence", None)
            if pres is not None and pres.enabled and e["control"] is None:
                e["control"] = "reference"

        width = max(len(n) for n in per_band) if per_band else 12
        out = [
            f"occupancy survey   {self._site.site_id}   elapsed {clock}   "
            f"{len(self._written)} file(s) written",
            f"  {'band':<{width}}  {'visits':>6}  {'chans':>6}  "
            f"{'active':>6}   busiest",
        ]
        for name, e in per_band.items():
            if e["top"]:
                busiest = (f"{e['top']['freq_hz']/1e6:10.4f} MHz "
                           f"{100*e['top']['occupancy']:5.1f}%")
            elif e["visits"]:
                busiest = "       -- nothing detected"
            else:
                busiest = "       -- not yet visited"
            tag = f"   [{e['control']}]" if e["control"] else ""
            out.append(f"  {name:<{width}}  {e['visits']:6d}  "
                       f"{e['channels']:6d}  {e['active']:6d}   "
                       f"{busiest}{tag}")

        out.append(f"  {snap['n_active']} channel(s) with a carrier across "
                   f"{len(snap['bands_visited'])} band(s) visited")

        if snap["channels"]:
            out.append("  busiest channels")
            for r in snap["channels"][:max_rows]:
                out.append(f"    {r['freq_hz']/1e6:10.4f} MHz  "
                           f"{100*r['occupancy']:5.1f}%  "
                           f"n={r['n_decided']:,}  {r['band']}")
            if snap["n_active"] > max_rows:
                out.append(f"    ... and {snap['n_active'] - max_rows} more")
        elif any(e["visits"] for e in per_band.values()):
            out.append("  nothing detected yet -- normal in the first "
                       "minutes; if it persists,")
            out.append("  check the antenna, the gain and the threshold")
        return "\n".join(out)

    def _maybe_status(self, force: bool = False) -> None:
        if self._status_interval_s <= 0 or self._display is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_status) < self._status_interval_s:
            return
        self._last_status = now
        self._display.render(self.format_status())

    def _past_deadline(self) -> bool:
        return (self._deadline is not None
                and time.monotonic() >= self._deadline)

    def _maybe_roll_interval(self) -> None:
        """Close and reopen the interval if it is due, carrying partials."""
        if self._interval_start is None:
            return
        now = self._clock()
        if (now - self._interval_start) < timedelta(
                seconds=self._design.interval_s):
            return
        in_progress = self._current_visit
        carry = {
            name: {"partials": st.accumulator.export_partials(),
                   "bins_per_channel": st.bins_per_channel,
                   "channels_observed": st.channels_observed,
                   "front_end": st.front_end}
            for name, st in self._state.items()
        }
        self._end_interval(now)
        self._begin_interval(now, carry=carry)
        if in_progress is not None:
            name, dwell = in_progress
            st = self._state.get(name)
            if st is not None:
                # One visit, not a fraction: the dwell is split across the
                # boundary but a visit is the unit the presence check
                # counts, and crediting it to both sides is closer than
                # crediting it to neither.
                st.visits = max(st.visits, 1)
                st.dwell_total_s = max(st.dwell_total_s, dwell)

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
        st = self._state[band.name]

        # Filters belong to the band, not the session. Applying them before
        # the retune means the noise floor settles once, not twice.
        if self._applied_front_end != band.name:
            st.front_end = self._backend.set_filters(band.front_end)
            self._applied_front_end = band.name
            # A notch change reshapes the passband, so every baseline taken
            # under the old filter state is now wrong.
            self._baselines.clear()
        elif st.front_end is None:
            st.front_end = self._backend.set_filters(band.front_end)

        self._backend.tune(step.center_hz, step.span_hz)
        axis = self._backend.freq_axis()
        if not self._bandwidth_checked:
            self._check_bandwidth(axis)

        channels = st.accumulator.channels
        extractor = ChannelLevelExtractor(
            channels, axis, usable_fraction=self._design.usable_fraction)

        # The noise floor is measured from a uniform grid across the whole
        # passband, NOT from the channels being recorded. For a grid band
        # the two would agree; for a TARGETED band they would not, because
        # every targeted channel is there precisely because a transmitter
        # is expected on it. Pooling the baseline over those would let it
        # ride up into the signals and bias occupancy down. See
        # detector.baseline_grid().
        grid = baseline_grid(step.center_hz,
                             step.span_hz * self._design.usable_fraction,
                             band.channel_bandwidth_hz)
        grid_extractor = ChannelLevelExtractor(
            grid, axis, usable_fraction=self._design.usable_fraction)

        key = (band.name, step.index)
        baseline = self._baselines.get(key)
        if baseline is None:
            baseline = BandBaseline(self._policy)
            self._baselines[key] = baseline

        st.bins_per_channel = max(st.bins_per_channel,
                                  extractor.bins_per_channel)
        st.channels_observed = max(st.channels_observed, extractor.n_observed)
        st.visits += 1
        st.dwell_total_s += float(dwell_s)
        # Remembered so that an interval boundary falling INSIDE this visit
        # can credit the new interval with it. Without that the new
        # interval reports zero visits while its accumulator fills, which
        # is visibly wrong on the status display and quietly wrong in the
        # presence check, whose detectability bound is a function of how
        # many visits there were.
        self._current_visit = (band.name, float(dwell_s))

        if extractor.n_observed == 0:
            logger.warning(
                "%s step %d/%d: no channel fits the usable passband at "
                "%.3f MHz -- check usable_bandwidth_hz against the band's "
                "channel widths", band.name, step.index + 1, step.of,
                step.center_hz / 1e6)
            return

        # Leaving a band is an observation break. Marked at the START of a
        # visit as well as the end, so an interrupted run never survives a
        # retune even if a previous visit ended abnormally.
        st.accumulator.mark_observation_break()
        try:
            for sweep in self._backend.sweeps(dwell_s):
                b = baseline.update(
                    grid_extractor.levels_db(sweep.spectrum_db))
                levels = extractor.levels_db(sweep.spectrum_db)
                # Re-read the state each sweep: a mid-visit interval roll
                # replaces the accumulator underneath us.
                self._state[band.name].accumulator.add_sweep(
                    decide(levels, b, self._policy),
                    clip_fraction=sweep.clip_fraction)
                self._maybe_roll_interval()
                if self._past_deadline():
                    break
        finally:
            self._current_visit = None
        self._state[band.name].accumulator.mark_observation_break()


    def intermod_check(self, band: BandPlan, step, sweeps: int = 40,
                       gain_step_db: float = 6.0) -> dict | None:
        """Paired-gain measurement on one tune step.

        Returns None when the backend cannot change gain, rather than
        faking a pass -- a diagnostic that cannot run must say so.

        The two measurements are taken back to back on the same tuning so
        that as little as possible changes between them. Intermittent
        traffic is still a confound: a channel that stops transmitting
        between the two looks like a high-order product, which is why
        intermod.classify() only assesses channels present in both and why
        the result is a suspicion rather than a verdict.
        """
        from occupancy_survey.intermod import classify, summarise

        # Deliberately independent of interval state: this is a diagnostic
        # that should be runnable before any survey has started, which is
        # exactly when an operator wants it.
        front_end = self._backend.set_filters(band.front_end)
        self._backend.tune(step.center_hz, step.span_hz)
        axis = self._backend.freq_axis()

        channels = band.channels()
        extractor = ChannelLevelExtractor(
            channels, axis, usable_fraction=self._design.usable_fraction)
        grid = baseline_grid(step.center_hz,
                             step.span_hz * self._design.usable_fraction,
                             band.channel_bandwidth_hz)
        grid_extractor = ChannelLevelExtractor(
            grid, axis, usable_fraction=self._design.usable_fraction)

        def measure() -> np.ndarray:
            baseline = BandBaseline(self._policy)
            rows = []
            for i, sweep in enumerate(self._backend.sweeps(
                    sweeps * self._design.integration_s)):
                b = baseline.update(
                    grid_extractor.levels_db(sweep.spectrum_db))
                if b is None:
                    continue
                levels = extractor.levels_db(sweep.spectrum_db)
                rows.append(levels - b)
                if len(rows) >= sweeps:
                    break
            if not rows:
                return np.full(len(channels), np.nan)
            # Median over sweeps: robust to a channel keying mid-measurement.
            return np.nanmedian(np.vstack(rows), axis=0)

        before = measure()
        if not self._backend.set_gain_offset_db(-abs(gain_step_db)):
            logger.warning("%s: backend cannot change gain; intermod check "
                           "skipped", band.name)
            return None
        try:
            after = measure()
        finally:
            self._backend.set_gain_offset_db(0.0)

        results = classify([c.freq_hz for c in channels], before, after,
                           abs(gain_step_db),
                           present_threshold_db=self._policy.threshold_db)
        out = summarise(results)
        out["band"] = band.name
        out["center_hz"] = step.center_hz
        out["front_end"] = front_end
        return out


class _Done(Exception):
    """Internal: duration reached."""
