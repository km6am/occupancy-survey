"""The runner: coherence between the design, the backend and the metadata."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import resolve_bands
from occupancy_survey.dataset import read_dataset, service_occupancy
from occupancy_survey.detector import ThresholdPolicy
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter

SITE = dict(site_id="bench", receiver_model="synthetic", antenna="none",
            grid_square="CM87")


def _design(**kw):
    base = dict(integration_s=0.01, dwell_s=2.0, interval_s=60.0,
                usable_bandwidth_hz=2e6)
    base.update(kw)
    return SamplingDesign(**base)


class TestBandwidthCoherence(unittest.TestCase):
    """The worst failure this tool could have, made impossible."""

    def test_a_design_wider_than_the_receiver_is_refused(self):
        # 6 MHz assumed (the default) against a 2 MHz backend: two thirds of
        # every band would never be observed, and it would look like quiet
        # channels rather than an error.
        survey = Survey(resolve_bands(["vhf_lmr_low"]),
                        SyntheticBackend(integration_s=0.01),
                        SiteMetadata(**SITE),
                        _design(usable_bandwidth_hz=6e6))
        with self.assertRaises(ValueError) as cm:
            survey.run(cycles=1)
        msg = str(cm.exception)
        self.assertIn("never be observed", msg)
        self.assertIn("usable_bandwidth_hz", msg)

    def test_a_matched_design_runs(self):
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["nwr"]),
                           SyntheticBackend(integration_s=0.01),
                           SiteMetadata(**SITE), _design(),
                           out_dir=d).run(cycles=2)
            self.assertTrue(paths)


class TestEndToEndRecovery(unittest.TestCase):

    def _run(self, emitters, bands, cycles=40, **design_kw):
        design = _design(**design_kw)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(bands),
                           SyntheticBackend(emitters, integration_s=0.01),
                           SiteMetadata(**SITE), design, out_dir=d,
                           seed=5).run(cycles=cycles)
            return [read_dataset(p) for p in paths]

    def test_duty_cycles_survive_the_whole_pipeline(self):
        """Through schedule, retune, accumulate, write and read back."""
        cases = {151_000_000.0: 0.225, 151_500_000.0: 1.0}
        emitters = [
            SyntheticEmitter(151_000_000.0, 25.0, duty=0.225,
                             period_sweeps=40),
            SyntheticEmitter(151_500_000.0, 25.0, duty=1.0),
        ]
        dss = self._run(emitters, ["vhf_lmr_low"])
        self.assertTrue(dss)
        chans = {c["freq_hz"]: c
                 for d in dss for c in d["bands"]["vhf_lmr_low"]["channels"]}
        for f, duty in cases.items():
            self.assertAlmostEqual(chans[f]["occupancy"], duty, delta=0.03,
                                   msg=f"{f/1e6:.3f} MHz")

        out = service_occupancy(dss, min_decided=10)
        self.assertEqual(out["targets"]["vhf_lmr_low"]["channels_active"], 2)

    def test_a_control_band_passes_when_its_carrier_is_present(self):
        em = [SyntheticEmitter(162_475_000.0, 30.0, duty=1.0,
                               bandwidth_hz=14_000.0)]
        dss = self._run(em, ["nwr"], cycles=10)
        check = dss[0]["bands"]["nwr"]["control_check"]
        self.assertEqual(check["verdict"], "pass")

    def test_a_control_band_fails_when_nothing_is_receivable(self):
        """The calibration working: no carrier, failed control."""
        dss = self._run([], ["nwr"], cycles=10)
        self.assertEqual(dss[0]["bands"]["nwr"]["control_check"]["verdict"],
                         "fail")

    def test_channels_are_covered_across_a_multistep_band(self):
        """A band needing several tunings must measure all of it.

        One emitter near each end of the span and one in the middle: all
        three must appear, or the tune-step decomposition is leaving gaps.
        """
        edges = [150_900_000.0, 153_400_000.0, 155_900_000.0]
        em = [SyntheticEmitter(f, 25.0, duty=1.0) for f in edges]
        # Coverage, not duty resolution: a coarse integration keeps the
        # sweep count (and so the runtime) down without changing what is
        # being asserted.
        dss = self._run(em, ["vhf_lmr_low"], cycles=8, integration_s=0.1,
                        dwell_s=1.0)
        chans = {c["freq_hz"]: c
                 for d in dss for c in d["bands"]["vhf_lmr_low"]["channels"]}
        for f in edges:
            self.assertIn(f, chans, f"{f/1e6:.3f} MHz was never observed")
            self.assertGreater(chans[f]["occupancy"], 0.9,
                               f"{f/1e6:.3f} MHz under-measured -- a tune "
                               f"step boundary may be clipping it")


class TestMetadataCoherence(unittest.TestCase):

    def test_the_written_threshold_is_the_one_that_was_used(self):
        """The hazard a test helper hit during development.

        A dataset whose stated threshold differs from the one its histograms
        were built at is wrong in a way nothing in the file can reveal.
        """
        policy = ThresholdPolicy(threshold_db=17.0, baseline_percentile=30.0)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["nwr"]),
                           SyntheticBackend(integration_s=0.01),
                           SiteMetadata(**SITE), _design(), policy=policy,
                           out_dir=d).run(cycles=3)
            back = read_dataset(paths[0])
        self.assertEqual(back["acquisition"]["detection"]["threshold_db"],
                         17.0)
        self.assertEqual(
            back["acquisition"]["detection"]["baseline_percentile"], 30.0)

    def test_the_synthetic_backend_marks_itself(self):
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["nwr"]),
                           SyntheticBackend(integration_s=0.01),
                           SiteMetadata(**SITE), _design(),
                           out_dir=d).run(cycles=2)
            back = read_dataset(paths[0])
        self.assertTrue(back["acquisition"]["receiver"]["synthetic"])
        self.assertIn("NO HARDWARE",
                      back["acquisition"]["receiver"]["note"])

    def test_the_sampling_bounds_are_written_with_the_data(self):
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["nwr"]),
                           SyntheticBackend(integration_s=0.01),
                           SiteMetadata(**SITE), _design(jitter_fraction=0.0),
                           out_dir=d).run(cycles=2)
            back = read_dataset(paths[0])
        sampling = back["acquisition"]["sampling"]
        self.assertIn("min_detectable_burst_s_at_6db_margin", sampling)
        self.assertIn("blind_spot", sampling)
        self.assertTrue(any("alias" in w for w in sampling["warnings"]),
                        "a run taken without jitter must carry the aliasing "
                        "warning in the data, not only in a log")


class TestIntervalBoundaries(unittest.TestCase):

    def test_several_intervals_produce_several_files(self):
        clock = {"t": datetime(2026, 10, 28, tzinfo=timezone.utc)}

        def now_fn():
            clock["t"] += timedelta(seconds=30)
            return clock["t"]

        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["nwr"]),
                           SyntheticBackend(integration_s=0.01),
                           SiteMetadata(**SITE),
                           _design(interval_s=60.0), out_dir=d,
                           ).run(cycles=6, now_fn=now_fn)
        self.assertGreaterEqual(len(paths), 2,
                                "a 60 s interval over 6 cycles of a 30 s "
                                "clock should close more than one interval")
        self.assertEqual(len(set(p.name for p in paths)), len(paths),
                         "interval files must not collide on filename")


if __name__ == "__main__":
    unittest.main()
