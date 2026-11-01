"""Receiver spurs: centres kept off channels, and a measured list tagged.

Measured 2026-11-01 on an RSPdx at CM87. Tuning centres 456.000, 162.475
and probably 154.700 read the receiver's DC spike as a carrier -- 456.000
read the same with no antenna -- while 151.200, 152.400 and 157.200 are
fixed spurs that read the same on an open port wherever the tuning was.
"""

import copy
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import BAND_PLANS, DEFAULT_SCHEDULE, resolve_bands
from occupancy_survey.config import AppConfig
from occupancy_survey.dataset import read_dataset, service_occupancy
from occupancy_survey.detector import ChannelLevelExtractor
from occupancy_survey.schedule import SamplingDesign, tune_steps
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


class TestCentresAvoidChannels(unittest.TestCase):

    def test_no_default_band_has_a_channel_on_a_tuning_centre(self):
        """At the RSPdx's 6 MHz. A narrower receiver can lack the slack to
        move a centre (FM at 2 MHz has 38 kHz and needs 100); there the
        centre stays and the survey's DC guard bins cover it."""
        for design in (SamplingDesign(),):
            for name in DEFAULT_SCHEDULE:
                band = BAND_PLANS[name]
                half = 0.5 * band.channel_bandwidth_hz
                for st in tune_steps(band, design):
                    nearest = min(abs(c.freq_hz - st.center_hz)
                                  for c in band.channels())
                    self.assertGreaterEqual(
                        nearest, half,
                        f"{name} centre {st.center_hz/1e6:.5f} MHz sits on a "
                        f"channel; its DC spike would read as a carrier")

    def test_every_channel_stays_inside_some_usable_window(self):
        """Moving a centre must not push an edge channel out of coverage.

        At 6 MHz only. At 2 MHz the FM band's end channels (180 kHz wide)
        already overhang the usable window with or without the move -- a
        pre-existing limit on a narrow receiver's FM control, noted
        separately -- and a centre never moves further than the slack."""
        self._covered(SamplingDesign())

    def test_a_centre_never_moves_further_than_its_slack(self):
        for bw in (6e6, 2e6):
            design = SamplingDesign(usable_bandwidth_hz=bw)
            usable = design.usable_bandwidth_hz * design.usable_fraction
            for name in DEFAULT_SCHEDULE:
                band = BAND_PLANS[name]
                import math
                n = max(1, math.ceil(band.span_hz / usable))
                w = band.span_hz / n
                for i, st in enumerate(tune_steps(band, design)):
                    plain = band.freq_min_hz + (i + 0.5) * w
                    self.assertLessEqual(abs(st.center_hz - plain),
                                         0.5 * (usable - w) + 1e-6, name)

    def _covered(self, design):
        usable = design.usable_bandwidth_hz * design.usable_fraction
        for name in DEFAULT_SCHEDULE:
            band = BAND_PLANS[name]
            steps = tune_steps(band, design)
            half = 0.5 * band.channel_bandwidth_hz
            for c in band.channels():
                self.assertTrue(any(
                    abs(c.freq_hz - s.center_hz) + half <= 0.5 * usable
                    for s in steps), f"{name} {c.freq_hz/1e6:.4f} uncovered")


class TestDcGuard(unittest.TestCase):

    def _axis(self, n=1024, centre=456e6, fs=2e6):
        return centre - fs / 2 + fs * np.arange(n) / n

    def test_a_spike_at_the_centre_does_not_reach_a_channel(self):
        axis = self._axis()
        from occupancy_survey.channels import Channel
        bin_hz = axis[1] - axis[0]
        # A channel whose edge reaches one bin past the centre.
        ch = Channel(freq_hz=456e6 + 6 * bin_hz, bandwidth_hz=12 * bin_hz,
                     service="IG")
        spec = np.zeros(axis.size)
        spec[axis.size // 2] = 40.0                 # the DC spike
        on = ChannelLevelExtractor([ch], axis, usable_fraction=1.0,
                                   dc_guard_bins=1).levels_db(spec)[0]
        off = ChannelLevelExtractor([ch], axis, usable_fraction=1.0,
                                    dc_guard_bins=0).levels_db(spec)[0]
        self.assertEqual(off, 40.0)
        self.assertEqual(on, 0.0)

    def test_the_guard_is_off_by_default(self):
        """rtl_power's stitched axis has no single centre to guard."""
        axis = self._axis()
        from occupancy_survey.channels import Channel
        ch = Channel(freq_hz=456e6, bandwidth_hz=11_250.0, service="IG")
        spec = np.zeros(axis.size)
        spec[axis.size // 2] = 40.0
        self.assertEqual(ChannelLevelExtractor(
            [ch], axis, usable_fraction=1.0).levels_db(spec)[0], 40.0)


class TestSpurList(unittest.TestCase):

    def _run(self, spurs):
        design = SamplingDesign(integration_s=0.01, dwell_s=0.5,
                                interval_s=600.0, usable_bandwidth_hz=2e6)
        band = resolve_bands(["vhf_lmr_low"])[0]
        c = tune_steps(band, design)[0].center_hz
        spur = round((c + 300e3) / 12_500) * 12_500
        real = round((c - 300e3) / 12_500) * 12_500
        em = [SyntheticEmitter(spur, 25.0, duty=1.0),
              SyntheticEmitter(real, 25.0, duty=1.0)]
        backend = SyntheticBackend(em, integration_s=0.01, sample_rate_hz=2e6)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey([band], backend, SiteMetadata("s", "x", "y"),
                           design, out_dir=d, seed=3,
                           spurs_mhz=[spur / 1e6] if spurs else ()
                           ).run(cycles=2)
            ds = [read_dataset(p) for p in paths]
        return ds, spur, real

    def test_a_listed_spur_is_tagged_and_recorded(self):
        ds, spur, real = self._run(spurs=True)
        chans = {c["freq_hz"]: c for c in ds[0]["bands"]["vhf_lmr_low"]["channels"]}
        self.assertTrue(chans[spur].get("receiver_spur"))
        self.assertFalse(chans[real].get("receiver_spur", False))
        self.assertEqual(ds[0]["acquisition"]["receiver"]["spurs_mhz"],
                         [spur / 1e6])
        self.assertTrue(ds[0]["bands"]["vhf_lmr_low"]["tune_centres_hz"])

    def test_service_figures_leave_spurs_out_and_say_so(self):
        ds, *_ = self._run(spurs=True)
        t = service_occupancy(ds, min_decided=1)["targets"]["vhf_lmr_low"]
        self.assertEqual(t["channels_excluded_as_receiver_spurs"], 1)
        self.assertEqual(t["channels_active"], 1)

    def test_without_a_list_nothing_is_tagged(self):
        ds, spur, _ = self._run(spurs=False)
        chans = {c["freq_hz"]: c for c in ds[0]["bands"]["vhf_lmr_low"]["channels"]}
        self.assertNotIn("receiver_spur", chans[spur])

    def test_config_refuses_a_malformed_list(self):
        raw = copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
        raw["sdr"]["spurs_mhz"] = ["one fifty two"]
        with self.assertRaises(ValueError):
            AppConfig.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
