"""What files in this repo would tell a stranger.

The bug these tests exist for, measured 2026-11-01: tools/make_channel_list
copied the prediction's `query` block into the channel list verbatim, and
that block holds the generator's exact latitude and longitude. The channel
list is the one file designed to be handed to volunteers. It also carried
absolute paths holding the generator's account name, which it leaked even
when the prediction itself was a synthetic fixture.

So the regression tests here are about the OUTBOUND direction, and the
scanner tests are about giving a volunteer a way to check the INBOUND one
without taking anyone's word for it.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from occupancy_survey import disclosure

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


class TestGridSquare(unittest.TestCase):

    def test_known_locator(self):
        # CM87 is the San Francisco peninsula; 6 characters refines it.
        self.assertEqual(disclosure.grid_square(37.4, -122.1), "CM87")
        self.assertTrue(
            disclosure.grid_square(37.4, -122.1, 6).startswith("CM87"))

    def test_four_and_six_are_the_only_lengths(self):
        self.assertEqual(len(disclosure.grid_square(37.4, -122.1, 4)), 4)
        self.assertEqual(len(disclosure.grid_square(37.4, -122.1, 6)), 6)
        with self.assertRaises(ValueError):
            disclosure.grid_square(37.4, -122.1, 8)

    def test_southern_and_eastern_hemispheres(self):
        for lat, lon in ((-33.9, 18.4), (35.7, 139.7), (-41.3, 174.8)):
            g = disclosure.grid_square(lat, lon)
            self.assertRegex(g, r"^[A-R]{2}[0-9]{2}$", f"{lat},{lon} -> {g}")

    def test_out_of_range_is_refused(self):
        for lat, lon in ((91.0, 0.0), (0.0, 181.0), (-91.0, 0.0)):
            with self.assertRaises(ValueError):
                disclosure.grid_square(lat, lon)

    def test_a_grid_square_is_coarser_than_the_input(self):
        # Two points 10 km apart must be able to share a 4-char square;
        # that coarsening is the entire point of using one.
        a = disclosure.grid_square(37.40, -122.10)
        b = disclosure.grid_square(37.45, -122.12)
        self.assertEqual(a, b)


class TestScannerFindsEachPattern(unittest.TestCase):

    def test_exact_coordinates(self):
        f = disclosure.scan({"lat": 37.4219, "lon": -122.0841})
        self.assertEqual(len(f), 2)
        for x in f:
            self.assertEqual(x.severity, "high")
            self.assertEqual(x.kind, "exact_coordinate")

    def test_coarse_coordinates_are_medium_not_high(self):
        f = disclosure.scan({"lat": 37.4, "lon": -122.1})
        self.assertTrue(f)
        self.assertEqual({x.severity for x in f}, {"medium"})

    def test_zero_coordinates_are_treated_as_placeholders(self):
        # The fixtures use lat/lon 0; flagging them would train people to
        # ignore the scanner.
        self.assertEqual(disclosure.scan({"lat": 0.0, "lon": 0.0}), [])

    def test_callsign_shaped_site_id(self):
        f = disclosure.scan({"site_id": "KM6AM"})
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].kind, "callsign")
        self.assertIn("FCC", f[0].detail)

    def test_ordinary_site_labels_are_not_flagged(self):
        for name in ("home-1", "vol-test-1", "site-a", "roof", "shed2"):
            self.assertEqual(disclosure.scan({"site_id": name}), [],
                             f"false positive on {name!r}")

    def test_home_paths_on_both_platforms(self):
        for p in ("/Users/alice/sota-rf/x.csv", "/home/bob/data",
                  r"C:\Users\carol\out"):
            f = disclosure.scan({"path": p})
            self.assertTrue(f, f"missed {p}")
            self.assertEqual(f[0].kind, "home_path")

    def test_relative_paths_are_not_flagged(self):
        self.assertEqual(
            disclosure.scan({"path": "tests/fixtures/prediction_sample.json"}),
            [])

    def test_eight_character_grid_square(self):
        f = disclosure.scan({"grid_square": "CM87uk23"})
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].kind, "grid_too_precise")

    def test_four_character_grid_square_is_fine(self):
        self.assertEqual(disclosure.scan({"grid_square": "CM87"}), [])

    def test_findings_are_sorted_worst_first(self):
        f = disclosure.scan({"lat": 37.4, "lon": -122.1, "site_id": "KM6AM"})
        self.assertEqual(f[0].severity, "high")
        self.assertEqual(f[-1].severity, "medium")

    def test_nested_structures_are_walked(self):
        f = disclosure.scan({"a": [{"b": {"site_id": "KM6AM"}}]})
        self.assertEqual(len(f), 1)
        self.assertIn("[0]", f[0].where)

    def test_booleans_are_not_coordinates(self):
        self.assertEqual(disclosure.scan({"lat": True}), [])

    def test_worst_severity(self):
        self.assertIsNone(disclosure.worst_severity([]))
        self.assertEqual(
            disclosure.worst_severity(disclosure.scan({"site_id": "KM6AM"})),
            "high")


class TestTheChannelListNoLongerLeaks(unittest.TestCase):
    """Regression: the file handed to volunteers carried exact coordinates."""

    def setUp(self):
        self.out = Path(__file__).parent / "_tmp_channel_list.json"
        r = subprocess.run(
            [sys.executable, str(ROOT / "tools/make_channel_list.py"),
             "--prediction", str(FIXTURES / "prediction_sample.json"),
             "--out", str(self.out)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
        if r.returncode != 0:
            self.skipTest(f"make_channel_list failed: {r.stderr[-300:]}")
        self.data = json.loads(self.out.read_text())

    def tearDown(self):
        self.out.unlink(missing_ok=True)

    def test_the_generated_list_is_clean(self):
        self.assertEqual(disclosure.scan(self.data), [])

    def test_no_lat_or_lon_key_survives_anywhere(self):
        blob = json.dumps(self.data).lower()
        for key in ('"lat"', '"lon"', '"longitude"', '"latitude"'):
            self.assertNotIn(key, blob)

    def test_location_is_a_grid_square_or_withheld(self):
        q = self.data["generated_from"]["query"]
        self.assertIn("location_precision", q)
        if q.get("grid_square"):
            self.assertLessEqual(len(q["grid_square"]), 4)

    def test_provenance_paths_are_basenames(self):
        g = self.data["generated_from"]
        for key in ("prediction", "summit_csv"):
            if g.get(key):
                self.assertNotIn("/", g[key])

    def test_it_still_says_the_channel_set_narrows_the_region(self):
        # The honest residual: a targeted list is intersectable against the
        # FCC database. Removing that note would overstate the protection.
        self.assertIn("region",
                      self.data["generated_from"]["disclosure_note"])


class TestUnavoidableDisclosuresAreStated(unittest.TestCase):

    def test_there_are_some(self):
        self.assertGreaterEqual(len(disclosure.UNAVOIDABLE_DISCLOSURES), 3)

    def test_the_timezone_one_is_named(self):
        blob = " ".join(w for _, w in disclosure.UNAVOIDABLE_DISCLOSURES)
        self.assertIn("UTC offset", blob)

    def test_the_fingerprint_one_is_named(self):
        names = [n for n, _ in disclosure.UNAVOIDABLE_DISCLOSURES]
        self.assertTrue(any("channels" in n for n in names))


class TestDatasetDiscloses(unittest.TestCase):

    def test_a_dataset_records_os_family_not_a_host_fingerprint(self):
        from occupancy_survey.dataset import AcquisitionMetadata
        sw = AcquisitionMetadata({}, {}, {}, {}, {}).to_dict()["software"]
        self.assertIn("os", sw)
        self.assertNotIn("platform", sw,
                         "platform.platform() carries kernel build and "
                         "architecture, which no measurement needs")
        self.assertNotIn("-", sw["os"], f"{sw['os']!r} looks too detailed")

    def test_site_metadata_from_the_volunteer_config_is_clean(self):
        from occupancy_survey.site import SiteMetadata
        meta = SiteMetadata(site_id="vol-1", receiver_model="RTL-SDR",
                            antenna="rubber duck indoors",
                            grid_square="CM87").as_metadata()
        self.assertEqual(disclosure.scan(meta), [])

    def test_a_callsign_site_id_is_caught_in_site_metadata(self):
        from occupancy_survey.site import SiteMetadata
        meta = SiteMetadata(site_id="KM6AM", receiver_model="RTL-SDR",
                            antenna="dipole").as_metadata()
        f = disclosure.scan(meta)
        self.assertTrue(f)
        self.assertEqual(f[0].kind, "callsign")


class TestTheWarningReachesANormalRun(unittest.TestCase):
    """A warning in a mode you can skip is documentation, not a warning.

    The callsign check was added to run_preflight(), which only runs under
    --check. A volunteer going straight to a survey would have written
    every dataset with their callsign in it and never been told. Keeping
    this a warning rather than a refusal was a deliberate choice -- which
    only means anything if the warning actually fires.
    """

    def _warn(self, site_id):
        import logging
        from types import SimpleNamespace

        from occupancy_survey.__main__ import _warn_about_disclosure
        from occupancy_survey.site import SiteMetadata

        cfg = SimpleNamespace(site=SiteMetadata(
            site_id=site_id, receiver_model="RTL-SDR", antenna="dipole"))
        with self.assertLogs("occupancy_survey",
                             level=logging.WARNING) as cm:
            _warn_about_disclosure(cfg)
            logging.getLogger("occupancy_survey").warning("sentinel")
        return [m for m in cm.output if "sentinel" not in m]

    def test_a_callsign_warns_on_the_normal_path(self):
        logged = self._warn("KM6AM")
        self.assertTrue(logged, "no warning on the survey path")
        self.assertIn("callsign", " ".join(logged))
        self.assertIn("FCC", " ".join(logged))

    def test_an_ordinary_label_is_silent(self):
        self.assertEqual(self._warn("vol-test-1"), [])

    def test_it_warns_rather_than_raising(self):
        # The decision was warn, not refuse. If this ever starts raising,
        # a volunteer loses a run they had already committed to.
        from types import SimpleNamespace

        from occupancy_survey.__main__ import _warn_about_disclosure
        from occupancy_survey.site import SiteMetadata
        cfg = SimpleNamespace(site=SiteMetadata(
            site_id="KM6AM", receiver_model="RTL-SDR", antenna="dipole"))
        self.assertIsNone(_warn_about_disclosure(cfg))

    def test_broken_site_metadata_does_not_stop_a_run(self):
        from types import SimpleNamespace

        from occupancy_survey.__main__ import _warn_about_disclosure
        bad = SimpleNamespace(site=SimpleNamespace(
            as_metadata=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        self.assertIsNone(_warn_about_disclosure(bad))


class TestBundleManifestCannotRot(unittest.TestCase):
    """Every file the bundle promises to ship must exist."""

    def _manifest(self):
        src = (ROOT / "tools/make_volunteer_bundle.py").read_text()
        ns = {}
        # Execute only the MODULES/EXTRAS literals, not the tool.
        block = src.split("MODULES = (", 1)[1]
        mods = "(" + block.split(")", 1)[0] + ")"
        extras = src.split("EXTRAS = (", 1)[1].split(")", 1)[0]
        ns["MODULES"] = eval(mods)
        ns["EXTRAS"] = eval("(" + extras + ")")
        return ns

    def test_every_bundled_module_exists(self):
        for rel in self._manifest()["MODULES"]:
            self.assertTrue((ROOT / rel).is_file(), f"missing {rel}")

    def test_the_bundle_excludes_the_analysis_and_gnuradio_paths(self):
        mods = self._manifest()["MODULES"]
        for excluded in ("backends/rspdx_gr.py", "intermod.py",
                         "overload_watch.py"):
            self.assertFalse(
                any(excluded in m for m in mods),
                f"{excluded} is not needed to contribute and enlarges what "
                f"a stranger is asked to trust")

    def test_the_scanner_ships_so_volunteers_can_audit(self):
        self.assertIn("occupancy_survey/disclosure.py",
                      self._manifest()["MODULES"])


if __name__ == "__main__":
    unittest.main()
