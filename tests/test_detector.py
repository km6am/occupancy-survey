"""The measurement core: does a known duty cycle come back out?"""

import unittest

import numpy as np

from occupancy_survey.channels import BandPlan, TARGET
from occupancy_survey.detector import (BandBaseline, ChannelLevelExtractor,
                                       ThresholdPolicy,
                                       baseline_elevation_db, decide,
                                       max_bin_noise_bias_db,
                                       required_snr_db)
from occupancy_survey.occupancy import BandAccumulator, HistogramSpec
from tests.synthetic_band import SyntheticBand, SyntheticEmitter


def _plan(step_hz=12_500.0, bw_hz=11_250.0):
    return BandPlan(name="test", freq_min_hz=150_800_000.0,
                    freq_max_hz=151_800_000.0, channel_step_hz=step_hz,
                    channel_bandwidth_hz=bw_hz, service="IG", role=TARGET)


def _run(emitters, n_sweeps=600, policy=None, seed=3, snr_noise_floor=-100.0):
    """Run a synthetic band through the full detector + accumulator."""
    plan = _plan()
    channels = plan.channels()
    policy = policy or ThresholdPolicy()
    band = SyntheticBand(
        freq_min_hz=plan.freq_min_hz - 100_000.0,
        freq_max_hz=plan.freq_max_hz + 100_000.0,
        bin_hz=1_000.0, emitters=emitters,
        noise_floor_db=snr_noise_floor, seed=seed,
    )
    extractor = ChannelLevelExtractor(channels, band.freq_axis,
                                      usable_fraction=1.0)
    baseline = BandBaseline(policy)
    acc = BandAccumulator(plan, channels, HistogramSpec(), policy)

    for i in range(n_sweeps):
        levels = extractor.levels_db(band.sweep(i))
        b = baseline.update(levels)
        acc.add_sweep(decide(levels, b, policy))

    by_freq = {r["freq_hz"]: r for r in acc.channel_records()}
    return acc, by_freq, extractor


