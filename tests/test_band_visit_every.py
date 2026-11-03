"""References visited every Nth pass: sampling.band_visit_every.

Broadcast FM and NWR answer "is the receiver working", and with every
predicted FM station in the list they took 6 of 13 tune steps -- nearly
half of every pass -- from the land-mobile channels the survey is for.
Measured with --plan on the CM87 targeted list: 13 steps / 266 s per pass
before, 7.5 / 154 s with FM cut to one window and both references every
4th pass.
"""

import copy
import sys
import unittest
from pathlib import Path

import yaml

from occupancy_survey.channels import resolve_bands
from occupancy_survey.config import AppConfig
from occupancy_survey.schedule import (SamplingDesign, Schedule,
                                       cycle_time_s, steps_per_pass,
                                       tune_steps)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from make_channel_list import narrow_controls  # noqa: E402


def _design(**kw):
    return SamplingDesign(usable_bandwidth_hz=6e6, jitter_fraction=0.0, **kw)


class TestSchedule(unittest.TestCase):

    def setUp(self):
        self.bands = resolve_bands(["vhf_lmr_low", "bcast_fm"])

    def _passes(self, design, n):
        """Band names visited on each of n passes, one call per pass as the
        survey loop makes them."""
        sched = Schedule(self.bands, design, seed=1)
        return [{b.name for b, _s, _d in sched.visits(cycles=1)}
                for _ in range(n)]

    def test_a_band_appears_on_every_nth_pass_counted_across_calls(self):
        passes = self._passes(_design(band_visit_every={"bcast_fm": 4}), 9)
        fm = [i for i, names in enumerate(passes) if "bcast_fm" in names]
        self.assertEqual(fm, [0, 4, 8])
        self.assertTrue(all("vhf_lmr_low" in names for names in passes))

    def test_the_first_pass_has_every_band(self):
        """A dead reference must show in the first pass, not the Nth."""
        passes = self._passes(_design(band_visit_every={"bcast_fm": 6}), 1)
        self.assertEqual(passes[0], {"vhf_lmr_low", "bcast_fm"})

    def test_cycle_time_counts_a_reduced_band_as_a_fraction(self):
        d1, d4 = _design(), _design(band_visit_every={"bcast_fm": 4})
        lmr = len(tune_steps(self.bands[0], d1))
        fm = len(tune_steps(self.bands[1], d1))
        self.assertAlmostEqual(steps_per_pass(self.bands, d4),
                               lmr + fm / 4)
        self.assertAlmostEqual(cycle_time_s(self.bands, d4),
                               (lmr + fm / 4) * (d4.dwell_s + 0.5))

    def test_default_is_every_pass(self):
        d = _design()
        self.assertEqual(d.visit_every("bcast_fm"), 1)
        self.assertEqual(len(self._passes(d, 3)[2]), 2)

    def test_bad_values_are_refused(self):
        for bad in (0, -1, 1.5, True, "4"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _design(band_visit_every={"bcast_fm": bad})

    def test_an_unknown_band_name_is_warned_about(self):
        sched = Schedule(self.bands, _design(band_visit_every={"bcats": 4}))
        self.assertTrue(any("'bcats'" in w for w in sched.warnings()))

    def test_a_band_skipped_past_a_whole_interval_is_warned_about(self):
        sched = Schedule(self.bands, _design(band_visit_every={"bcast_fm": 50},
                                             interval_s=600.0))
        self.assertTrue(any("no bcast_fm samples" in w
                            for w in sched.warnings()))

    def test_the_setting_is_recorded_in_the_dataset_metadata(self):
        sched = Schedule(self.bands, _design(band_visit_every={"bcast_fm": 4}))
        summary = sched.design_summary()
        self.assertEqual(summary["band_visit_every"], {"bcast_fm": 4})
        self.assertIn("tune_steps_per_pass", summary)


class TestConfig(unittest.TestCase):

    def test_yaml_reaches_the_design(self):
        raw = yaml.safe_load(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        raw = copy.deepcopy(raw)
        raw["sampling"]["band_visit_every"] = {"bcast_fm": 4, "nwr": 4}
        cfg = AppConfig.from_dict(raw)
        self.assertEqual(cfg.sampling.visit_every("nwr"), 4)
        self.assertEqual(cfg.sampling.visit_every("uhf_lmr"), 1)


class _Plan:
    def __init__(self, role):
        self.role = role


class TestNarrowControls(unittest.TestCase):

    def _row(self, mhz, proxy):
        return {"freq_hz": mhz * 1e6, "field_proxy_w_per_m2": proxy}

    def test_control_keeps_one_window_round_its_strongest_station(self):
        selected = {
            "bcast_fm": [self._row(88.5, 3e-6), self._row(105.7, 1e-5),
                         self._row(106.9, 9e-7), self._row(104.1, 5e-8)],
            "uhf_lmr": [self._row(462.3, 1e-9), self._row(451.5, 1e-9)],
        }
        plans = {"bcast_fm": _Plan("control"), "uhf_lmr": _Plan("target")}
        out = narrow_controls(selected, plans, 2.4)
        self.assertEqual([r["freq_hz"] / 1e6 for r in out["bcast_fm"]],
                         [105.7, 106.9])
        self.assertEqual(out["uhf_lmr"], selected["uhf_lmr"])


if __name__ == "__main__":
    unittest.main()
