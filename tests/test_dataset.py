"""The on-disk format: self-describing, refusing to be ambiguous, mergeable."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from occupancy_survey.channels import BAND_PLANS, BandPlan, CONTROL, TARGET
from occupancy_survey.dataset import (SCHEMA, AcquisitionMetadata,
                                      IntervalDataset, merge_channels,
                                      read_dataset, service_occupancy)
from occupancy_survey.detector import (BandBaseline, ChannelLevelExtractor,
                                       ThresholdPolicy, decide)
from occupancy_survey.occupancy import BandAccumulator, HistogramSpec
from occupancy_survey.schedule import SamplingDesign, Schedule
from occupancy_survey.site import SiteMetadata
from tests.synthetic_band import SyntheticBand, SyntheticEmitter


def _plan(name="test", role=TARGET, expected=None):
    return BandPlan(name=name, freq_min_hz=150_800_000.0,
                    freq_max_hz=151_800_000.0, channel_step_hz=12_500.0,
                    channel_bandwidth_hz=11_250.0, service="IG", role=role,
                    expected_occupancy=expected)


def _metadata(site_id="site-a", gain=40.0, agc=False, policy=None):
    site = SiteMetadata(site_id=site_id, receiver_model="SDRplay RSPdx",
                        antenna="discone at 6 m", grid_square="CM87")
    design = SamplingDesign()
    policy = policy or ThresholdPolicy()
    sched = Schedule([_plan()], design, seed=1)
    return AcquisitionMetadata(
        site=site.as_metadata(),
        sampling=sched.design_summary(),
        detection=policy.as_metadata(),
        histogram=HistogramSpec().as_metadata(),
        receiver={"backend": "synthetic", "gain": gain, "agc_enabled": agc,
                  "sample_rate_hz": 2_000_000.0, "fft_size": 4096},
    )


def _accumulate(plan, emitters, n_sweeps=300, policy=None, seed=3):
    policy = policy or ThresholdPolicy()
    channels = plan.channels()
    band = SyntheticBand(plan.freq_min_hz - 100_000.0,
                         plan.freq_max_hz + 100_000.0, bin_hz=1_000.0,
                         emitters=emitters, seed=seed)
    ex = ChannelLevelExtractor(channels, band.freq_axis, usable_fraction=1.0)
    baseline = BandBaseline(policy)
    acc = BandAccumulator(plan, channels, HistogramSpec(), policy)
    for i in range(n_sweeps):
        levels = ex.levels_db(band.sweep(i))
        acc.add_sweep(decide(levels, baseline.update(levels), policy),
                      clip_fraction=0.0)
    return acc, ex


def _dataset(site_id="site-a", emitters=(), plan=None, n_sweeps=300, seed=3,
             policy=None):
    plan = plan or _plan()
    policy = policy or ThresholdPolicy()
    acc, ex = _accumulate(plan, list(emitters), n_sweeps, policy, seed)
    start = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    # The recorded threshold must come from the SAME policy that did the
    # accumulating. Threading it through here rather than letting _metadata
    # build its own is not tidiness: a dataset whose metadata states a
    # threshold the histograms were not built at is silently wrong.
    ds = IntervalDataset(metadata=_metadata(site_id, policy=policy),
                         interval_start=start,
                         interval_end=start + timedelta(hours=1))
    ds.add_band(plan, acc, ex.bins_per_channel, ex.n_observed)
    return ds


class TestRefusals(unittest.TestCase):
    """A file that cannot be interpreted later must not be written."""

    def test_agc_on_is_refused(self):
        ds = _dataset()
        ds.metadata.receiver["agc_enabled"] = True
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as cm:
                ds.write(d)
        self.assertIn("AGC", str(cm.exception))

    def test_missing_threshold_is_refused(self):
        ds = _dataset()
        del ds.metadata.detection["threshold_db"]
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as cm:
                ds.write(d)
        self.assertIn("threshold_db", str(cm.exception))

    def test_missing_gain_is_refused(self):
        ds = _dataset()
        ds.metadata.receiver["gain"] = None
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                ds.write(d)

    def test_an_empty_interval_is_refused(self):
        start = datetime(2026, 10, 28, tzinfo=timezone.utc)
        ds = IntervalDataset(metadata=_metadata(), interval_start=start,
                             interval_end=start + timedelta(hours=1))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                ds.write(d)

    def test_a_foreign_schema_is_refused_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text('{"schema": "something-else/9", "bands": {}}')
            with self.assertRaises(ValueError) as cm:
                read_dataset(p)
        self.assertIn("Refusing to guess", str(cm.exception))


class TestRoundTrip(unittest.TestCase):

    def test_every_acquisition_condition_survives_the_round_trip(self):
        f = 151_000_000.0
        ds = _dataset(emitters=[SyntheticEmitter(f, snr_db=25.0, duty=0.5,
                                                 period_sweeps=20)])
        with tempfile.TemporaryDirectory() as d:
            path = ds.write(d)
            self.assertTrue(path.name.startswith("occ_site-a_2026"))
            back = read_dataset(path)

        self.assertEqual(back["schema"], SCHEMA)
        acq = back["acquisition"]
        self.assertEqual(acq["detection"]["threshold_db"], 10.0)
        self.assertEqual(acq["detection"]["estimator"],
                         "band_pooled_percentile")
        self.assertFalse(acq["receiver"]["agc_enabled"])
        self.assertEqual(acq["receiver"]["gain"], 40.0)
        self.assertEqual(acq["sampling"]["dwell_s"], 20.0)
        self.assertEqual(acq["sampling"]["integration_s"], 0.1)
        self.assertIn("min_detectable_burst_s_at_6db_margin", acq["sampling"])
        self.assertEqual(acq["site"]["grid_square"], "CM87")
        self.assertTrue(acq["site"]["location_shared"])
        self.assertGreater(back["bands"]["test"]["bins_per_channel"], 1)

        chans = {c["freq_hz"]: c for c in back["bands"]["test"]["channels"]}
        self.assertAlmostEqual(chans[f]["occupancy"], 0.5, delta=0.05)

    def test_a_withheld_location_still_produces_a_valid_dataset(self):
        site = SiteMetadata(site_id="anon", receiver_model="RTL-SDR v3",
                            antenna="whip")
        meta = _metadata()
        meta.site = site.as_metadata()
        plan = _plan()
        acc, ex = _accumulate(plan, [])
        start = datetime(2026, 10, 28, tzinfo=timezone.utc)
        ds = IntervalDataset(metadata=meta, interval_start=start,
                             interval_end=start + timedelta(hours=1))
        ds.add_band(plan, acc, ex.bins_per_channel, ex.n_observed)
        with tempfile.TemporaryDirectory() as d:
            back = read_dataset(ds.write(d))
        self.assertIsNone(back["acquisition"]["site"]["grid_square"])
        self.assertFalse(back["acquisition"]["site"]["location_shared"])
        self.assertEqual(back["acquisition"]["site"]["location_precision"],
                         "withheld")

    def test_the_grid_caveat_travels_with_a_gridded_band(self):
        with tempfile.TemporaryDirectory() as d:
            back = read_dataset(_dataset().write(d))
        self.assertEqual(back["bands"]["test"]["channel_plan"],
                         "uniform_grid")
        self.assertIn("not the FCC channel plan",
                      back["bands"]["test"]["grid_caveat"])


class TestMerging(unittest.TestCase):

    def test_two_sites_pool_onto_a_common_excess_db_axis(self):
        """The property the format exists for.

        Two receivers with different absolute noise floors must pool,
        because each histogram has had its own baseline subtracted.
        """
        f = 151_000_000.0
        em = [SyntheticEmitter(f, snr_db=25.0, duty=1.0)]
        a = _dataset("site-a", em, seed=3).to_dict()
        b = _dataset("site-b", em, seed=9).to_dict()

        merged = merge_channels([a, b], "test")
        by_freq = {m.freq_hz: m for m in merged}
        self.assertIn(f, by_freq)
        m = by_freq[f]
        self.assertEqual(m.sites, {"site-a", "site-b"})
        self.assertGreater(m.occupancy_at(10.0), 0.99)
        self.assertTrue(m.comparable)

        single = merge_channels([a], "test")
        self.assertEqual(m.n_decided,
                         2 * {x.freq_hz: x for x in single}[f].n_decided)

    def test_merging_is_rethresholdable(self):
        f = 151_000_000.0
        a = _dataset("site-a", [SyntheticEmitter(f, 18.0, 1.0)], seed=3)
        merged = {m.freq_hz: m for m in merge_channels([a.to_dict()], "test")}
        self.assertGreater(merged[f].occupancy_at(5.0), 0.99)
        self.assertLess(merged[f].occupancy_at(40.0), 0.01)

    def test_mismatched_histogram_specs_are_refused(self):
        a = _dataset("site-a").to_dict()
        b = _dataset("site-b").to_dict()
        b["acquisition"]["histogram"]["bin_db"] = 2.0
        with self.assertRaises(ValueError) as cm:
            merge_channels([a, b], "test")
        self.assertIn("histogram specs", str(cm.exception))

    def test_differing_channel_widths_are_flagged_not_hidden(self):
        a = _dataset("site-a").to_dict()
        b = _dataset("site-b").to_dict()
        b["bands"]["test"]["bins_per_channel"] = 180
        merged = merge_channels([a, b], "test")
        self.assertTrue(merged)
        self.assertFalse(any(m.comparable for m in merged),
                         "pooling across channel widths must set the flag, "
                         "because the excess-dB zero points differ")


class TestServiceOccupancy(unittest.TestCase):
    """The statistic the survey exists to produce."""

    def test_mean_and_active_channel_occupancy_differ_as_documented(self):
        # One busy channel on a grid of 81: the mean over all channels is
        # tiny, the mean over active channels is the duty cycle. Confusing
        # the two is the mistake this split exists to prevent.
        f = 151_000_000.0
        ds = _dataset("site-a", [SyntheticEmitter(f, 25.0, duty=0.5,
                                                  period_sweeps=20)],
                      n_sweeps=600).to_dict()
        out = service_occupancy([ds], min_decided=10)
        t = out["targets"]["test"]
        self.assertAlmostEqual(t["active_channel_occupancy"], 0.5, delta=0.06)
        self.assertLess(t["mean_channel_occupancy"], 0.05)
        self.assertEqual(t["channels_active"], 1)
        self.assertEqual(t["sites"], ["site-a"])

    def test_control_bands_are_reported_separately(self):
        plan = _plan(name="ctl", role=CONTROL, expected=1.0)
        f = 151_000_000.0
        ds = _dataset("site-a", [SyntheticEmitter(f, 25.0, 1.0)], plan=plan,
                      n_sweeps=300).to_dict()
        out = service_occupancy([ds], min_decided=10)
        self.assertIn("ctl", out["controls"])
        self.assertNotIn("ctl", out["targets"])
        check = out["controls"]["ctl"]["control_checks"][0]
        self.assertEqual(check["verdict"], "pass")

    def test_a_control_with_no_carrier_fails_its_check(self):
        plan = _plan(name="ctl", role=CONTROL, expected=1.0)
        ds = _dataset("site-a", [], plan=plan, n_sweeps=300).to_dict()
        check = ds["bands"]["ctl"]["control_check"]
        self.assertEqual(check["verdict"], "fail")
        self.assertIn("receive path", check["meaning"])

    def test_mixed_default_thresholds_must_be_resolved_explicitly(self):
        a = _dataset("site-a").to_dict()
        b = _dataset("site-b", policy=ThresholdPolicy(threshold_db=14.0)
                     ).to_dict()
        with self.assertRaises(ValueError) as cm:
            service_occupancy([a, b])
        self.assertIn("different default", str(cm.exception))
        out = service_occupancy([a, b], threshold_db=12.0)
        self.assertEqual(out["targets"]["test"]["threshold_db"], 12.0)


if __name__ == "__main__":
    unittest.main()
