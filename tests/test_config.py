"""Configuration: the coherence traps are closed, and silence is refused."""

import unittest

from occupancy_survey.config import AppConfig, SdrConfig

BASE = {
    "site": {"site_id": "s1", "receiver_model": "RSPdx", "antenna": "discone"},
    "sdr": {"sample_rate_hz": 2_048_000, "fft_size": 4096},
    "bands": ["nwr"],
}


def _cfg(**over):
    raw = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in BASE.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    return AppConfig.from_dict(raw)


class TestDerivedValues(unittest.TestCase):

    def test_usable_bandwidth_follows_the_sample_rate(self):
        """Closes the gap-between-tune-steps trap by construction."""
        cfg = _cfg(sdr={"sample_rate_hz": 2_048_000})
        self.assertEqual(cfg.sampling.usable_bandwidth_hz, 2_048_000)

        cfg = _cfg(sdr={"sample_rate_hz": 6_000_000})
        self.assertEqual(cfg.sampling.usable_bandwidth_hz, 6_000_000)

    def test_it_can_still_be_set_explicitly_narrower(self):
        cfg = _cfg(sampling={"usable_bandwidth_hz": 1_500_000})
        self.assertEqual(cfg.sampling.usable_bandwidth_hz, 1_500_000)

    def test_bandwidth_defaults_to_the_sample_rate(self):
        self.assertEqual(_cfg().sdr.bw_hz, 2_048_000)


class TestRefusals(unittest.TestCase):

    def test_agc_is_not_a_setting(self):
        with self.assertRaises(ValueError) as cm:
            _cfg(sdr={"agc_enabled": True})
        self.assertIn("not a setting", str(cm.exception))

    def test_agc_false_is_also_refused_as_a_key(self):
        """Even agc_enabled: false must be refused.

        Accepting it would imply the opposite is available, and somebody
        would eventually set it.
        """
        with self.assertRaises(ValueError):
            _cfg(sdr={"agc_enabled": False})

    def test_agc_cannot_be_set_on_the_dataclass_either(self):
        with self.assertRaises(TypeError):
            SdrConfig(agc_enabled=True)

    def test_a_misspelled_sdr_key_is_refused_not_ignored(self):
        """Silently ignoring it would record the default as if chosen."""
        with self.assertRaises(ValueError) as cm:
            _cfg(sdr={"if_gain_reducton_db": 20})
        self.assertIn("if_gain_reducton_db", str(cm.exception))

    def test_a_missing_site_block_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            AppConfig.from_dict({"sdr": {}, "bands": ["nwr"]})
        self.assertIn("site", str(cm.exception))

    def test_a_missing_antenna_is_refused(self):
        with self.assertRaises(ValueError):
            _cfg(site={"antenna": ""})

    def test_an_unknown_band_fails_before_acquisition_not_after(self):
        with self.assertRaises(ValueError) as cm:
            _cfg(bands=["nwr", "vhf_lmr_middle"])
        self.assertIn("vhf_lmr_middle", str(cm.exception))

    def test_a_too_precise_grid_square_is_refused(self):
        from occupancy_survey.site import LocationTooPrecise
        with self.assertRaises(LocationTooPrecise):
            _cfg(site={"grid_square": "CM87ux12"})

    def test_no_location_is_valid(self):
        cfg = _cfg()
        self.assertIsNone(cfg.site.grid_square)
        self.assertFalse(cfg.site.as_metadata()["location_shared"])


class TestCli(unittest.TestCase):

    def test_plan_touches_no_hardware_and_reports_the_bounds(self):
        import io
        import contextlib
        import tempfile
        from pathlib import Path
        import yaml
        from occupancy_survey.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.yaml"
            p.write_text(yaml.safe_dump(BASE))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--config", str(p), "--plan"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("blind spot", out)
        self.assertIn("catch probability", out)
        # The two per-band numbers most easily misread: the threshold is not
        # an SNR, and an empty channel does not read 0%.
        self.assertIn("SNR a continuous carrier needs", out)
        self.assertIn("noise floor", out)
        self.assertIn("bins/channel", out)

    def test_an_unknown_backend_is_an_error_not_a_default(self):
        import tempfile
        from pathlib import Path
        import yaml
        from occupancy_survey.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.yaml"
            p.write_text(yaml.safe_dump(BASE))
            rc = main(["--config", str(p), "--backend", "nonesuch",
                       "--cycles", "1"])
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
