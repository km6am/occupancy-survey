"""The paired-gain diagnostic must divide by the step the device TOOK.

intermod.classify() computes implied order as 1 + excess_drop / G. A 6 dB
request that the attenuator rounds to 4 inflates every order by half, which
pushes real signals toward the suspect bound and makes the receiver look
like it is generating signals it is not. The RSPdx was measured quantising a
20 dB RF request to 24, so this is not hypothetical.
"""

import unittest

import numpy as np

from occupancy_survey.backends.base import Backend
from occupancy_survey.intermod import classify, summarise, SUSPECT_ORDER


class TestDividingByTheWrongStep(unittest.TestCase):
    """Why the verification matters, in the numbers it changes."""

    def _order(self, excess_before, excess_after, step_db):
        freqs = [150_000_000.0]
        r = classify(freqs, np.array([excess_before]),
                     np.array([excess_after]), step_db,
                     present_threshold_db=6.0)
        return r[0].implied_order

    def test_a_real_signal_reads_order_one_with_the_right_step(self):
        # A real signal's excess over the floor does not move with gain.
        self.assertAlmostEqual(self._order(20.0, 20.0, 4.0), 1.0, places=2)

    def test_an_im3_product_reads_near_three(self):
        # Third order: excess drops by about 2G, here 2 x 4 dB.
        self.assertAlmostEqual(self._order(20.0, 12.0, 4.0), 3.0, places=2)

    def test_assuming_six_when_the_device_moved_four_understates_order(self):
        """The real risk: a genuine IM3 product reading as benign."""
        true_step = 4.0
        im3_after = 20.0 - 2 * true_step
        honest = self._order(20.0, im3_after, true_step)
        wrong = self._order(20.0, im3_after, 6.0)
        self.assertAlmostEqual(honest, 3.0, places=2)
        self.assertLess(wrong, honest,
                        "assuming a larger step than the device took pulls "
                        "the implied order DOWN, toward 'real signal'")

    def test_assuming_four_when_the_device_moved_six_invents_suspicion(self):
        """And the other direction manufactures false positives."""
        true_step = 6.0
        real_after = 20.0 - 0.9 * true_step    # a signal that faded slightly
        wrong = self._order(20.0, real_after, 4.0)
        self.assertGreater(wrong, SUSPECT_ORDER,
                           "an understated step can push a real signal past "
                           "the suspect bound")


class TestBackendContract(unittest.TestCase):

    def test_the_default_reports_unmeasurable_rather_than_zero(self):
        """Zero would mean 'the gain did not move', which is a real result."""
        self.assertIsNone(Backend.achieved_gain_offset_db(object()))

    def test_a_backend_that_cannot_step_gain_still_returns_false(self):
        self.assertFalse(Backend.set_gain_offset_db(object(), -6.0))


class TestSurveySkipsAnUnmovedStep(unittest.TestCase):
    """A step the device refused must abort the diagnostic, not divide by it."""

    def test_a_sub_half_db_step_returns_none(self):
        from occupancy_survey.survey import Survey
        calls = []

        class B:
            def set_filters(self, fe):
                return {}

            def tune(self, c, s):
                pass

            def freq_axis(self):
                return np.linspace(149e6, 151e6, 512)

            def sweeps(self, dwell):
                for _ in range(4):
                    yield type("S", (), {"spectrum_db": np.full(512, -90.0),
                                         "t_unix": 0.0,
                                         "clip_fraction": 0.0})()

            def set_gain_offset_db(self, off):
                calls.append(off)
                return True

            def achieved_gain_offset_db(self):
                return 0.0          # the attenuator was already at its end

        from occupancy_survey.channels import BAND_PLANS
        from occupancy_survey.schedule import tune_steps
        from occupancy_survey.config import (SamplingDesign, ThresholdPolicy,
                                             HistogramSpec, SiteMetadata)
        band = BAND_PLANS["vhf_lmr_low"]
        design = SamplingDesign()
        s = Survey([band], B(), SiteMetadata(site_id="t", receiver_model="x", antenna="y"), design,
                   ThresholdPolicy(), HistogramSpec(), out_dir="/tmp/_x")
        step = next(iter(tune_steps(band, design)))
        self.assertIsNone(s.intermod_check(band, step, sweeps=2),
                          "dividing by a step the device did not take would "
                          "report order 1 for everything")
        self.assertIn(0.0, calls, "gain must be restored even on the abort")


if __name__ == "__main__":
    unittest.main()
