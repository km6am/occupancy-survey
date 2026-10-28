"""Recovering a carrier's keying period, not just its duty cycle.

Motivated by the NOAA Weather Radio question: NWR was assumed to be a
continuous carrier and demoted to a measurement target on 2026-10-28 when
an operator reported finite transmissions with dead time between them.

The distinction this has to resolve is one a listener cannot make:

    dead AUDIO, carrier up  -> interference duty is still 100%, because a
                               carrier desenses a front end whether or not
                               it is modulated
    dead CARRIER            -> genuinely below 100%

Both are silence from a speaker. Carrier-present logging separates them
trivially -- but only if the gap lengths and the period come out right,
which is what these tests check.
"""

import unittest

import numpy as np

from occupancy_survey.channels import BandPlan, TARGET
from occupancy_survey.detector import (BandBaseline, ChannelLevelExtractor,
                                       ThresholdPolicy, decide)
from occupancy_survey.occupancy import BandAccumulator, HistogramSpec
from tests.synthetic_band import SyntheticBand, SyntheticEmitter

PLAN = BandPlan(name="nwr_test", freq_min_hz=162_400_000.0,
                freq_max_hz=162_550_000.0, channel_step_hz=25_000.0,
                channel_bandwidth_hz=16_000.0, service="NWR", role=TARGET)


def _run_continuous(emitters, n_sweeps, policy=None):
    """One uninterrupted dwell -- no retunes, so no observation breaks."""
    policy = policy or ThresholdPolicy()
    channels = PLAN.channels()
    band = SyntheticBand(PLAN.freq_min_hz - 60_000.0,
                         PLAN.freq_max_hz + 60_000.0, bin_hz=1_000.0,
                         emitters=emitters, seed=4)
    ex = ChannelLevelExtractor(channels, band.freq_axis, usable_fraction=1.0)
    baseline = BandBaseline(policy)
    acc = BandAccumulator(PLAN, channels, HistogramSpec(), policy)
    for i in range(n_sweeps):
        levels = ex.levels_db(band.sweep(i))
        acc.add_sweep(decide(levels, baseline.update(levels), policy))
    return {r["freq_hz"]: r for r in acc.channel_records()}


class TestKeyedCarrier(unittest.TestCase):
    """A carrier that keys on and off on a known period."""

    def test_the_period_and_duty_both_come_back(self):
        # 40-sweep period, 25% on. At 0.1 s integration that is a 4 s
        # cycle; the shape of the question is the same for a 3-6 minute one
        # and far cheaper to simulate.
        f = 162_475_000.0
        rec = _run_continuous(
            [SyntheticEmitter(f, snr_db=28.0, duty=0.25, period_sweeps=40,
                              bandwidth_hz=14_000.0)], n_sweeps=1600)[f]

        self.assertAlmostEqual(rec["occupancy"], 0.25, delta=0.03)

        keying = rec["keying"]
        self.assertGreater(keying["cycles_observed"], 20)
        self.assertAlmostEqual(keying["period_sweeps"], 40.0, delta=2.0)
        self.assertAlmostEqual(keying["on_sweeps_median"], 10.0, delta=2.0)
        self.assertAlmostEqual(keying["off_sweeps_median"], 30.0, delta=2.0)
        self.assertAlmostEqual(keying["duty_from_cycles"], 0.25, delta=0.03)

    def test_a_clock_driven_cycle_reads_as_regular(self):
        f = 162_475_000.0
        rec = _run_continuous(
            [SyntheticEmitter(f, snr_db=28.0, duty=0.5, period_sweeps=30,
                              bandwidth_hz=14_000.0)], n_sweeps=1200)[f]
        self.assertLess(rec["keying"]["regularity"], 0.15,
                        "a square, clock-driven cycle should read as "
                        "regular; traffic keyed by people should not")

    def test_gaps_are_recorded_not_only_runs(self):
        f = 162_475_000.0
        rec = _run_continuous(
            [SyntheticEmitter(f, snr_db=28.0, duty=0.25, period_sweeps=40,
                              bandwidth_hz=14_000.0)], n_sweeps=1200)[f]
        self.assertGreater(rec["gaps"]["n"], 10)
        self.assertAlmostEqual(rec["gaps"]["median_sweeps"], 30.0, delta=3.0)
        self.assertGreater(rec["gaps"]["median_sweeps"],
                           rec["runs"]["median_sweeps"],
                           "a 25% duty cycle must have gaps longer than runs")


class TestContinuousCarrier(unittest.TestCase):
    """The other hypothesis: dead audio, carrier up."""

    def test_a_carrier_that_never_stops_reports_no_period(self):
        """The answer is 'no cycles', not a period of zero.

        This is the result that would say NWR's dead time is audio only and
        its interference duty really is 100%.
        """
        f = 162_475_000.0
        rec = _run_continuous(
            [SyntheticEmitter(f, snr_db=28.0, duty=1.0,
                              bandwidth_hz=14_000.0)], n_sweeps=800)[f]
        self.assertGreater(rec["occupancy"], 0.99)
        self.assertEqual(rec["keying"]["cycles_observed"], 0)
        self.assertIsNone(rec["keying"]["period_sweeps"])
        self.assertIn("never stopped", rec["keying"]["note"])
        self.assertEqual(rec["gaps"]["n"], 0)

    def test_an_empty_channel_also_reports_no_period(self):
        rec = _run_continuous([], n_sweeps=600)[162_475_000.0]
        self.assertEqual(rec["occupancy"], 0.0)
        self.assertEqual(rec["keying"]["cycles_observed"], 0)


