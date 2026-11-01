"""The two-tier summit report: used or not, then how much.

A summit's channel list asks a narrower question than a band survey, and a
channel busy 5% of the time matters to an activator. These tests pin the
verdicts to the evidence that justifies each one.
"""

import importlib.util
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "summit_report", REPO / "tools" / "summit_report.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)

LO, BIN, NB = -10.0, 1.0, 70
SPV = 200                      # sweeps per visit: 20 s dwell / 0.1 s


def _rec(freq, n_decided, present_at_db=None, n_present=0, runs=0,
         mean_sweeps=0.0):
    """A channel record whose present sweeps all sit at one excess level."""
    hist = np.zeros(NB, int)
    hist[10] = n_decided - n_present            # noise at 0 dB excess
    if n_present:
        hist[int(present_at_db - LO)] = n_present
    return {"freq_hz": freq, "n_decided": n_decided, "n_present": n_present,
            "occupancy": n_present / n_decided, "hist": hist.tolist(),
            "underflow": 0, "overflow": 0,
            "runs": {"n": runs, "mean_sweeps": mean_sweeps}}


def _ds(records, band="vhf_lmr_low", hour=12.0, step=12_500.0):
    return {
        "interval": {"start_utc": "2026-11-02T20:00:00+00:00",
                     "end_utc": "2026-11-02T21:00:00+00:00",
                     "local_hour": hour},
        "acquisition": {
            "histogram": {"lo_db": LO, "bin_db": BIN, "n_bins": NB},
            "detection": {"threshold_db": 10.0},
            "sampling": {"integration_s": 0.1, "dwell_s": 20.0,
                         "sweeps_per_visit": SPV},
            "receiver": {}, "site": {"site_id": "t"}},
        "bands": {band: {"channel_step_hz": step, "channels": records}},
    }


def _listed(freq, band="vhf_lmr_low", summit="W6/NC-001", role="target",
            proxy=1e-6):
    return {"freq_hz": freq, "band": band, "band_role": role,
            "service": "IG", "owner": "X",
            "field_proxy_w_per_m2": proxy,
            "near_summit": {"summit": summit, "summit_name": "Test Peak"}
            if summit else None}


def _one(records, listed, **kw):
    groups, _ = sr.build([_ds(records)], listed, **kw)
    return {ch["freq_hz"]: v for _, rows in groups for ch, v in rows}