class TestChannelLevelExtractor(unittest.TestCase):

    def test_channels_outside_usable_span_are_not_observed(self):
        plan = _plan()
        channels = plan.channels()
        band = SyntheticBand(plan.freq_min_hz, plan.freq_max_hz, 1_000.0)
        # usable_fraction 0.5 keeps only the middle half of the span.
        ex = ChannelLevelExtractor(channels, band.freq_axis,
                                   usable_fraction=0.5)
        levels = ex.levels_db(band.sweep(0))
        self.assertTrue(np.isnan(levels[0]),
                        "a channel at the band edge must read NaN, not a level")
        self.assertTrue(np.isnan(levels[-1]))
        self.assertTrue(np.isfinite(levels[len(levels) // 2]))
        self.assertLess(ex.n_observed, len(channels))

    def test_bins_per_channel_reflects_channel_width(self):
        plan = _plan()
        band = SyntheticBand(plan.freq_min_hz - 50_000.0,
                             plan.freq_max_hz + 50_000.0, 1_000.0)
        ex = ChannelLevelExtractor(plan.channels(), band.freq_axis,
                                   usable_fraction=1.0)
        # 11.25 kHz of channel at 1 kHz bins
        self.assertGreaterEqual(ex.bins_per_channel, 10)
        self.assertLessEqual(ex.bins_per_channel, 13)

    def test_spectrum_width_mismatch_is_an_error(self):
        plan = _plan()
        band = SyntheticBand(plan.freq_min_hz, plan.freq_max_hz, 1_000.0)
        ex = ChannelLevelExtractor(plan.channels(), band.freq_axis)
        with self.assertRaises(ValueError):
            ex.levels_db(np.zeros(7))


class TestBaseline(unittest.TestCase):

    def test_no_decision_before_the_window_fills(self):
        policy = ThresholdPolicy(min_baseline_samples=10_000)
        plan = _plan()
        band = SyntheticBand(plan.freq_min_hz, plan.freq_max_hz, 1_000.0)
        ex = ChannelLevelExtractor(plan.channels(), band.freq_axis)
        baseline = BandBaseline(policy)
        self.assertIsNone(baseline.update(ex.levels_db(band.sweep(0))))

    def test_permanently_keyed_channel_reads_fully_occupied(self):
        """The failure mode a purely temporal baseline would have.

        A channel occupied 100% of the time must read ~100%, not 0%. This is
        the NOAA Weather Radio control case, and it is the single reason the
        baseline is pooled across channels rather than over a channel's own
        history.
        """
        f = 151_000_000.0
        _, by_freq, _ = _run([SyntheticEmitter(f, snr_db=25.0, duty=1.0)])
        self.assertIn(f, by_freq)
        self.assertGreater(by_freq[f]["occupancy"], 0.99)

    def test_baseline_resets_on_configuration_change(self):
        policy = ThresholdPolicy(min_baseline_samples=50)
        plan = _plan()
        band = SyntheticBand(plan.freq_min_hz, plan.freq_max_hz, 1_000.0)
        ex = ChannelLevelExtractor(plan.channels(), band.freq_axis)
        baseline = BandBaseline(policy)
        for i in range(5):
            baseline.update(ex.levels_db(band.sweep(i)))
        self.assertGreater(baseline.pooled_count, 0)
        baseline.reset()
        self.assertEqual(baseline.pooled_count, 0)


class TestDutyCycleRecovery(unittest.TestCase):
    """The headline claim: a known duty cycle comes back within tolerance."""

    def test_recovers_tia603_style_duty_cycles(self):
        # The figures duty_cycles.md carries as *standard*: 22.5% for
        # conventional base/mobile, 10% for portables. Plus 100% and 0% as
        # the ends of the range.
        cases = [
            (151_000_000.0, 1.00, 0),
            (151_100_000.0, 0.50, 3),
            (151_200_000.0, 0.225, 7),
            (151_300_000.0, 0.10, 11),
        ]
        emitters = [
            SyntheticEmitter(f, snr_db=25.0, duty=d, period_sweeps=40,
                             phase_sweeps=p)
            for f, d, p in cases
        ]
        _, by_freq, _ = _run(emitters, n_sweeps=800)

        for f, duty, _ in cases:
            self.assertIn(f, by_freq, f"{f/1e6:.3f} MHz produced no record")
            got = by_freq[f]["occupancy"]
            self.assertAlmostEqual(
                got, duty, delta=0.03,
                msg=f"{f/1e6:.3f} MHz: duty {duty:.3f}, measured {got:.3f}")

    def test_quiet_channel_reads_zero_not_none(self):
        """Observed-and-quiet must be distinguishable from not-observed."""
        _, by_freq, _ = _run([SyntheticEmitter(151_000_000.0, 25.0, 1.0)])
        quiet = by_freq[151_500_000.0]
        self.assertEqual(quiet["occupancy"], 0.0)
        self.assertGreater(quiet["n_decided"], 0)

    def test_active_channel_count_tracks_the_emitters(self):
        emitters = [SyntheticEmitter(151_000_000.0 + i * 100_000.0,
                                     snr_db=25.0, duty=1.0)
                    for i in range(5)]
        acc, _, _ = _run(emitters, n_sweeps=300)
        summary = acc.stats.summary()
        self.assertEqual(summary["active_channels"]["max"], 5)
        self.assertAlmostEqual(summary["active_channels"]["median"], 5.0,
                               delta=0.5)


class TestThresholdSensitivity(unittest.TestCase):
    """The threshold IS the result -- so show it moving the result, and pin
    the relation between the threshold and an SNR.

    These tests exist because `threshold_db` is dB above the baseline, and
    the baseline is a percentile of the max-bin statistic that already sits
    above the mean noise power. Confusing the two silently loses every
    signal within a few dB of threshold.
    """

    def test_a_carrier_crosses_in_and_out_with_the_threshold(self):
        f = 151_000_000.0
        em = [SyntheticEmitter(f, snr_db=20.0, duty=1.0)]
        _, by_freq, _ = _run(em, n_sweeps=400)
        self.assertGreater(by_freq[f]["occupancy"], 0.99)

        acc, _, _ = _run(em, n_sweeps=400,
                         policy=ThresholdPolicy(threshold_db=30.0))
        strict = {r["freq_hz"]: r for r in acc.channel_records()}[f]
        self.assertLess(strict["occupancy"], 0.05,
                        "the same carrier must vanish at a 30 dB threshold -- "
                        "if it does not, the threshold is not doing anything")

    def test_required_snr_locates_the_detection_edge(self):
        """Discriminating test of the analytic sensitivity relation.

        The transition from missed to caught is sharp -- about 2 dB wide --
        so the test locates the measured 50% crossing by bisection and
        compares it with the prediction. A relation that merely got the
        ordering right would pass a "stronger is better" test; this one
        fails if the predicted edge is off by more than 1 dB, which the
        naive threshold + baseline_elevation form is.
        """
        f = 151_000_000.0
        policy = ThresholdPolicy(threshold_db=10.0)
        _, _, extractor = _run([], n_sweeps=70, policy=policy)
        n_bins = extractor.bins_per_channel
        predicted = required_snr_db(policy.threshold_db, n_bins,
                                    policy.baseline_percentile)

        def occ(snr_db):
            _, by_freq, _ = _run(
                [SyntheticEmitter(f, snr_db=snr_db, duty=1.0)],
                n_sweeps=400, policy=policy, seed=17)
            return by_freq[f]["occupancy"]

        self.assertLess(occ(predicted - 5.0), 0.05,
                        "5 dB below the predicted edge should be missed")
        self.assertGreater(occ(predicted + 5.0), 0.95,
                           "5 dB above the predicted edge should be caught")

        lo, hi = predicted - 6.0, predicted + 6.0
        for _ in range(9):
            mid = 0.5 * (lo + hi)
            if occ(mid) < 0.5:
                lo = mid
            else:
                hi = mid
        measured_edge = 0.5 * (lo + hi)

        self.assertAlmostEqual(
            measured_edge, predicted, delta=1.0,
            msg=f"predicted detection edge {predicted:.2f} dB SNR, measured "
                f"50% crossing at {measured_edge:.2f} dB. If this fails the "
                f"derivation in required_snr_db() is wrong, not imprecise.")

    def test_baseline_elevation_matches_simulation(self):
        """Verify the analytic baseline elevation against the simulation.

        Noise-only band: the measured baseline minus the known mean noise
        power must equal baseline_elevation_db() for the channel width in
        use. This is the assumption-check for the independence of adjacent
        FFT bins, which is the weak link in the derivation.
        """
        policy = ThresholdPolicy(min_baseline_samples=2000)
        plan = _plan()
        channels = plan.channels()
        noise_floor_db = -100.0
        band = SyntheticBand(plan.freq_min_hz - 100_000.0,
                             plan.freq_max_hz + 100_000.0, bin_hz=1_000.0,
                             emitters=(), noise_floor_db=noise_floor_db,
                             seed=5)
        ex = ChannelLevelExtractor(channels, band.freq_axis,
                                   usable_fraction=1.0)
        baseline = BandBaseline(policy)
        measured = None
        for i in range(40):
            measured = baseline.update(ex.levels_db(band.sweep(i)))
        self.assertIsNotNone(measured)

        # The synthetic mean noise power per bin is 1.0 linear, i.e. 0 dB
        # relative, so the whole elevation is measured - noise_floor_db.
        observed_elevation = measured - noise_floor_db
        predicted = baseline_elevation_db(ex.bins_per_channel,
                                          policy.baseline_percentile)
        self.assertAlmostEqual(
            observed_elevation, predicted, delta=0.4,
            msg=f"analytic baseline elevation {predicted:.2f} dB vs "
                f"simulated {observed_elevation:.2f} dB for "
                f"{ex.bins_per_channel} bins/channel")

    def test_occupancy_is_rethresholdable_from_the_histogram(self):
        """The histogram must reproduce a re-run at a different threshold.

        This is the property that makes the dataset outlive its threshold
        choice, so it is tested against ground truth rather than asserted.
        """
        f = 151_000_000.0
        em = [SyntheticEmitter(f, snr_db=18.0, duty=1.0)]
        acc_default, by_default, _ = _run(em, n_sweeps=400, seed=11)
        from_hist = acc_default._chan[
            [c.freq_hz for c in acc_default.channels].index(f)
        ].occupancy_at(30.0)

        acc_strict, by_strict, _ = _run(
            em, n_sweeps=400, seed=11,
            policy=ThresholdPolicy(threshold_db=30.0))

        self.assertAlmostEqual(from_hist, by_strict[f]["occupancy"], delta=1e-9,
                               msg="re-thresholding the stored histogram must "
                                   "match a re-run at that threshold")


class TestNoiseBiasReporting(unittest.TestCase):

    def test_bias_grows_with_bins_and_is_zero_for_one_bin(self):
        self.assertEqual(max_bin_noise_bias_db(1), 0.0)
        self.assertLess(max_bin_noise_bias_db(4), max_bin_noise_bias_db(64))
        # Sanity: a 12-bin channel's max sits a few dB above the mean.
        self.assertGreater(max_bin_noise_bias_db(12), 3.0)
        self.assertLess(max_bin_noise_bias_db(12), 6.0)


if __name__ == "__main__":
    unittest.main()
