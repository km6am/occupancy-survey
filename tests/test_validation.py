"""The QTH validation: the join, the floor, and what the figure may claim."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validate = _load("validate_occupancy")
qth = _load("qth_predict")


def _pred(freq_hz, proxy, continuity="intermittent", service="IG",
          role="target"):
    return {"freq_hz": freq_hz, "band": "vhf_lmr_low", "band_role": role,
            "owner": "owner", "service": service, "continuity": continuity,
            "expected_duty": None, "continuity_basis": "architectural",
            "distance_m": 5000.0, "erp_w": 100.0,
            "field_proxy_w_per_m2": proxy}


def _meas(freq_hz, occupancy, band="vhf_lmr_low", role="target", n=1000):
    return {"freq_hz": freq_hz, "band": band, "band_role": role,
            "occupancy": occupancy, "n_decided": n, "threshold_db": 10.0,
            "sites": ["s1"], "comparable": True}


class TestJoin(unittest.TestCase):

    def test_a_prediction_half_a_channel_off_still_matches(self):
        """An FCC frequency need not land on this tool's uniform grid."""
        prediction = {"channels": [_pred(151_006_000.0, 1e-5)]}
        measured = {151_000_000.0: _meas(151_000_000.0, 0.2)}
        rows, busy = validate.join(prediction, measured)
        self.assertTrue(rows[0]["matched"])
        self.assertEqual(rows[0]["occupancy"], 0.2)
        self.assertEqual(busy, [])

    def test_a_prediction_a_whole_channel_off_does_not_match(self):
        prediction = {"channels": [_pred(151_025_000.0, 1e-5)]}
        measured = {151_000_000.0: _meas(151_000_000.0, 0.2)}
        rows, busy = validate.join(prediction, measured)
        self.assertFalse(rows[0]["matched"])
        self.assertEqual(len(busy), 1,
                         "the measured channel is now unexplained and must "
                         "surface as busy-with-no-record")

    def test_busy_with_no_fcc_record_is_surfaced(self):
        prediction = {"channels": []}
        measured = {155_000_000.0: _meas(155_000_000.0, 0.44)}
        rows, busy = validate.join(prediction, measured)
        self.assertEqual(len(busy), 1)
        self.assertAlmostEqual(busy[0]["occupancy"], 0.44)

    def test_a_quiet_unpredicted_channel_is_not_surfaced(self):
        """Only BUSY unexplained channels are interesting."""
        prediction = {"channels": []}
        measured = {155_000_000.0: _meas(155_000_000.0, 0.0)}
        _, busy = validate.join(prediction, measured)
        self.assertEqual(busy, [])

    def test_control_channels_are_not_reported_as_unexplained(self):
        prediction = {"channels": []}
        measured = {162_475_000.0: _meas(162_475_000.0, 1.0, band="nwr",
                                         role="control")}
        _, busy = validate.join(prediction, measured)
        self.assertEqual(busy, [],
                         "a busy control channel is the expected result, "
                         "not an unexplained emitter")


