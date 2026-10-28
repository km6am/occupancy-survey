"""Presence as a validation reference, and the precondition on it.

NWR's duty-cycle claim was retracted 2026-10-28. This is the surviving,
weaker, more robust use: over a long enough observation NWR must APPEAR,
because the NWS publishes the transmitter list, so absence contradicts
published data rather than an inference. It needs no assumption about
carrier keying, which is exactly why it holds where the other did not.
"""

import tempfile
import unittest

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import BAND_PLANS, resolve_bands
from occupancy_survey.dataset import read_dataset
from occupancy_survey.occupancy import HistogramSpec
from occupancy_survey.presence import (PresenceReference, min_detectable_duty,
                                       presence_check, strongest_channel)
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter

SPEC = HistogramSpec().as_metadata()
REF = PresenceReference(enabled=True, basis="test")


def _rec(freq_hz, n_present, n_decided, excess_bin=30):
    """A channel record with all present samples in one excess bin."""
    hist = [0] * SPEC["n_bins"]
    hist[0] = n_decided - n_present          # quiet samples near the floor
    hist[excess_bin] = n_present
    return {"freq_hz": freq_hz, "n_present": n_present,
            "n_decided": n_decided, "hist": hist,
            "underflow": 0, "overflow": 0}


class TestMinDetectableDuty(unittest.TestCase):

    def test_more_visits_detect_smaller_duty_cycles(self):
        a = min_detectable_duty(12, 20.0, 360.0)
        b = min_detectable_duty(50, 20.0, 360.0)
        self.assertGreater(a, b)
        self.assertAlmostEqual(a, 0.165, delta=0.01)

    def test_enough_visits_make_any_duty_detectable(self):
        self.assertEqual(min_detectable_duty(200, 20.0, 360.0), 0.0)

    def test_too_few_visits_prove_nothing(self):
        """The honest answer when the observation bought nothing."""
        # One 1 s look at a 600 s cycle: only a near-continuous carrier
        # could have been caught.
        self.assertGreater(min_detectable_duty(1, 1.0, 600.0), 0.9)
        self.assertEqual(min_detectable_duty(0, 20.0, 360.0), 1.0)

    def test_a_longer_dwell_detects_smaller_duty_cycles(self):
        self.assertLess(min_detectable_duty(12, 60.0, 360.0),
                        min_detectable_duty(12, 5.0, 360.0))

    def test_the_peers_rule_of_thumb_lands_where_the_model_does(self):
        """15-30 min of coverage was suggested as the precondition.

        At a 20 s dwell that is 45-90 visits, and the model says those
        detect duty cycles down to ~1% and below -- so the heuristic and
        the model agree, which is worth pinning rather than assuming.
        """
        self.assertLess(min_detectable_duty(45, 20.0, 360.0), 0.02)
        self.assertEqual(min_detectable_duty(90, 20.0, 360.0), 0.0)


class TestVerdicts(unittest.TestCase):
    """Three distinguishable outcomes, not one."""

    def _check(self, records, n_visits=50, predicted=None):
        return presence_check(records, SPEC, integration_s=0.1,
                              n_visits=n_visits, dwell_s=20.0,
                              reference=REF,
                              predicted_strongest_hz=predicted)

    def test_nothing_seen_after_enough_looking_indicts_the_receiver(self):
        out = self._check([_rec(162_400_000.0, 0, 5000)], n_visits=50)
        self.assertEqual(out["verdict"], "absent")
        self.assertIn("indicts the MEASURING END", out["meaning"])
        self.assertLess(out["min_detectable_duty"], 0.05)

    def test_nothing_seen_after_too_little_looking_is_inconclusive(self):
        """The failure the precondition exists to prevent.

        Without the gate this would read as a broken receive path and send
        an operator to check hardware that is fine.
        """
        out = presence_check([_rec(162_400_000.0, 0, 10)], SPEC,
                             integration_s=0.1, n_visits=1, dwell_s=1.0,
                             reference=REF)
        self.assertEqual(out["verdict"], "inconclusive")
        self.assertIn("says NOTHING about the receive path", out["meaning"])

    def test_a_detection_with_the_predicted_channel_strongest(self):
        out = self._check(
            [_rec(162_400_000.0, 900, 1000, excess_bin=40),
             _rec(162_475_000.0, 200, 1000, excess_bin=20)],
            predicted=162_400_000.0)
        self.assertEqual(out["verdict"], "present_as_predicted")
        self.assertEqual(out["strongest_observed_hz"], 162_400_000.0)

    def test_a_detection_with_the_wrong_channel_strongest(self):
        """Separates a prediction error from a receive-path error."""
        out = self._check(
            [_rec(162_400_000.0, 200, 1000, excess_bin=20),
             _rec(162_550_000.0, 900, 1000, excess_bin=40)],
            predicted=162_400_000.0)
        self.assertEqual(out["verdict"], "present_wrong_channel")
        self.assertIn("prediction or the antenna pattern", out["meaning"])
        self.assertIn("receive path, which is working", out["meaning"])

    def test_a_detection_without_a_prediction_says_only_that(self):
        out = self._check([_rec(162_400_000.0, 900, 1000)])
        self.assertEqual(out["verdict"], "present")
        self.assertIn("Supply a prediction", out["meaning"])

    def test_a_disabled_reference_produces_no_check(self):
        self.assertIsNone(
            presence_check([], SPEC, 0.1, 10, 20.0,
                           PresenceReference(enabled=False)))


