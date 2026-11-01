"""The RF-step intermod check: absolute levels, A-B-A, measured step.

Every case here is something measured on an RSPdx at CM87 on 2026-11-01:
a receiver-limited floor that moved 0-0.7 dB for an 8 dB RF step, a UHF
readback 45% larger than the step signals saw, and spurs that did not
follow RF gain at all.
"""

import tempfile
import unittest

import numpy as np

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import resolve_bands
from occupancy_survey.intermod import (classify, classify_rf_step,
                                       summarise_rf_step)
from occupancy_survey.schedule import SamplingDesign, tune_steps
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey

FLOOR = -6.0
G = 8.3


def _case(kinds, g=G, floor_drop=0.7, readback=12.0):
    """kinds: list of (excess_dB, kind) with kind in real/im3/spur/stops."""
    freqs, a1, b, a2 = [], [], [], []
    for i, (ex, kind) in enumerate(kinds):
        lvl = FLOOR + ex
        drop = {"real": g, "im3": 3 * g, "spur": 0.4, "stops": 0.0}[kind]
        freqs.append(450e6 + i * 12_500)
        a1.append(lvl)
        a2.append(lvl - 15.0 if kind == "stops" else lvl)
        b.append(lvl - drop)
    return classify_rf_step(freqs, a1, b, a2, FLOOR, FLOOR - floor_drop,
                            FLOOR, readback_step_db=readback)


def _verdicts(res):
    return [r["verdict"] for r in res["channels"]]


class TestAbsoluteLevels(unittest.TestCase):

    def test_real_signals_read_order_one_on_a_receiver_limited_floor(self):
        """The case the excess form got wrong: floor moved 0.7 of 8.3 dB."""
        res = _case([(30, "real"), (35, "real"), (20, "real")])
        self.assertTrue(all(v == "real signal" for v in _verdicts(res)))
        # The old excess form on the same numbers calls them products.
        old = classify([1, 2, 3], [30, 35, 20],
                       [30 - G + 0.7, 35 - G + 0.7, 20 - G + 0.7], G)
        self.assertTrue(all(o.implied_order > 1.6 for o in old))

    def test_the_step_is_measured_not_read_back(self):
        """Readback said 12 dB; signals fell 8.3."""
        res = _case([(30, "real"), (35, "real"), (28, "real")])
        self.assertEqual(res["gain_step_source"], "measured")
        self.assertAlmostEqual(res["gain_step_db_used"], G, places=2)

    def test_a_third_order_product_is_found(self):
        res = _case([(30, "real"), (35, "real"), (40, "im3")])
        self.assertEqual(_verdicts(res)[2], "likely 3rd-order product")

    def test_a_weak_product_that_vanishes_is_still_caught(self):
        """A 30 dB IM3 falls 25 dB under an 8.3 dB step and sinks into the
        floor -- while a real 30 dB signal would sit 22 dB up at B."""
        res = _case([(30, "real"), (35, "real"), (30, "im3")])
        self.assertTrue(_verdicts(res)[2].startswith("vanished"))
        self.assertGreaterEqual(res["channels"][2]["order"], 2.0)

    def test_a_weak_real_signal_that_fades_is_not_called_a_product(self):
        """At 11 dB a real signal is near the floor at B anyway, so its
        disappearing proves nothing."""
        res = _case([(30, "real"), (35, "real"), (11, "real")])
        self.assertTrue(_verdicts(res)[2].startswith("too weak"))

    def test_a_spur_is_internal_not_a_product(self):
        """456.000 fell 0.8 dB for an 8.4 dB step and read the same with
        no antenna on the port."""
        res = _case([(30, "real"), (35, "real"), (17, "spur")])
        self.assertTrue(_verdicts(res)[2].startswith("internal"))

    def test_traffic_that_stops_is_unstable_not_a_product(self):
        res = _case([(30, "real"), (35, "real"), (30, "stops")])
        self.assertTrue(_verdicts(res)[2].startswith("unstable"))

    def test_a_channel_lost_in_the_floor_at_b_is_not_assessed(self):
        res = _case([(30, "real"), (35, "real"), (11, "real")])
        self.assertTrue(_verdicts(res)[2].startswith("too weak"))

    def test_falls_back_to_readback_and_says_so(self):
        res = _case([(30, "real"), (12, "real")])
        self.assertEqual(res["gain_step_source"], "readback")
        s = summarise_rf_step(res)
        self.assertIn("readback", s["advice"])