class TestAudibilityFloor(unittest.TestCase):
    """What makes 'predicted but never active' mean anything.

    A real smoke test against FCC data returned 11,980 predicted channels
    for one location. Without a floor, the silent-but-predicted list is
    thousands of distant low-power licences and carries no information.
    """

    def test_the_floor_comes_from_the_weakest_control_actually_heard(self):
        prediction = {"channels": [
            _pred(88_500_000.0, 1e-3, "continuous", "FM", "control"),
            _pred(107_900_000.0, 4e-6, "continuous", "FM", "control"),
            _pred(151_000_000.0, 1e-5),
        ]}
        measured = {
            88_500_000.0: _meas(88_500_000.0, 1.0, "bcast_fm", "control"),
            107_900_000.0: _meas(107_900_000.0, 0.99, "bcast_fm", "control"),
            151_000_000.0: _meas(151_000_000.0, 0.0),
        }
        rows, _ = validate.join(prediction, measured)
        floor, n = validate.empirical_audibility_floor(rows)
        self.assertAlmostEqual(floor, 4e-6)
        self.assertEqual(n, 2)

    def test_a_control_that_was_not_heard_does_not_set_the_floor(self):
        prediction = {"channels": [
            _pred(88_500_000.0, 1e-3, "continuous", "FM", "control"),
            _pred(107_900_000.0, 4e-9, "continuous", "FM", "control"),
        ]}
        measured = {
            88_500_000.0: _meas(88_500_000.0, 1.0, "bcast_fm", "control"),
            107_900_000.0: _meas(107_900_000.0, 0.0, "bcast_fm", "control"),
        }
        rows, _ = validate.join(prediction, measured)
        floor, n = validate.empirical_audibility_floor(rows)
        self.assertAlmostEqual(floor, 1e-3)
        self.assertEqual(n, 1)

    def test_no_controls_means_no_floor_rather_than_a_guess(self):
        prediction = {"channels": [_pred(151_000_000.0, 1e-5)]}
        measured = {151_000_000.0: _meas(151_000_000.0, 0.2)}
        rows, _ = validate.join(prediction, measured)
        self.assertEqual(validate.empirical_audibility_floor(rows), (None, 0))

    def test_the_report_says_the_list_is_unfiltered_without_a_floor(self):
        prediction = {"channels": [_pred(151_000_000.0, 1e-9)]}
        measured = {151_000_000.0: _meas(151_000_000.0, 0.0)}
        rows, busy = validate.join(prediction, measured)
        text = validate.report(rows, busy, {}, _conditions(), 10.0)
        self.assertIn("UNFILTERED", text)

    def test_the_report_filters_and_states_the_floor(self):
        prediction = {"channels": [
            _pred(88_500_000.0, 1e-3, "continuous", "FM", "control"),
            _pred(151_000_000.0, 1e-9),       # far below the floor
            _pred(151_500_000.0, 1e-2),       # well above it, and silent
        ]}
        measured = {
            88_500_000.0: _meas(88_500_000.0, 1.0, "bcast_fm", "control"),
            151_000_000.0: _meas(151_000_000.0, 0.0),
            151_500_000.0: _meas(151_500_000.0, 0.0),
        }
        rows, busy = validate.join(prediction, measured)
        text = validate.report(rows, busy, {}, _conditions(), 10.0)
        self.assertIn("Empirical audibility floor", text)
        self.assertIn("151.5000", text,
                      "a strong predicted channel reading 0% must be listed")
        self.assertNotIn("151.0000", text,
                         "a channel far below the audibility floor must be "
                         "filtered out, or the list is noise")


def _conditions():
    return {"sites": ["s1"], "intervals": 1, "grid_square": "CM87",
            "first_utc": "2026-10-28T12:00:00+00:00",
            "last_utc": "2026-10-28T13:00:00+00:00",
            "baseline_percentile": 25.0, "dwell_s": 20.0,
            "integration_s": 0.1, "revisit_s": 287.0, "min_burst_s": 0.025,
            "receiver_model": "RSPdx", "antenna": "discone", "warnings": []}


class TestEvidentialHonesty(unittest.TestCase):

    def test_synthetic_datasets_are_refused(self):
        """Validating a model against simulated data would always agree."""
        from datetime import datetime, timedelta, timezone

        from occupancy_survey.backends.synthetic import SyntheticBackend
        from occupancy_survey.channels import resolve_bands
        from occupancy_survey.schedule import SamplingDesign
        from occupancy_survey.site import SiteMetadata
        from occupancy_survey.survey import Survey

        with tempfile.TemporaryDirectory() as d:
            paths = Survey(
                resolve_bands(["nwr"]),
                SyntheticBackend(integration_s=0.01),
                SiteMetadata("s", "synthetic", "none"),
                SamplingDesign(integration_s=0.01, dwell_s=1.0,
                               interval_s=30.0, usable_bandwidth_hz=2e6),
                out_dir=d).run(cycles=3)
            with self.assertRaises(SystemExit) as cm:
                validate.load_measured([str(p) for p in paths])
        self.assertIn("synthetic", str(cm.exception))

    def test_the_report_names_what_is_most_likely_wrong(self):
        rows, busy = validate.join({"channels": []}, {})
        text = validate.report(rows, busy, {}, _conditions(), 10.0)
        self.assertIn("WHICH STEP IS MOST LIKELY TO BE WRONG", text)
        self.assertIn("aliasing", text.lower())

    def test_the_report_flags_a_missing_control_band(self):
        rows, busy = validate.join({"channels": []}, {})
        text = validate.report(rows, busy, {}, _conditions(), 10.0)
        self.assertIn("Without a control band", text)

    def test_no_title_in_the_figure_claims_validation(self):
        """A plot title carries the same evidential weight as a sentence."""
        src = (REPO / "tools" / "validate_occupancy.py").read_text()
        titles = []
        for line in src.splitlines():
            if "set_title(" in line or "suptitle(" in line:
                titles.append(line)
        self.assertTrue(titles)
        banned = ("validat", "confirm", "proves", "proven", "verified",
                  "because", "caused", "due to")
        joined = " ".join(titles).lower()
        for word in banned:
            self.assertNotIn(
                word, joined,
                f"a figure title containing '{word}' would assert more than "
                f"the data shows")


