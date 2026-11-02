"""Per-band RF gain, the FM band's own baseline, and a control that can fail.

Measured 2026-11-01 on an RSPdx at CM87: the FM control band clipped at
2-3% with the land-mobile gain and stopped at 40 dB of RF reduction; the
device picks its LNA state for the frequency it is at when gain is SET;
and a 25th-percentile baseline sat 5-10 dB above the floor in a city FM
band. See out/lab/PLAN.md findings F2, F14, F15.
"""

import copy
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import BAND_PLANS, resolve_bands
from occupancy_survey.config import AppConfig
from occupancy_survey.dataset import read_dataset
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def _raw(**sdr):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["sdr"].update(sdr)
    return raw


class TestConfig(unittest.TestCase):

    def test_a_per_band_value_is_accepted(self):
        cfg = AppConfig.from_dict(_raw(band_rf_gain_reduction_db={"bcast_fm": 40}))
        self.assertEqual(cfg.sdr.band_rf_gain_reduction_db, {"bcast_fm": 40.0})

    def test_a_band_not_in_the_schedule_is_refused(self):
        """A typo would leave that band at the default while looking set."""
        with self.assertRaises(ValueError) as cm:
            AppConfig.from_dict(_raw(band_rf_gain_reduction_db={"bcast_fn": 40}))
        self.assertIn("bcast_fn", str(cm.exception))

    def test_a_negative_reduction_is_refused(self):
        with self.assertRaises(ValueError):
            AppConfig.from_dict(_raw(band_rf_gain_reduction_db={"bcast_fm": -5}))

    def test_a_receiver_that_cannot_apply_it_is_refused(self):
        """Recording a per-band gain the receiver never applied is worse
        than not having the option."""
        with self.assertRaises(ValueError) as cm:
            AppConfig.from_dict(_raw(backend="rtl_sdr",
                                     band_rf_gain_reduction_db={"bcast_fm": 40}))
        self.assertIn("SDRplay", str(cm.exception))