class TestSummary(unittest.TestCase):

    def test_clean_band_with_a_spur(self):
        s = summarise_rf_step(_case([(30, "real"), (35, "real"),
                                     (17, "spur")]))
        self.assertEqual(s["channels_suspect"], 0)
        self.assertEqual(s["channels_internal"], 1)
        self.assertIn("no evidence of front-end intermod", s["advice"])
        self.assertIn("spur", s["advice"])

    def test_products_are_counted_and_named(self):
        s = summarise_rf_step(_case([(30, "real"), (35, "real"),
                                     (30, "im3")]))
        self.assertEqual(s["channels_suspect"], 1)
        self.assertIn("front end", s["advice"])


class _RfStepBackend(SyntheticBackend):
    """Synthetic backend whose levels respond to an RF step like hardware.

    A real signal falls by the step, a product by three times it, a spur
    not at all, and the floor (receiver-generated) not at all.
    """

    def __init__(self, emitters_db, step_db, readback_db, **kw):
        super().__init__(**kw)
        self._em = emitters_db
        self._step, self._readback = step_db, readback_db
        self._off = 0.0

    def set_band_gain(self, name):
        return {"actuated": True}

    def set_gain_offset_db(self, offset_db):
        self._off = offset_db
        return True

    def achieved_gain_offset_db(self):
        return -self._readback if self._off else 0.0

    def sweeps(self, dwell_s):
        axis = self.freq_axis()
        rng = np.random.default_rng(0)
        n = max(1, int(round(dwell_s / self._integration_s)))
        reduced = self._off != 0.0
        for _ in range(n):
            spec = 10 * np.log10(rng.exponential(1.0, axis.size))
            for f, lvl, kind in self._em:
                drop = {"real": self._step, "im3": 3 * self._step,
                        "spur": 0.0}[kind] if reduced else 0.0
                i = int(np.argmin(np.abs(axis - f)))
                spec[i - 2:i + 3] = lvl - drop
            yield type("S", (), {"spectrum_db": spec, "t_unix": 0.0,
                                 "clip_fraction": 0.0})()


class TestSurveyRunsTheRfStep(unittest.TestCase):

    def test_end_to_end(self):
        design = SamplingDesign(integration_s=0.01, dwell_s=0.5,
                                usable_bandwidth_hz=2e6)
        band = resolve_bands(["vhf_lmr_low"])[0]
        step = tune_steps(band, design)[0]
        c = step.center_hz
        em = [(c - 400e3, 35.0, "real"), (c - 200e3, 40.0, "real"),
              (c + 100e3, 30.0, "im3"), (c + 300e3, 20.0, "spur")]
        backend = _RfStepBackend(em, step_db=6.0, readback_db=6.0,
                                 integration_s=0.01, sample_rate_hz=2e6)
        with tempfile.TemporaryDirectory() as d:
            s = Survey([band], backend, SiteMetadata("s", "x", "y"), design,
                       out_dir=d)
            out = s.intermod_check(band, step, sweeps=80, gain_step_db=10)
        self.assertEqual(out["gain_step_source"], "measured")
        self.assertAlmostEqual(out["gain_step_db_used"], 6.0, delta=0.5)
        v = {round(r["freq_hz"] - c): r["verdict"] for r in out["channels"]}
        self.assertEqual(v[-400_000], "real signal")
        self.assertEqual(v[100_000], "likely 3rd-order product")
        self.assertTrue(v[300_000].startswith("internal"))
        self.assertEqual(backend._off, 0.0, "gain must be put back")


if __name__ == "__main__":
    unittest.main()