class TestPlotRenders(unittest.TestCase):

    def test_the_figure_renders_with_both_panels(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib not installed")

        prediction = {"channels": [
            _pred(88_500_000.0, 1e-3, "continuous", "FM", "control"),
            _pred(151_000_000.0, 3e-5, "intermittent", "IG"),
            _pred(151_200_000.0, 2e-5, "continuous", "YG"),
            _pred(151_400_000.0, 9e-6, "intermittent", "PW"),
            _pred(151_600_000.0, 5e-3, "intermittent", "IG"),
        ]}
        measured = {
            88_500_000.0: _meas(88_500_000.0, 1.0, "bcast_fm", "control"),
            151_000_000.0: _meas(151_000_000.0, 0.21),
            151_200_000.0: _meas(151_200_000.0, 0.99),
            151_400_000.0: _meas(151_400_000.0, 0.12),
            151_600_000.0: _meas(151_600_000.0, 0.0),
        }
        rows, busy = validate.join(prediction, measured)
        with tempfile.TemporaryDirectory() as d:
            out = validate.plot(rows, busy, _conditions(), 10.0,
                                Path(d) / "fig.png")
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 20_000)


class TestPseudoSummit(unittest.TestCase):

    def test_the_pseudo_summit_is_shaped_as_sota_rf_expects(self):
        """sota-rf finds its header by startswith('SummitCode') and filters
        on the prefix before the slash."""
        with tempfile.TemporaryDirectory() as d:
            p = qth.write_pseudo_summit(Path(d) / "s.csv", 37.7, -122.4,
                                        alt_m=283.0)
            lines = p.read_text().splitlines()
        self.assertTrue(lines[0].startswith("SummitCode"))
        self.assertIn("QTH/SITE-001", lines[1])
        self.assertEqual(qth.PSEUDO_CODE.split("/")[0], qth.PSEUDO_ASSOCIATION)

    def test_multiple_frequencies_in_one_licence_become_separate_channels(self):
        from occupancy_survey.channels import resolve_bands
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "QTH_rf_sources.csv"
            csv_path.write_text(
                "distance_m,rf_owner,rf_freqs_mhz,rf_max_power_w,rf_services\n"
                "5000,Acme,151.0 151.2 999.0,100,IG\n")
            rows = qth.parse_emitters(csv_path,
                                      resolve_bands(["vhf_lmr_low"]))
        self.assertEqual(len(rows), 2,
                         "two of three frequencies are in band; the 999 MHz "
                         "one must be dropped")
        self.assertEqual({r["freq_hz"] for r in rows},
                         {151_000_000.0, 151_200_000.0})
        for r in rows:
            self.assertEqual(r["continuity"], "intermittent")
            self.assertAlmostEqual(r["field_proxy_w_per_m2"], 100 / 5000 ** 2)

    def test_a_trunked_service_code_is_classified_continuous(self):
        from occupancy_survey.channels import resolve_bands
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "c.csv"
            csv_path.write_text(
                "distance_m,rf_owner,rf_freqs_mhz,rf_max_power_w,rf_services\n"
                "5000,County,151.0,100,YW\n")
            rows = qth.parse_emitters(csv_path,
                                      resolve_bands(["vhf_lmr_low"]))
        self.assertEqual(rows[0]["continuity"], "continuous")
        self.assertEqual(rows[0]["expected_duty"], 1.0)


if __name__ == "__main__":
    unittest.main()