class _OrderBackend(SyntheticBackend):
    """Synthetic backend that logs tune and gain calls in order."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def tune(self, center_hz, span_hz):
        self.calls.append(("tune", center_hz))
        super().tune(center_hz, span_hz)

    def set_band_gain(self, band_name):
        self.calls.append(("gain", band_name))
        return {"actuated": True, "rf_reduction_db_requested":
                40.0 if band_name == "bcast_fm" else 20.0}


def _design(**kw):
    base = dict(integration_s=0.01, dwell_s=0.5, interval_s=600.0,
                usable_bandwidth_hz=20e6)
    base.update(kw)
    return SamplingDesign(**base)


class TestSurveyAppliesBandGain(unittest.TestCase):

    def _run(self, bands, emitters=(), cycles=2):
        design = _design()
        backend = _OrderBackend(list(emitters), integration_s=0.01,
                                sample_rate_hz=design.usable_bandwidth_hz)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(bands), backend,
                           SiteMetadata("s", "RSPdx", "discone"), design,
                           out_dir=d, seed=1).run(cycles=cycles)
            return backend, [read_dataset(p) for p in paths]

    def test_gain_is_set_after_the_tune_on_every_band_entry(self):
        backend, _ = self._run(["vhf_lmr_low", "bcast_fm"])
        for i, call in enumerate(backend.calls):
            if call[0] == "gain":
                self.assertEqual(backend.calls[i - 1][0], "tune",
                                 "gain must be set at the band's frequency, "
                                 "i.e. after the retune")
        entries = [c[1] for c in backend.calls if c[0] == "gain"]
        self.assertGreaterEqual(entries.count("bcast_fm"), 2,
                                "re-set on every visit, not once per run")

    def test_each_band_records_its_own_gain(self):
        _, dss = self._run(["vhf_lmr_low", "bcast_fm"])
        b = dss[0]["bands"]
        self.assertEqual(b["bcast_fm"]["front_end"]["gain"]
                         ["rf_reduction_db_requested"], 40.0)
        self.assertEqual(b["vhf_lmr_low"]["front_end"]["gain"]
                         ["rf_reduction_db_requested"], 20.0)

    def test_the_fm_band_records_its_own_baseline_percentile(self):
        _, dss = self._run(["vhf_lmr_low", "bcast_fm"])
        b = dss[0]["bands"]
        self.assertEqual(b["bcast_fm"]["baseline_percentile"], 5.0)
        self.assertEqual(b["vhf_lmr_low"]["baseline_percentile"], 25.0)

    def test_only_the_fm_band_overrides_the_percentile(self):
        for name, plan in BAND_PLANS.items():
            if name == "bcast_fm":
                self.assertEqual(plan.baseline_percentile, 5.0)
            else:
                self.assertIsNone(plan.baseline_percentile, name)


class TestControlNeedsEveryStation(unittest.TestCase):
    """The overnight run passed every hour on one good station."""

    def _check(self, emitters):
        design = _design()
        backend = SyntheticBackend(emitters, integration_s=0.01,
                                   sample_rate_hz=design.usable_bandwidth_hz)
        with tempfile.TemporaryDirectory() as d:
            paths = Survey(resolve_bands(["bcast_fm"]), backend,
                           SiteMetadata("s", "x", "y"), design, out_dir=d,
                           seed=2).run(cycles=6)
            return read_dataset(paths[0])["bands"]["bcast_fm"]["control_check"]

    def test_all_stations_continuous_passes(self):
        em = [SyntheticEmitter(f, 30.0, duty=1.0, bandwidth_hz=170_000.0)
              for f in (88_500_000.0, 89_300_000.0, 90_100_000.0)]
        c = self._check(em)
        self.assertEqual(c["verdict"], "pass")
        self.assertEqual(c["stations_at_floor"], c["station_candidates"])

    def test_one_good_station_among_intermittent_ones_fails(self):
        em = [SyntheticEmitter(88_500_000.0, 30.0, duty=1.0,
                               bandwidth_hz=170_000.0)]
        em += [SyntheticEmitter(f, 30.0, duty=0.4, period_sweeps=40,
                                bandwidth_hz=170_000.0)
               for f in (89_300_000.0, 90_100_000.0)]
        c = self._check(em)
        self.assertEqual(c["max_measured_occupancy"] >= 0.9, True,
                         "the old rule would have passed this")
        self.assertEqual(c["verdict"], "fail")
        self.assertEqual(len(c["intermittent_hz"]), 2)


class TestRspdxSetsGainAtTheBand(unittest.TestCase):
    """GnuRadioBackend.set_band_gain without a flowgraph."""

    def _backend(self, overrides):
        from occupancy_survey.backends.rspdx_gr import GnuRadioBackend

        class Src:
            def __init__(self):
                self.g = {"IF": -20.0, "RF": -24.0}

            def set_gain(self, v, name):
                # The device's table at UHF: request 20 lands on -19.
                self.g[name] = -19.0 if (name == "RF" and v == -20.0) else v

            def get_gain(self, name):
                return self.g[name]

            def get_gain_mode(self):
                return False

        class Sink:
            def reset(self):
                pass

        class Clip:
            def take(self):
                return 0.0

        class Cfg:
            device_type = "rspdx"
            if_gain_reduction_db = 20.0
            rf_gain_reduction_db = 20.0
            band_rf_gain_reduction_db = overrides

        b = object.__new__(GnuRadioBackend)
        b._source, b._sink, b._clip, b._config = Src(), Sink(), Clip(), Cfg()
        b._settle_s = 0.0
        b._center = 456e6
        b._gain_accepted = {"IF": -20.0, "RF": -24.0}
        b._gain_accepted_at = {162.475e6: dict(b._gain_accepted)}
        return b

    def test_the_override_is_applied_and_read_back(self):
        b = self._backend({"bcast_fm": 40.0})
        meta = b.set_band_gain("bcast_fm")
        self.assertTrue(meta["actuated"])
        self.assertTrue(meta["per_band_override"])
        self.assertEqual(meta["readback_db"]["RF"], -40.0)

    def test_the_readback_at_the_band_is_the_lock_reference(self):
        b = self._backend({})
        meta = b.set_band_gain("uhf_lmr")
        self.assertEqual(meta["readback_db"]["RF"], -19.0)
        g = b.gain_in_effect()
        self.assertTrue(g["locked"], "the value taken at this band is not drift")
        self.assertEqual(g["stages"]["RF"]["configured_db"], -20.0)


if __name__ == "__main__":
    unittest.main()
