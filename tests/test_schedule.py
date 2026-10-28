"""The sampling design and its stated bounds."""

import unittest

from occupancy_survey.channels import BAND_PLANS, DEFAULT_SCHEDULE, resolve_bands
from occupancy_survey.schedule import (SamplingDesign, Schedule,
                                       blind_spot_table, catch_probability,
                                       cycle_time_s,
                                       effective_independent_samples,
                                       min_detectable_burst_s,
                                       occupancy_standard_error, tune_steps)


class TestTuneSteps(unittest.TestCase):

    def test_steps_cover_the_band_without_gaps(self):
        design = SamplingDesign(usable_bandwidth_hz=6e6, usable_fraction=0.8)
        band = BAND_PLANS["uhf_lmr"]
        steps = tune_steps(band, design)
        self.assertGreater(len(steps), 1, "20 MHz needs several tunings")

        # Each channel of the band must fall inside the usable part of at
        # least one step, or it is simply never measured.
        usable_half = 0.5 * design.usable_bandwidth_hz * design.usable_fraction
        for ch in band.channels():
            covered = any(
                abs(ch.freq_hz - s.center_hz) + 0.5 * ch.bandwidth_hz
                <= usable_half for s in steps)
            self.assertTrue(covered,
                            f"{ch.freq_hz/1e6:.4f} MHz is in no usable span")

    def test_a_narrow_band_is_one_step(self):
        self.assertEqual(len(tune_steps(BAND_PLANS["nwr"], SamplingDesign())), 1)

    def test_cycle_time_scales_with_tune_steps(self):
        design = SamplingDesign(dwell_s=10.0)
        narrow = cycle_time_s([BAND_PLANS["nwr"]], design)
        wide = cycle_time_s([BAND_PLANS["uhf_lmr"]], design)
        self.assertGreater(wide, 4 * narrow,
                           "a 20 MHz band costs several times a 150 kHz one")


class TestBlindSpot(unittest.TestCase):

    def test_zero_margin_cannot_survive_any_averaging(self):
        self.assertAlmostEqual(min_detectable_burst_s(0.1, 0.0), 0.1)
        self.assertAlmostEqual(min_detectable_burst_s(0.1, -5.0), 0.1)

    def test_ten_db_of_margin_buys_a_factor_of_ten(self):
        self.assertAlmostEqual(min_detectable_burst_s(0.1, 10.0), 0.01,
                               places=6)
        self.assertAlmostEqual(min_detectable_burst_s(0.1, 20.0), 0.001,
                               places=7)

    def test_the_motivating_case_is_actually_a_blind_spot(self):
        """The task's example: 3 s every 5 minutes -- a 1% duty cycle.

        Against a 1 s dwell every 10 minutes it is near-invisible, which is
        the point the example makes. The number worth pinning, though, is
        what the DEFAULT design does with it, because that is what will
        actually be run: 62% in one hour. Not 95%, not "fine". A 1% channel
        is not reliably even DETECTED in an hour by this design, let alone
        measured, and the figure is asserted here so that a later change to
        the defaults cannot quietly improve it on paper.
        """
        poor = catch_probability(burst_s=3.0, period_s=300.0, dwell_s=1.0,
                                 n_visits=6)
        self.assertLess(poor, 0.10,
                        "a 1 s dwell every 10 minutes should almost never "
                        f"catch this, got {poor:.3f}")

        design = SamplingDesign()
        one_hour = catch_probability(burst_s=3.0, period_s=300.0,
                                     dwell_s=design.dwell_s, n_visits=12)
        self.assertAlmostEqual(
            one_hour, 0.62, delta=0.02,
            msg=f"the default design catches a 1% channel {one_hour:.2f} of "
                f"the time in an hour; docs/sampling-design.md states 0.62 "
                f"and both must move together")

    def test_detecting_a_one_percent_channel_takes_hours_not_an_hour(self):
        """How much airtime the stated blind spot actually costs.

        Detection and estimation are different budgets. For a 1% channel on
        the default schedule: ~3 hours to be 95% sure of seeing it at all,
        ~13 hours to pin its duty cycle to within 30% relative. Both figures
        appear in docs/sampling-design.md and this pins them, so the doc
        cannot drift away from the code.
        """
        design = SamplingDesign()
        burst, period = 3.0, 300.0
        cycle = cycle_time_s(resolve_bands(DEFAULT_SCHEDULE), design)

        visits_for_95pct = next(
            n for n in range(1, 10_000)
            if catch_probability(burst, period, design.dwell_s, n) >= 0.95)
        self.assertEqual(visits_for_95pct, 38)
        self.assertAlmostEqual(visits_for_95pct * cycle / 3600.0, 3.0,
                               delta=0.2)

        # Estimating 1% to within 30% relative needs ~1100 effective samples.
        p = burst / period
        target_se = 0.3 * p
        n_eff_needed = p * (1 - p) / target_se ** 2
        per_visit = effective_independent_samples(
            design.dwell_s, design.integration_s, burst, n_visits=1)
        visits_for_estimate = n_eff_needed / per_visit

        self.assertAlmostEqual(visits_for_estimate * cycle / 3600.0, 13.0,
                               delta=1.0)
        self.assertGreater(
            visits_for_estimate, 4 * visits_for_95pct,
            "estimating a 1% duty cycle must cost several times the airtime "
            "of merely detecting the channel -- if these converge, "
            "effective_independent_samples() is counting correlated sweeps "
            "as independent again")

    def test_catch_probability_is_monotone_and_bounded(self):
        self.assertEqual(catch_probability(5.0, 10.0, 20.0, 1), 1.0,
                         "dwell longer than the period must be certain")
        self.assertEqual(catch_probability(1.0, 100.0, 1.0, 0), 0.0)
        a = catch_probability(1.0, 600.0, 20.0, 1)
        b = catch_probability(1.0, 600.0, 20.0, 10)
        self.assertLess(a, b)
        self.assertLessEqual(b, 1.0)

    def test_correlated_sweeps_are_not_independent_samples(self):
        """The precision claim: treating each sweep as independent overstates
        it by about sqrt(burst/integration)."""
        design = SamplingDesign(dwell_s=20.0, integration_s=0.1)
        naive = design.sweeps_per_visit * 12
        honest = effective_independent_samples(
            design.dwell_s, design.integration_s, burst_s=5.0, n_visits=12)
        self.assertLess(honest, naive / 10,
                        "a 5 s transmission sampled every 100 ms gives far "
                        "fewer independent samples than sweeps")

        se_naive = occupancy_standard_error(0.2, naive)
        se_honest = occupancy_standard_error(0.2, honest)
        self.assertGreater(se_honest, 5 * se_naive,
                           "the honest error bar must be several times the "
                           "naive one, which is the point of reporting it")

    def test_a_dwell_shorter_than_a_transmission_is_one_sample(self):
        n = effective_independent_samples(dwell_s=2.0, integration_s=0.1,
                                          burst_s=30.0, n_visits=4)
        self.assertEqual(n, 4.0)

    def test_no_uncertainty_is_reported_as_nan_not_zero(self):
        import math
        self.assertTrue(math.isnan(occupancy_standard_error(0.2, 0)))

    def test_blind_spot_table_marks_undetectable_rows(self):
        design = SamplingDesign(integration_s=1.0)
        rows = blind_spot_table(design, n_visits=6)
        undetectable = [r for r in rows if not r["detectable"]]
        self.assertTrue(undetectable,
                        "a 1 s integration period must have a blind spot for "
                        "the shortest bursts in the table")
        for r in undetectable:
            self.assertLess(r["burst_s"], design.integration_s)


