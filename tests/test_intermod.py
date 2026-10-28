"""The paired-gain intermod discriminator."""

import unittest

import numpy as np

from occupancy_survey.intermod import (SUSPECT_ORDER, classify, implied_order,
                                       summarise)


class TestImpliedOrder(unittest.TestCase):

    def test_a_real_signal_implies_order_one(self):
        """Signal and noise both drop by the gain step, so excess holds."""
        self.assertAlmostEqual(implied_order(30.0, 30.0, 6.0), 1.0)

    def test_a_third_order_product_implies_order_three(self):
        """Product drops 3G, noise drops G, so excess drops 2G."""
        self.assertAlmostEqual(implied_order(30.0, 30.0 - 12.0, 6.0), 3.0)

    def test_a_second_order_product_implies_order_two(self):
        self.assertAlmostEqual(implied_order(30.0, 30.0 - 6.0, 6.0), 2.0)

    def test_a_nonpositive_gain_step_is_refused(self):
        with self.assertRaises(ValueError):
            implied_order(30.0, 20.0, 0.0)


class TestClassify(unittest.TestCase):

    def _paired(self, gain_step=6.0):
        # One real carrier, one IM3 product, one weak channel below the
        # presence threshold, one never detected.
        freqs = [151.0e6, 151.1e6, 151.2e6, 151.3e6]
        before = [30.0, 30.0, 2.0, np.nan]
        after = [30.0, 30.0 - 2 * gain_step, 2.0, np.nan]
        return freqs, before, after, gain_step

    def test_the_product_is_flagged_and_the_signal_is_not(self):
        freqs, before, after, step = self._paired()
        res = {r.freq_hz: r for r in classify(freqs, before, after, step)}
        self.assertFalse(res[151.0e6].suspect)
        self.assertEqual(res[151.0e6].verdict,
                         "consistent with a real signal")
        self.assertTrue(res[151.1e6].suspect)
        self.assertEqual(res[151.1e6].verdict, "likely 3rd-order product")

    def test_a_channel_too_weak_to_assess_is_not_flagged(self):
        freqs, before, after, step = self._paired()
        res = {r.freq_hz: r for r in classify(freqs, before, after, step)}
        self.assertFalse(res[151.2e6].present_in_both)
        self.assertFalse(res[151.2e6].suspect)
        self.assertEqual(res[151.2e6].verdict, "not assessable")

    def test_an_unobserved_channel_is_not_flagged(self):
        freqs, before, after, step = self._paired()
        res = {r.freq_hz: r for r in classify(freqs, before, after, step)}
        self.assertFalse(res[151.3e6].suspect)

    def test_mismatched_lengths_are_refused(self):
        with self.assertRaises(ValueError):
            classify([1.0, 2.0], [1.0], [1.0, 2.0], 6.0)

    def test_the_threshold_sits_clear_of_both_hypotheses(self):
        """The test must discriminate, not merely order.

        A real signal implies 1.0 and an IM3 product implies 3.0; the
        suspect threshold has to leave room for scatter without touching
        either.
        """
        self.assertGreater(SUSPECT_ORDER, 1.3)
        self.assertLess(SUSPECT_ORDER, 2.4)


class TestSummarise(unittest.TestCase):

    def test_a_clean_front_end_gets_the_all_clear(self):
        res = classify([151e6, 152e6], [30.0, 25.0], [30.0, 25.0], 6.0)
        s = summarise(res)
        self.assertEqual(s["channels_suspect"], 0)
        self.assertIn("linear front end", s["advice"])

    def test_a_badly_overloaded_front_end_names_the_fix_in_order(self):
        freqs = [151e6 + i * 1e5 for i in range(10)]
        before = [30.0] * 10
        after = [30.0 - 12.0] * 8 + [30.0, 30.0]
        s = summarise(classify(freqs, before, after, 6.0))
        self.assertGreaterEqual(s["suspect_fraction"], 0.2)
        self.assertIn("bandpass", s["advice"])
        self.assertIn("reduce gain", s["advice"])
        self.assertLess(s["advice"].index("bandpass"),
                        s["advice"].index("reduce gain"),
                        "the filter fixes must come before the gain fix, "
                        "because they cost no sensitivity and it does")

    def test_nothing_assessable_says_so_rather_than_passing(self):
        s = summarise(classify([151e6], [2.0], [2.0], 6.0))
        self.assertIsNone(s["suspect_fraction"])
        self.assertIn("says nothing", s["advice"])


if __name__ == "__main__":
    unittest.main()
