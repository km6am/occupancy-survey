"""SDRplay models beyond the RSPdx: RSPdx-R2, RSP1A, RSP1B.

The per-model differences come from gr-sdrplay3 3.11.0.8's own classes:
rsp1a and rsp1b have no set_antenna, get_antennas or set_hdr_mode, and
rspdxr2 has exactly the RSPdx's methods. A fake module stands in for
gnuradio.sdrplay3 here, built to refuse the calls the real class lacks, so
the factory is tested against the same absence that would break it on
hardware.
"""

import copy
import unittest
from pathlib import Path

import yaml

from occupancy_survey.backends.gr_sdr_sources import (_create_sdrplay_source,
                                                      create_source)
from occupancy_survey.backends.rspdx_gr import GnuRadioBackend
from occupancy_survey.backends.sdrplay_models import (SDRPLAY_MODELS,
                                                      sdrplay_model)
from occupancy_survey.config import AppConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"

_COMMON = ("set_sample_rate", "set_center_freq", "set_bandwidth",
           "set_gain_mode", "set_gain", "set_rf_notch_filter",
           "set_dab_notch_filter", "set_biasT", "set_dc_offset_mode",
           "set_iq_balance_mode")
_DX_ONLY = ("set_antenna", "set_hdr_mode")


def _fake_sdrplay3():
    """A module whose classes have only the methods the real ones have."""
    calls = []

    def make_class(block, methods):
        def method(name):
            return lambda self, *a: calls.append((block, name, a))
        attrs = {m: method(m) for m in methods}
        attrs["__init__"] = lambda self, sel, args: calls.append(
            (block, "__init__", (sel,)))
        return type(block, (), attrs)

    mod = type("sdrplay3", (), {})()
    mod.stream_args = lambda fmt, n: (fmt, n)
    for block in ("rspdx", "rspdxr2"):
        setattr(mod, block, make_class(block, _COMMON + _DX_ONLY))
    for block in ("rsp1a", "rsp1b"):
        setattr(mod, block, make_class(block, _COMMON))
    return mod, calls


class _Cfg:
    sample_rate_hz = 6e6
    center_freq_hz = 152e6
    bw_hz = 6e6
    if_gain_reduction_db = 20.0
    rf_gain_reduction_db = 20.0
    antenna_port = "Antenna A"
    hdr_mode = False

    def __init__(self, device_type):
        self.device_type = device_type


class TestFactory(unittest.TestCase):

    def _open(self, device_type):
        mod, calls = _fake_sdrplay3()
        _create_sdrplay_source(_Cfg(device_type), sdrplay3=mod)
        return calls

    def test_each_model_opens_its_own_block(self):
        for device_type, model in SDRPLAY_MODELS.items():
            calls = self._open(device_type)
            self.assertEqual(calls[0][:2], (model.block, "__init__"),
                             device_type)

    def test_the_rsp1a_and_rsp1b_get_no_antenna_or_hdr_call(self):
        """The real classes have neither setter; calling one raises
        AttributeError from inside device setup."""
        for device_type in ("rsp1a", "rsp1b"):
            names = {c[1] for c in self._open(device_type)}
            self.assertFalse(names & set(_DX_ONLY), device_type)
            self.assertIn("set_rf_notch_filter", names)

    def test_the_dx_family_sets_its_port_and_hdr(self):
        for device_type in ("rspdx", "rspdxr2"):
            calls = self._open(device_type)
            self.assertIn((device_type, "set_antenna", ("Antenna A",)),
                          calls)
            self.assertIn((device_type, "set_hdr_mode", (False,)), calls)

    def test_gain_is_still_a_reduction_on_every_model(self):
        for device_type in SDRPLAY_MODELS:
            calls = self._open(device_type)
            self.assertIn((device_type, "set_gain", (-20.0, "RF")), calls)
            self.assertIn((device_type, "set_gain_mode", (False,)), calls)

    def test_unlisted_sdrplay_models_are_refused_by_name(self):
        """The RSP1 has no FM notch; the RSP2 and RSPduo have untested port
        and tuner semantics. None is reachable by accident."""
        for device_type in ("rsp1", "rsp2", "rspduo"):
            self.assertIsNone(sdrplay_model(device_type))
            with self.assertRaises(ValueError) as cm:
                create_source(_Cfg(device_type))
            self.assertIn("Unknown SDR device type", str(cm.exception))


def _raw(**sdr):
    raw = copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    raw["sdr"].update({"backend": "gnuradio"})
    raw["sdr"].update(sdr)
    return raw


