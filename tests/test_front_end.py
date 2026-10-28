"""Per-band front-end filtering, and the interaction that bites."""

import tempfile
import unittest

from occupancy_survey.backends.base import Backend
from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import (BAND_PLANS, CONTROL, DEFAULT_SCHEDULE,
                                       FrontEnd, resolve_bands)
from occupancy_survey.dataset import read_dataset
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey


class TestFilterPlan(unittest.TestCase):

    def test_the_fm_notch_is_off_only_for_the_fm_control_band(self):
        """The interaction that would fail the control for the wrong reason.

        Every band in the schedule wants the broadcast-FM notch ON except
        the FM control band itself. Leaving it on there makes the control
        read ~0%, which declares the whole run untrustworthy for a fault
        that is in the filter plan rather than the receive path.
        """
        for name in DEFAULT_SCHEDULE:
            band = BAND_PLANS[name]
            if name == "bcast_fm":
                self.assertFalse(
                    band.front_end.rf_notch,
                    "the FM notch must be OFF for the FM control band")
                self.assertIn("MUST BE OFF", band.front_end.reason)
            else:
                self.assertTrue(
                    band.front_end.rf_notch,
                    f"{name} is measured alongside broadcast FM and needs "
                    f"the notch on")

    def test_the_nwr_control_shares_the_vhf_targets_filtering(self):
        """A control filtered differently from its targets is not a control."""
        nwr = BAND_PLANS["nwr"]
        vhf = BAND_PLANS["vhf_lmr_low"]
        self.assertEqual(nwr.role, CONTROL)
        self.assertEqual(nwr.front_end.rf_notch, vhf.front_end.rf_notch)
        self.assertEqual(nwr.front_end.dab_notch, vhf.front_end.dab_notch)

    def test_every_band_names_an_external_filter_and_a_reason(self):
        for name in DEFAULT_SCHEDULE:
            fe = BAND_PLANS[name].front_end
            self.assertTrue(fe.external, f"{name} names no external filter")
            self.assertTrue(fe.reason, f"{name} gives no reason")

    def test_external_filters_are_recorded_never_claimed_as_applied(self):
        meta = FrontEnd(rf_notch=True, external="150-174 MHz BPF").as_metadata()
        self.assertEqual(meta["external_filter_recommended"], "150-174 MHz BPF")
        self.assertIn("RECORDED ONLY", meta["note"])


class _FilterRecordingBackend(SyntheticBackend):
    """Synthetic backend that remembers the filter calls it received."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.filter_calls = []

    def set_filters(self, front_end):
        self.filter_calls.append(
            (front_end.rf_notch, front_end.dab_notch))
        return {**front_end.as_metadata(), "actuated": True,
                "applied": {"rf_notch": front_end.rf_notch,
                            "dab_notch": front_end.dab_notch}}


class TestFiltersAreApplied(unittest.TestCase):

    def _run(self, bands, out_dir):
        backend = _FilterRecordingBackend(integration_s=0.01)
        design = SamplingDesign(integration_s=0.01, dwell_s=0.5,
                                interval_s=600.0, usable_bandwidth_hz=2e6)
        paths = Survey(resolve_bands(bands), backend,
                       SiteMetadata("s", "RSPdx", "discone"), design,
                       out_dir=out_dir).run(cycles=2)
        return backend, paths

    def test_each_band_gets_its_own_filter_state(self):
        with tempfile.TemporaryDirectory() as d:
            backend, _ = self._run(["vhf_lmr_low", "bcast_fm"], d)
        self.assertIn((True, True), backend.filter_calls,
                      "the VHF band must request the notches on")
        self.assertIn((False, False), backend.filter_calls,
                      "the FM control must request them off")

    def test_the_applied_state_reaches_the_dataset(self):
        with tempfile.TemporaryDirectory() as d:
            _, paths = self._run(["vhf_lmr_low", "bcast_fm"], d)
            back = read_dataset(paths[0])
        vhf = back["bands"]["vhf_lmr_low"]["front_end"]
        fm = back["bands"]["bcast_fm"]["front_end"]
        self.assertTrue(vhf["applied"]["rf_notch"])
        self.assertFalse(fm["applied"]["rf_notch"])
        self.assertIn("external_filter_recommended", vhf)

    def test_a_receiver_without_filters_says_so_rather_than_claiming_them(self):
        backend = SyntheticBackend(integration_s=0.01)
        meta = backend.set_filters(BAND_PLANS["vhf_lmr_low"].front_end)
        self.assertFalse(meta["actuated"])
        self.assertIn("no switchable filters", meta["actuated_note"])


class TestBaselineIndependence(unittest.TestCase):
    """A targeted channel set must not supply its own noise floor."""

    def test_baseline_grid_spans_the_passband_not_the_channel_list(self):
        from occupancy_survey.detector import baseline_grid
        cells = baseline_grid(center_hz=151_000_000.0, span_hz=2_000_000.0,
                              bandwidth_hz=11_250.0)
        self.assertGreaterEqual(len(cells), 8)
        lo = min(c.freq_hz for c in cells)
        hi = max(c.freq_hz for c in cells)
        self.assertLess(lo, 150_100_000.0)
        self.assertGreater(hi, 151_900_000.0)
        for c in cells:
            self.assertEqual(c.bandwidth_hz, 11_250.0,
                             "grid cells must match the real channel width, "
                             "because the max-bin bias depends on it")


class TestGainStepping(unittest.TestCase):

    def test_a_backend_that_cannot_step_gain_reports_false(self):
        class _Fixed(Backend):
            def receiver_metadata(self): return {}
            def tune(self, c, s): pass
            def freq_axis(self): return []
            def sweeps(self, d): return iter(())
        self.assertFalse(_Fixed().set_gain_offset_db(-6.0))

    def test_the_intermod_check_is_skipped_not_faked(self):
        backend = SyntheticBackend(integration_s=0.01)
        design = SamplingDesign(integration_s=0.01, dwell_s=0.5,
                                usable_bandwidth_hz=2e6)
        bands = resolve_bands(["nwr"])
        with tempfile.TemporaryDirectory() as d:
            survey = Survey(bands, backend, SiteMetadata("s", "x", "y"),
                            design, out_dir=d)
            from occupancy_survey.schedule import tune_steps
            step = tune_steps(bands[0], design)[0]
            self.assertIsNone(survey.intermod_check(bands[0], step),
                              "a backend that cannot change gain must return "
                              "None, not a clean bill of health")


if __name__ == "__main__":
    unittest.main()
