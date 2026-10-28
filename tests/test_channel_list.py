"""Targeted channel lists: covering, tagging, and what targeting costs."""

import json
import tempfile
import unittest
from pathlib import Path

from occupancy_survey.channels import (BAND_PLANS, CONTROL, Channel, TARGET,
                                       load_channel_list)
from occupancy_survey.schedule import SamplingDesign, cover_channels, tune_steps


def _list_file(d, bands):
    p = Path(d) / "channels.json"
    p.write_text(json.dumps({
        "schema": "occupancy-survey/channel-list/1",
        "bands": bands,
    }))
    return p


def _band(freqs, role=TARGET, bw=11_250.0, expected=None, service="IG"):
    return {"role": role, "service": service, "channel_bandwidth_hz": bw,
            "expected_occupancy": expected,
            "channels": [{"freq_hz": f, "service": service,
                          "continuity": "intermittent", "expected_duty": None,
                          "owner": "o", "field_proxy_w_per_m2": 1e-5,
                          "distance_m": 5000.0, "near_summit": None}
                         for f in freqs]}


class TestCovering(unittest.TestCase):

    def test_sparse_channels_cost_one_window_per_cluster(self):
        chans = ([Channel(151e6 + i * 12.5e3, 11_250.0, "IG") for i in range(12)]
                 + [Channel(460e6 + i * 12.5e3, 11_250.0, "IG")
                    for i in range(20)])
        windows = cover_channels(chans, 2.048e6 * 0.8)
        self.assertEqual(len(windows), 2,
                         "two tight clusters 300 MHz apart need two windows")

    def test_every_channel_lands_inside_some_window(self):
        chans = [Channel(151e6 + i * 250e3, 11_250.0, "IG") for i in range(40)]
        usable = 2.048e6 * 0.8
        windows = cover_channels(chans, usable)
        for c in chans:
            self.assertTrue(
                any(abs(c.freq_hz - w) + 0.5 * c.bandwidth_hz <= usable / 2
                    for w, _lo in windows),
                f"{c.freq_hz/1e6:.4f} MHz fell between windows")

    def test_a_channel_wider_than_the_passband_is_refused(self):
        with self.assertRaises(ValueError):
            cover_channels([Channel(100e6, 5e6, "FM")], 2e6)

    def test_an_empty_list_covers_nothing(self):
        self.assertEqual(cover_channels([], 2e6), [])


class TestLoading(unittest.TestCase):

    def test_a_targeted_band_keeps_role_and_filter_plan(self):
        """Controls must stay controls, and keep their notch state."""
        with tempfile.TemporaryDirectory() as d:
            p = _list_file(d, {
                "bcast_fm": _band([88.5e6, 101.3e6], role=CONTROL,
                                  bw=180_000.0, expected=1.0, service="FM"),
            })
            plans = {b.name: b for b in load_channel_list(p)}
        fm = plans["bcast_fm"]
        self.assertEqual(fm.role, CONTROL)
        self.assertEqual(fm.expected_occupancy, 1.0)
        self.assertFalse(fm.front_end.rf_notch,
                         "a targeted FM control still needs the notch OFF")
        self.assertTrue(fm.targeted)

    def test_a_targeted_vhf_band_inherits_the_notch_on(self):
        with tempfile.TemporaryDirectory() as d:
            p = _list_file(d, {"vhf_lmr_low": _band([151e6, 152e6])})
            plans = {b.name: b for b in load_channel_list(p)}
        self.assertTrue(plans["vhf_lmr_low"].front_end.rf_notch)

    def test_channels_are_exactly_the_listed_frequencies(self):
        freqs = [151.0135e6, 151.4765e6, 155.9055e6]
        with tempfile.TemporaryDirectory() as d:
            p = _list_file(d, {"vhf_lmr_low": _band(freqs)})
            band = load_channel_list(p)[0]
        got = [c.freq_hz for c in band.channels()]
        self.assertEqual(got, freqs,
                         "FCC frequencies must be used exactly -- snapping "
                         "them to a grid reintroduces the splitting error "
                         "that targeting removes")

    def test_a_foreign_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text('{"schema": "something/1", "bands": {}}')
            with self.assertRaises(ValueError):
                load_channel_list(p)

    def test_an_empty_list_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                load_channel_list(_list_file(d, {}))

    def test_the_note_warns_that_discovery_is_foreclosed(self):
        with tempfile.TemporaryDirectory() as d:
            band = load_channel_list(
                _list_file(d, {"vhf_lmr_low": _band([151e6])}))[0]
        self.assertIn("CANNOT find a busy channel with no FCC record",
                      band.note)


class TestTargetingCost(unittest.TestCase):
    """Pin the measured saving, because I guessed it wrong once."""

    def test_targeting_saves_tune_steps_but_not_an_order_of_magnitude(self):
        """Licensed channels are spread across their band, not clustered.

        A targeted VHF set spanning nearly the whole allocation needs the
        same tuning windows as the grid. docs/sampling-design.md states a
        30% cycle saving overall; this pins the mechanism behind it.
        """
        design = SamplingDesign(usable_bandwidth_hz=6e6)
        grid_steps = len(tune_steps(BAND_PLANS["vhf_lmr_low"], design))

        # 60 channels spread over 4.99 MHz, as the real prediction gives.
        freqs = [150.81e6 + i * (4.99e6 / 59) for i in range(60)]
        with tempfile.TemporaryDirectory() as d:
            band = load_channel_list(
                _list_file(d, {"vhf_lmr_low": _band(freqs)}))[0]
        targeted_steps = len(tune_steps(band, design))

        self.assertEqual(targeted_steps, grid_steps,
                         "a targeted set spanning the whole band saves no "
                         "tune steps; if this ever passes with fewer, the "
                         "30% figure in the docs needs recomputing")

    def test_a_genuinely_clustered_band_does_save(self):
        design = SamplingDesign(usable_bandwidth_hz=6e6)
        freqs = [151.0e6 + i * 12.5e3 for i in range(60)]   # 0.75 MHz span
        with tempfile.TemporaryDirectory() as d:
            band = load_channel_list(
                _list_file(d, {"vhf_lmr_low": _band(freqs)}))[0]
        self.assertEqual(len(tune_steps(band, design)), 1)


if __name__ == "__main__":
    unittest.main()