class TestSchedule(unittest.TestCase):

    def test_jitter_varies_the_dwell_within_bounds(self):
        design = SamplingDesign(jitter_fraction=0.25)
        sched = Schedule(resolve_bands(["nwr"]), design, seed=4)
        dwells = [sched.jittered_dwell_s() for _ in range(200)]
        self.assertGreater(max(dwells) - min(dwells), 0.0,
                           "jitter must actually vary the dwell")
        for d in dwells:
            self.assertGreaterEqual(d, design.dwell_s * 0.75 - 1e-9)
            self.assertLessEqual(d, design.dwell_s * 1.25 + 1e-9)

    def test_no_jitter_is_a_recorded_warning_not_a_silent_choice(self):
        sched = Schedule(resolve_bands(["nwr"]),
                         SamplingDesign(jitter_fraction=0.0))
        self.assertTrue(any("alias" in w for w in sched.warnings()))

    def test_a_schedule_that_cannot_meet_its_target_says_so(self):
        design = SamplingDesign(dwell_s=120.0, revisit_target_s=60.0)
        sched = Schedule(resolve_bands(DEFAULT_SCHEDULE), design)
        warnings = sched.warnings()
        self.assertTrue(any("exceeds revisit_target_s" in w for w in warnings),
                        f"expected a revisit warning, got {warnings}")

    def test_a_short_visit_is_a_recorded_warning(self):
        sched = Schedule(resolve_bands(["nwr"]),
                         SamplingDesign(dwell_s=1.0, integration_s=0.1))
        self.assertTrue(any("sweeps per visit" in w for w in sched.warnings()))

    def test_visits_cover_every_tune_step_each_cycle(self):
        design = SamplingDesign()
        bands = resolve_bands(DEFAULT_SCHEDULE)
        sched = Schedule(bands, design, seed=2)
        visits = list(sched.visits(cycles=2))
        self.assertEqual(len(visits), 2 * len(sched.steps))
        seen = {(b.name, st.index) for b, st, _ in visits}
        self.assertEqual(len(seen), len(sched.steps))

    def test_design_summary_carries_the_bounds(self):
        sched = Schedule(resolve_bands(DEFAULT_SCHEDULE), SamplingDesign())
        summary = sched.design_summary()
        for key in ("threshold" if False else "integration_s", "dwell_s",
                    "cycle_time_s", "min_detectable_burst_s_at_6db_margin",
                    "blind_spot", "warnings"):
            self.assertIn(key, summary)

    def test_invalid_designs_are_refused(self):
        with self.assertRaises(ValueError):
            SamplingDesign(dwell_s=0.01, integration_s=1.0)
        with self.assertRaises(ValueError):
            SamplingDesign(jitter_fraction=1.0)
        with self.assertRaises(ValueError):
            SamplingDesign(integration_s=0.0)


if __name__ == "__main__":
    unittest.main()