class TestTier1(unittest.TestCase):

    def test_a_channel_busy_five_percent_is_used(self):
        f = 151_100_000.0
        v = _one([_rec(f, 24 * SPV, 25, int(0.05 * 24 * SPV), runs=30,
                       mean_sweeps=8)], [_listed(f)])[f]
        self.assertEqual(v["tier1"], "used")
        self.assertAlmostEqual(v["duty"], 0.05, places=3)
        lo, hi = v["duty_ci95"]
        self.assertLess(lo, 0.05)
        self.assertGreater(hi, 0.05)

    def test_a_continuous_carrier_is_used_though_it_has_no_complete_run(self):
        """Every run of a carrier that never drops is cut by a retune and
        discarded, so it has zero complete transmissions. The first version
        called NWR 162.400 and two always-on UHF carriers 'trace'."""
        f = 151_100_000.0
        v = _one([_rec(f, 24 * SPV, 30, 24 * SPV, runs=0)], [_listed(f)])[f]
        self.assertEqual(v["tier1"], "used")
        # Burst taken as the full dwell: one independent sample per look.
        self.assertAlmostEqual(v["n_eff"], 24.0)

    def test_one_blip_is_a_trace_not_use(self):
        """A single short detection is not enough to call a channel used."""
        f = 151_100_000.0
        v = _one([_rec(f, 24 * SPV, 25, 3, runs=1, mean_sweeps=3)],
                 [_listed(f)])[f]
        self.assertEqual(v["tier1"], "trace")

    def test_silence_carries_the_duty_it_could_have_missed(self):
        f = 151_100_000.0
        v = _one([_rec(f, 60 * SPV)], [_listed(f)])[f]
        self.assertEqual(v["tier1"], "not heard")
        # 60 looks: 1 - 0.05**(1/60) = 4.9%
        self.assertAlmostEqual(v["duty_upper95"], 0.0487, places=3)

    def test_a_known_spur_is_not_called_used(self):
        """152.400 read 25 dB over baseline with no antenna attached."""
        f = 152_400_000.0
        v = _one([_rec(f, 24 * SPV, 25, 24 * SPV, runs=24, mean_sweeps=200)],
                 [_listed(f)])[f]
        self.assertEqual(v["tier1"], "spur")

    def test_detections_hugging_the_threshold_are_flagged(self):
        f = 151_100_000.0
        v = _one([_rec(f, 24 * SPV, 11, 500, runs=40, mean_sweeps=12)],
                 [_listed(f)])[f]
        self.assertEqual(v["tier1"], "used")
        self.assertTrue(v["level_marginal"])

    def test_silence_below_the_audibility_floor_is_inconclusive(self):
        """Not heard from the QTH is not 'unused at the summit' when the
        transmitter is weaker here than anything this receiver has heard."""
        fm, weak, strong = 88_500_000.0, 151_100_000.0, 151_200_100.0
        ds = _ds([_rec(weak, 30 * SPV), _rec(strong, 30 * SPV)])
        ds["bands"]["bcast_fm"] = {"channel_step_hz": 200_000.0, "channels": [
            _rec(fm, 30 * SPV, 30, 30 * SPV, runs=30, mean_sweeps=200)]}
        listed = [_listed(fm, band="bcast_fm", role="control", summit=None,
                          proxy=1e-4),
                  _listed(weak, proxy=1e-7), _listed(strong, proxy=1e-3)]
        groups, meta = sr.build([ds], listed, spurs_mhz=())
        got = {ch["freq_hz"]: v for _, rows in groups for ch, v in rows}
        self.assertEqual(meta["audibility_floor_proxy"], 1e-4)
        self.assertEqual(got[weak]["tier1"], "inconclusive")
        self.assertEqual(got[strong]["tier1"], "not heard")


class TestMatching(unittest.TestCase):

    def test_a_grid_match_says_it_may_be_split(self):
        listed_f, grid_f = 151_106_250.0, 151_100_000.0
        v = _one([_rec(grid_f, 24 * SPV, 25, 500, runs=20, mean_sweeps=25)],
                 [_listed(listed_f)])[listed_f]
        self.assertIn("grid cell", v["match"])

    def test_channels_pool_across_intervals(self):
        f = 151_100_000.0
        a = _ds([_rec(f, 12 * SPV, 25, 120, runs=10, mean_sweeps=12)])
        b = _ds([_rec(f, 12 * SPV, 25, 120, runs=10, mean_sweeps=12)])
        groups, _ = sr.build([a, b], [_listed(f)])
        v = groups[0][1][0][1]
        self.assertEqual(v["transmissions"], 20)
        self.assertAlmostEqual(v["visits"], 24.0)

    def test_untagged_channels_are_left_out_unless_asked(self):
        f = 151_100_000.0
        recs = [_rec(f, 24 * SPV)]
        self.assertEqual(sr.build([_ds(recs)], [_listed(f, summit=None)])[0], [])
        groups, _ = sr.build([_ds(recs)], [_listed(f, summit=None)],
                             include_all=True)
        self.assertEqual(groups[0][0], "not near a summit")

    def test_hours_select_daytime_intervals(self):
        f = 151_100_000.0
        night = _ds([_rec(f, 24 * SPV)], hour=2.0)
        day = _ds([_rec(f, 24 * SPV, 25, 960, runs=40, mean_sweeps=24)],
                  hour=12.0)
        groups, meta = sr.build([night, day], [_listed(f)], hours="08-18")
        self.assertEqual(meta["n_intervals"], 1)
        self.assertEqual(groups[0][1][0][1]["tier1"], "used")


class TestBounds(unittest.TestCase):

    def test_not_heard_bound_shrinks_with_looks(self):
        self.assertAlmostEqual(sr.not_heard_bound(1), 0.95)
        self.assertLess(sr.not_heard_bound(300), 0.011)

    def test_wilson_contains_the_estimate(self):
        lo, hi = sr.wilson(0.05, 100)
        self.assertLess(lo, 0.05)
        self.assertGreater(hi, 0.05)


if __name__ == "__main__":
    unittest.main()