class TestStrongestChannel(unittest.TestCase):

    def test_ranking_is_by_level_not_by_how_busy(self):
        """A strong occasional carrier outranks a weak constant one.

        The question is which transmitter is loudest, not which is busiest,
        because that is what the predicted-strongest check compares.
        """
        busy_weak = _rec(162_400_000.0, 990, 1000, excess_bin=12)
        rare_strong = _rec(162_550_000.0, 120, 1000, excess_bin=45)
        best, level = strongest_channel([busy_weak, rare_strong], SPEC)
        self.assertEqual(best["freq_hz"], 162_550_000.0)
        self.assertGreater(level, 30.0)

    def test_a_channel_never_present_is_not_ranked(self):
        best, _ = strongest_channel([_rec(162_400_000.0, 0, 1000)], SPEC)
        self.assertIsNone(best)


class TestThroughTheRunner(unittest.TestCase):

    def _run(self, emitters, cycles, out_dir):
        design = SamplingDesign(integration_s=0.01, dwell_s=0.5,
                                interval_s=600.0, usable_bandwidth_hz=2.048e6)
        backend = SyntheticBackend(emitters, integration_s=0.01,
                                   sample_rate_hz=2.048e6)
        paths = Survey(resolve_bands(["nwr"]), backend,
                       SiteMetadata("s", "RSPdx", "discone"), design,
                       out_dir=out_dir, seed=3).run(cycles=cycles)
        return read_dataset(paths[0])

    def test_nwr_carries_a_presence_check_but_no_control_check(self):
        em = [SyntheticEmitter(162_475_000.0, 30.0, duty=1.0,
                               bandwidth_hz=14_000.0)]
        with tempfile.TemporaryDirectory() as d:
            band = self._run(em, 30, d)["bands"]["nwr"]
        self.assertIsNone(band["control_check"],
                          "a control check would gate the run on the "
                          "retracted duty-cycle assumption")
        check = band["presence_check"]
        self.assertIsNotNone(check)
        self.assertEqual(check["verdict"], "present")
        self.assertEqual(check["strongest_observed_hz"], 162_475_000.0)
        self.assertGreater(check["observed_channel_seconds"], 0.0)

    def test_an_empty_band_with_few_visits_is_inconclusive_not_absent(self):
        with tempfile.TemporaryDirectory() as d:
            band = self._run([], 2, d)["bands"]["nwr"]
        self.assertEqual(band["presence_check"]["verdict"], "inconclusive")

    def test_a_band_without_a_presence_reference_gets_no_check(self):
        design = SamplingDesign(integration_s=0.01, dwell_s=0.5,
                                interval_s=600.0, usable_bandwidth_hz=2.048e6)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["vhf_lmr_low"]),
                           SyntheticBackend(integration_s=0.01,
                                            sample_rate_hz=2.048e6),
                           SiteMetadata("s", "RSPdx", "d"), design,
                           out_dir=d).run(cycles=2)
            band = read_dataset(paths[0])["bands"]["vhf_lmr_low"]
        self.assertIsNone(band["presence_check"])

    def test_the_nwr_band_declares_its_basis(self):
        presence = BAND_PLANS["nwr"].presence
        self.assertTrue(presence.enabled)
        self.assertIn("publishes the transmitter list", presence.basis)
        self.assertIn("retracted", presence.basis,
                      "the basis must record that the duty-cycle claim is "
                      "withdrawn, or someone will reuse it")


if __name__ == "__main__":
    unittest.main()
