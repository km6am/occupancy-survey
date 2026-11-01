"""An implied order outside the range a product can have is not a product.

The measured case, 2026-11-01 (vhf_lmr_low @ 152.100 MHz, 6 dB step): one
channel came back at order 4.61 labelled "likely 3rd-order product". A 3rd
order product predicts 3.0; 4.61 means the excess fell by 21.7 dB for a 6 dB
gain change, which no simple product does. The parsimonious explanation is
that the transmission stopped between the two measurements.

These tests pin both directions: such a channel must not be reported as a
product, and the band must not come out looking clean because of it.
"""

import unittest

from occupancy_survey.intermod import (IMPLAUSIBLE_ORDER_MAX,
                                       IMPLAUSIBLE_ORDER_MIN, classify,
                                       summarise)


def one(order: float, gain_step_db: float = 6.0, excess_before: float = 30.0):
    """A single channel whose implied order is exactly `order`."""
    after = excess_before - (order - 1.0) * gain_step_db
    return classify([150e6], [excess_before], [after], gain_step_db)[0]


class TestPlausibilityBounds(unittest.TestCase):

    def test_real_signal_still_reads_as_one(self):
        r = one(1.0)
        self.assertAlmostEqual(r.implied_order, 1.0, places=6)
        self.assertFalse(r.suspect)
        self.assertFalse(r.unexplained)
        self.assertEqual(r.verdict, "consistent with a real signal")

    def test_im2_and_im3_unchanged(self):
        self.assertEqual(one(2.0).verdict, "likely 2nd-order product")
        self.assertTrue(one(2.0).suspect)
        self.assertEqual(one(3.0).verdict, "likely 3rd-order product")
        self.assertTrue(one(3.0).suspect)
        self.assertFalse(one(3.0).unexplained)

    def test_the_measured_461_is_not_called_a_product(self):
        r = one(4.61)
        self.assertTrue(r.unexplained)
        self.assertFalse(r.suspect, "4.61 must not count as a product")
        self.assertNotIn("3rd-order", r.verdict)
        self.assertIn("stopped", r.verdict)

    def test_order_below_the_floor_reads_as_a_signal_appearing(self):
        r = one(-1.0)
        self.assertTrue(r.unexplained)
        self.assertFalse(r.suspect)
        self.assertIn("started", r.verdict)

    def test_bounds_bracket_the_real_product_orders(self):
        self.assertLess(IMPLAUSIBLE_ORDER_MIN, 1.0)
        self.assertGreater(IMPLAUSIBLE_ORDER_MAX, 3.0)

    def test_not_assessable_is_never_unexplained(self):
        # Below the presence threshold: the test says nothing about it.
        r = classify([150e6], [2.0], [-40.0], 6.0,
                     present_threshold_db=10.0)[0]
        self.assertFalse(r.present_in_both)
        self.assertFalse(r.unexplained)
        self.assertFalse(r.suspect)
        self.assertEqual(r.verdict, "not assessable")


class TestSummaryKeepsTheBucketsApart(unittest.TestCase):

    def _summary(self, orders, gain_step_db=6.0):
        before = [30.0] * len(orders)
        after = [30.0 - (o - 1.0) * gain_step_db for o in orders]
        freqs = [150e6 + 1e6 * i for i in range(len(orders))]
        return summarise(classify(freqs, before, after, gain_step_db))

    def test_unexplained_counted_separately(self):
        out = self._summary([1.0, 3.0, 4.61])
        self.assertEqual(out["channels_assessable"], 3)
        self.assertEqual(out["channels_suspect"], 1)
        self.assertEqual(out["channels_unexplained"], 1)

    def test_all_unexplained_does_not_read_as_clean(self):
        out = self._summary([4.61, 5.0])
        self.assertEqual(out["channels_suspect"], 0)
        self.assertEqual(out["channels_unexplained"], 2)
        self.assertNotIn("No evidence of intermod", out["advice"])
        self.assertIn("NOT shown clean", out["advice"])
        self.assertIn("repeat the run", out["advice"])

    def test_a_genuinely_linear_band_still_reads_clean(self):
        out = self._summary([1.0, 1.0, 1.1])
        self.assertEqual(out["channels_suspect"], 0)
        self.assertEqual(out["channels_unexplained"], 0)
        self.assertIn("linear front end", out["advice"])

    def test_unexplained_channels_are_still_listed(self):
        out = self._summary([1.0, 4.61])
        listed = [c["freq_hz"] for c in out["suspect_channels"]]
        self.assertEqual(len(listed), 1)
        self.assertIn("stopped", out["suspect_channels"][0]["verdict"])

    def test_mixed_case_mentions_both(self):
        out = self._summary([3.0, 3.0, 4.61, 1.0])
        self.assertEqual(out["channels_suspect"], 2)
        self.assertEqual(out["channels_unexplained"], 1)
        self.assertIn("nonlinearity", out["advice"])
        self.assertIn("mid-measurement", out["advice"])

    def test_suspect_fraction_excludes_the_unexplained(self):
        # 4 assessable, 2 products, 2 unexplained -> 0.5, not 1.0. The
        # diagnostic must not claim more evidence than it has.
        out = self._summary([3.0, 2.0, 4.61, 5.5])
        self.assertAlmostEqual(out["suspect_fraction"], 0.5)
        self.assertAlmostEqual(out["unexplained_fraction"], 0.5)


if __name__ == "__main__":
    unittest.main()
