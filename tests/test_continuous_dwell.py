"""A single-band continuous dwell: how a keying period gets measured.

The normal schedule cannot answer that question -- a 20 s dwell cannot
measure a carrier-on period of minutes, and the ~287 s revisit overlaps the
180-360 s NWR programme cycle, which is the commensurate-period aliasing
docs/sampling-design.md section 5 warns about. So the measurement needs one
band, one tuning, one uninterrupted dwell, and the runner has to behave
correctly for a visit that lasts hours.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import resolve_bands
from occupancy_survey.dataset import read_dataset
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter

SITE = dict(site_id="nwr", receiver_model="RSPdx", antenna="discone")


def _survey(out_dir, interval_s=3600.0, emitters=()):
    design = SamplingDesign(
        integration_s=0.01, dwell_s=86400.0, revisit_target_s=86400.0,
        jitter_fraction=0.0, interval_s=interval_s,
        usable_bandwidth_hz=2.048e6)
    backend = SyntheticBackend(emitters, integration_s=0.01,
                               sample_rate_hz=2.048e6)
    return Survey(resolve_bands(["nwr"]), backend, SiteMetadata(**SITE),
                  design, out_dir=out_dir)


class TestDurationDuringALongVisit(unittest.TestCase):

    def test_duration_stops_a_visit_that_would_outlast_it(self):
        """The bug: --duration was only checked BETWEEN visits.

        With a dwell longer than the run, there is never a second visit, so
        a duration limit checked only between visits would be ignored
        entirely and the run would continue for the whole dwell.
        """
        import time
        with tempfile.TemporaryDirectory() as d:
            started = time.monotonic()
            _survey(d).run(duration_s=1.0)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20.0,
                        f"a 1 s duration inside an 86400 s dwell ran for "
                        f"{elapsed:.1f} s; the deadline is not being "
                        f"checked inside the sweep loop")


class TestIntervalRollMidVisit(unittest.TestCase):

    def test_a_long_visit_still_produces_periodic_files(self):
        """One enormous file at the end risks losing everything to a crash."""
        clock = {"t": datetime(2026, 10, 28, tzinfo=timezone.utc)}

        def now_fn():
            clock["t"] += timedelta(seconds=20)
            return clock["t"]

        with tempfile.TemporaryDirectory() as d:
            paths = _survey(d, interval_s=60.0).run(duration_s=2.0,
                                                    now_fn=now_fn)
            self.assertGreaterEqual(
                len(paths), 2,
                "a continuous dwell must still roll its interval; otherwise "
                "a keying run writes nothing until it finishes")
            self.assertEqual(len({p.name for p in paths}), len(paths))

    def test_a_transmission_spanning_a_boundary_is_not_lost(self):
        """An interval boundary is bookkeeping, not an observation break.

        A run interrupted by a RETUNE is a lower bound and is discarded. A
        run that merely straddles an interval boundary is complete -- the
        receiver never stopped listening -- so it is carried and recorded
        in the interval where it completes. Without the carry, a continuous
        keying measurement would lose a cycle at every boundary.
        """
        from occupancy_survey.occupancy import (BandAccumulator,
                                                HistogramSpec)
        from occupancy_survey.detector import SweepDecision, ThresholdPolicy
        import numpy as np

        plan = resolve_bands(["nwr"])[0]
        policy = ThresholdPolicy()
        chans = plan.channels()[:1]

        def sweep(present):
            return SweepDecision(
                baseline_db=0.0,
                levels_db=np.array([20.0 if present else 0.0]),
                excess_db=np.array([20.0 if present else 0.0]),
                present=np.array([present]), decided=np.array([True]))

        first = BandAccumulator(plan, chans, HistogramSpec(), policy)
        for _ in range(6):
            first.add_sweep(sweep(True))        # carrier on, boundary hits
        carried = first.export_partials()
        self.assertEqual(carried[0][0], 6, "6 sweeps of run in progress")

        second = BandAccumulator(plan, chans, HistogramSpec(), policy)
        second.adopt_partials(carried)
        for _ in range(4):
            second.add_sweep(sweep(True))       # ...continues
        second.add_sweep(sweep(False))          # ...then stops

        rec = second.channel_records()[0]
        self.assertEqual(rec["runs"]["max_sweeps"], 10,
                         "the run must total 10 sweeps across the boundary, "
                         "not be split into 6 and 4 and both discarded")


class TestKeyingThroughTheRunner(unittest.TestCase):

    def test_a_keyed_carrier_yields_a_period_end_to_end(self):
        f = 162_475_000.0
        em = [SyntheticEmitter(f, snr_db=28.0, duty=0.25, period_sweeps=40,
                               bandwidth_hz=14_000.0)]
        with tempfile.TemporaryDirectory() as d:
            paths = _survey(d, emitters=em).run(duration_s=3.0)
            self.assertTrue(paths)
            data = read_dataset(paths[-1])

        band = data["bands"]["nwr"]
        self.assertEqual(band["role"], "target",
                         "NWR must be a target; a control_check here would "
                         "gate the run on a retracted assumption")
        self.assertIsNone(band["control_check"])
        self.assertIn("integration_s", band)

        rec = {c["freq_hz"]: c for c in band["channels"]}[f]
        self.assertAlmostEqual(rec["occupancy"], 0.25, delta=0.05)
        self.assertGreater(rec["keying"]["cycles_observed"], 3)
        self.assertAlmostEqual(rec["keying"]["period_sweeps"], 40.0,
                               delta=4.0)


if __name__ == "__main__":
    unittest.main()


class TestWarningSuppression(unittest.TestCase):
    """Three spurious warnings on a correct config teach people to ignore
    warnings, so a deliberate continuous dwell must not trip them."""

    def _schedule(self, dwell_s, interval_s, bands=("nwr",)):
        from occupancy_survey.schedule import Schedule
        design = SamplingDesign(integration_s=0.1, dwell_s=dwell_s,
                                revisit_target_s=dwell_s,
                                jitter_fraction=0.0, interval_s=interval_s,
                                usable_bandwidth_hz=2.048e6)
        return Schedule(resolve_bands(list(bands)), design)

    def test_a_continuous_dwell_is_recognised_not_warned_about(self):
        sched = self._schedule(dwell_s=86400.0, interval_s=1800.0)
        self.assertTrue(sched.continuous_dwell)
        warnings = sched.warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("continuous-dwell configuration", warnings[0])
        for phrase in ("exceeds revisit_target_s", "alias",
                       "sweeps per visit"):
            self.assertNotIn(phrase, warnings[0])

    def test_a_normal_schedule_still_gets_its_warnings(self):
        sched = self._schedule(dwell_s=20.0, interval_s=3600.0,
                               bands=("vhf_lmr_low", "nwr", "uhf_lmr"))
        self.assertFalse(sched.continuous_dwell)
        self.assertTrue(any("alias" in w for w in sched.warnings()),
                        "jitter_fraction=0 on a stepping schedule is still "
                        "a real problem and must still warn")


class TestVisitCountAcrossIntervalBoundary(unittest.TestCase):
    """A band cannot have detections with zero visits.

    Spotted on the live status display during a demonstration run: a band
    showed `visits 0` beside an active channel at 55.6%. An interval
    boundary had fallen INSIDE a visit, and _begin_interval resets the
    visit count while the sweep loop carries on filling the new
    accumulator -- so the new interval reported no visits while its data
    accumulated.

    Visibly wrong on screen, and quietly wrong in the presence check,
    whose detectability bound is a function of how many visits there were:
    under-counting makes it claim less sensitivity than it had.
    """

    def test_a_roll_inside_a_visit_still_credits_the_visit(self):
        from datetime import datetime, timedelta, timezone

        from occupancy_survey.backends.synthetic import SyntheticBackend
        from occupancy_survey.channels import resolve_bands
        from occupancy_survey.dataset import read_dataset
        from occupancy_survey.schedule import SamplingDesign
        from occupancy_survey.site import SiteMetadata
        from occupancy_survey.survey import Survey

        # A clock that jumps a full interval on every reading, so a
        # boundary lands inside essentially every visit.
        clock = {"t": datetime(2026, 10, 29, tzinfo=timezone.utc)}

        def now_fn():
            clock["t"] += timedelta(seconds=40)
            return clock["t"]

        design = SamplingDesign(integration_s=0.01, dwell_s=1.0,
                                interval_s=30.0, usable_bandwidth_hz=2.048e6)
        em = [SyntheticEmitter(162_475_000.0, 28.0, duty=1.0,
                               bandwidth_hz=14_000.0)]
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["nwr"]),
                           SyntheticBackend(em, integration_s=0.01,
                                            sample_rate_hz=2.048e6),
                           SiteMetadata("s", "RSPdx", "discone"), design,
                           out_dir=d).run(cycles=4, now_fn=now_fn)
            datasets = [read_dataset(p) for p in paths]

        with_data = [d for d in datasets
                     if d["bands"].get("nwr", {}).get("channels")]
        self.assertTrue(with_data, "expected at least one interval with data")
        for ds in with_data:
            self.assertGreater(
                ds["bands"]["nwr"]["visits"], 0,
                "an interval holding channel data must report at least one "
                "visit; zero would understate the presence check's "
                "detectability bound")