class TestObservationBreaks(unittest.TestCase):
    """Why the keying measurement needs a continuous dwell."""

    def test_a_run_cut_by_a_retune_is_dropped_not_recorded_short(self):
        """Pooling interrupted runs with complete ones biases both short.

        A run that ended because the receiver tuned elsewhere is a lower
        bound on the real one. Counting it would make every period estimate
        too short, and the normal band-stepping schedule interrupts
        constantly -- which is exactly why docs/nwr-measurement.md calls for
        a dedicated single-band dwell.
        """
        policy = ThresholdPolicy()
        acc = BandAccumulator(PLAN, PLAN.channels()[:1], HistogramSpec(),
                              policy)
        from occupancy_survey.detector import SweepDecision

        def sweep(present, decided=True):
            return SweepDecision(
                baseline_db=0.0,
                levels_db=np.array([20.0 if present else 0.0]),
                excess_db=np.array([20.0 if present else 0.0]),
                present=np.array([present]),
                decided=np.array([decided]))

        for _ in range(5):          # carrier on
            acc.add_sweep(sweep(True))
        acc.add_sweep(sweep(False, decided=False))   # retune mid-transmission
        for _ in range(5):          # carrier still on afterwards
            acc.add_sweep(sweep(True))
        acc.add_sweep(sweep(False))                  # a real gap starts

        rec = acc.channel_records()[0]
        self.assertEqual(rec["runs"]["n"], 1,
                         "only the run bounded by observation on both sides "
                         "counts; the two halves split by the retune are "
                         "lower bounds and are dropped")
        self.assertEqual(rec["runs"]["max_sweeps"], 5)


if __name__ == "__main__":
    unittest.main()


class TestRetuneDoesNotFabricateAPeriod(unittest.TestCase):
    """A period must not be reported when the schedule cannot measure one.

    Caught by dry-running the measurement plan. Two bugs stacked:

    1. The accumulator only marked an observation break when a CHANNEL fell
       outside the passband, not when the receiver left the BAND. Between
       visits no sweeps arrive at all, so runs and gaps silently spanned
       retunes.
    2. Even with the break marked, the on/off pairing used `runs[-1]` --
       the last run ever recorded -- so a gap seen after a retune was
       paired with a run from before it.

    Either one produces a confident "period" that is really the dwell
    length. Occupancy was unaffected throughout, which is what made it
    dangerous: the number next to it looked equally trustworthy.
    """

    def _period(self, dwell_s, bands, cycles):
        import tempfile
        from occupancy_survey.backends.synthetic import SyntheticBackend
        from occupancy_survey.channels import resolve_bands
        from occupancy_survey.dataset import read_dataset
        from occupancy_survey.schedule import SamplingDesign
        from occupancy_survey.site import SiteMetadata
        from occupancy_survey.survey import Survey

        em = [SyntheticEmitter(151_475_000.0, 26.0, duty=0.2,
                               period_sweeps=50)]
        design = SamplingDesign(integration_s=0.01, dwell_s=dwell_s,
                                interval_s=100000.0, revisit_target_s=100000.0,
                                jitter_fraction=0.0, usable_bandwidth_hz=6e6)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(bands),
                           SyntheticBackend(em, integration_s=0.01,
                                            sample_rate_hz=6e6),
                           SiteMetadata("q", "RSPdx", "d"), design,
                           out_dir=d, seed=7).run(cycles=cycles)
            data = read_dataset(paths[0])
        chans = {c["freq_hz"]: c
                 for c in data["bands"]["vhf_lmr_low"]["channels"]}
        return chans[151_475_000.0]

    def test_band_stepping_reports_no_period_rather_than_the_dwell(self):
        rec = self._period(0.4, ["vhf_lmr_low", "nwr"], cycles=12)
        self.assertAlmostEqual(rec["occupancy"], 0.20, delta=0.03,
                               msg="occupancy must still be correct -- it is "
                                   "a ratio over decided samples")
        self.assertEqual(rec["keying"]["cycles_observed"], 0)
        self.assertIsNone(rec["keying"]["period_sweeps"],
                          "a 40-sweep dwell cannot bracket a 50-sweep "
                          "period; reporting one would be the dwell length "
                          "wearing the signal's clothes")

    def test_a_continuous_dwell_recovers_the_real_period(self):
        rec = self._period(20.0, ["vhf_lmr_low"], cycles=1)
        self.assertAlmostEqual(rec["occupancy"], 0.20, delta=0.03)
        self.assertGreater(rec["keying"]["cycles_observed"], 20)
        self.assertAlmostEqual(rec["keying"]["period_sweeps"], 50.0,
                               delta=2.0)