class TestConfig(unittest.TestCase):

    def test_per_band_gain_is_accepted_on_every_model(self):
        for device_type in SDRPLAY_MODELS:
            raw = _raw(device_type=device_type,
                       band_rf_gain_reduction_db={"bcast_fm": 50})
            if not sdrplay_model(device_type).has_antenna_switch:
                raw["sdr"].pop("antenna_port", None)
            cfg = AppConfig.from_dict(raw)
            self.assertEqual(cfg.sdr.band_rf_gain_reduction_db,
                             {"bcast_fm": 50.0}, device_type)

    def test_a_port_on_a_single_input_model_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            AppConfig.from_dict(_raw(device_type="rsp1a",
                                     antenna_port="Antenna B"))
        self.assertIn("one antenna input", str(cm.exception))

    def test_the_default_port_name_is_cleared_on_a_single_input_model(self):
        """SdrConfig's default names an RSPdx port; on an RSP1A it would
        be recorded in every dataset as a switch that does not exist."""
        raw = _raw(device_type="rsp1b")
        raw["sdr"].pop("antenna_port", None)
        self.assertEqual(AppConfig.from_dict(raw).sdr.antenna_port, "")

    def test_a_misspelled_dx_port_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            AppConfig.from_dict(_raw(device_type="rspdxr2",
                                     antenna_port="Antenna D"))
        self.assertIn("Antenna A", str(cm.exception))

    def test_hdr_on_a_model_without_it_is_refused(self):
        raw = _raw(device_type="rsp1a", hdr_mode=True)
        raw["sdr"].pop("antenna_port", None)
        with self.assertRaises(ValueError) as cm:
            AppConfig.from_dict(raw)
        self.assertIn("HDR", str(cm.exception))

    def test_a_synthetic_run_is_not_held_to_the_hardware(self):
        """The demo config keeps the default device_type; checking a port
        against hardware it never opens would only break the demo."""
        cfg = AppConfig.from_dict(_raw(backend="synthetic",
                                       device_type="rsp1a",
                                       antenna_port="Antenna B"))
        self.assertEqual(cfg.sdr.antenna_port, "Antenna B")


class TestBackend(unittest.TestCase):

    def _backend(self, device_type, port=""):
        class Cfg:
            if_gain_reduction_db = 20.0
            rf_gain_reduction_db = 20.0
            band_rf_gain_reduction_db = {}
            hdr_mode = False
            antenna_port = port

        class Sink:
            dropped = 0

        class Watch:
            def as_metadata(self):
                return {}

        b = object.__new__(GnuRadioBackend)
        b._config = Cfg()
        b._config.device_type = device_type
        b._sink, b._overload, b._running = Sink(), Watch(), False
        b._sample_rate, b._fft_size, b._integration_s = 6e6, 8192, 0.1
        b._actual_integration_s, b._frames_per_integration = 0.1, 73
        return b

    def test_metadata_records_the_model_and_its_gain_convention(self):
        meta = self._backend("rsp1a").receiver_metadata()
        self.assertEqual(meta["model"], "RSP1A")
        self.assertIn("REDUCTION", meta["gain_units"])
        self.assertIsNone(meta["antenna_port"])
        self.assertNotIn("hdr_mode", meta)

    def test_the_dx_r2_records_port_and_hdr(self):
        meta = self._backend("rspdxr2", "Antenna C").receiver_metadata()
        self.assertEqual(meta["model"], "RSPdx-R2")
        self.assertEqual(meta["antenna_port"], "Antenna C")
        self.assertIn("hdr_mode", meta)

    def test_switching_ports_on_a_single_input_model_says_why_it_cannot(self):
        with self.assertRaises(ValueError) as cm:
            self._backend("rsp1b").set_antenna_port("Antenna B")
        self.assertIn("RSP1B", str(cm.exception))

    def test_per_band_gain_is_actuated_on_the_family(self):
        """set_band_gain used to return early for anything but 'rspdx'."""
        for device_type in ("rspdxr2", "rsp1a", "rsp1b"):
            b = self._backend(device_type)
            calls = []

            class Src:
                def set_gain(self, v, stage):
                    calls.append((stage, v))

                def get_gain(self, stage):
                    return -20.0

            class Sink:
                def reset(self):
                    pass

            class Clip:
                def take(self):
                    return 0.0

            b._source, b._sink, b._clip = Src(), Sink(), Clip()
            b._settle_s, b._center = 0.0, 98e6
            b._gain_accepted, b._gain_accepted_at = {}, {}
            b._config.band_rf_gain_reduction_db = {"bcast_fm": 50.0}
            meta = b.set_band_gain("bcast_fm")
            self.assertTrue(meta["actuated"], device_type)
            self.assertIn(("RF", -50.0), calls, device_type)


if __name__ == "__main__":
    unittest.main()
