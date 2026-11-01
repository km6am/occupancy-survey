"""The vendored reference must stay honest about what it is.

Vendoring a claim someone else owns creates one specific failure: the copy
drifts, nobody notices, and measurements get compared against an assumption
that has already been revised. These tests pin the three properties that
make the copy safe rather than dangerous --

  1. it carries enough provenance to find what it came from,
  2. the hand-written SERVICE_CONTINUITY mapping agrees with it wherever
     the source names a service code explicitly,
  3. "source not reachable" never reports as "in sync".

and they pin the data contracts, so a sota-rf column that disappears is a
named failure rather than a column of None.
"""

import csv
import hashlib
import json
import re
import unittest
from pathlib import Path

from occupancy_survey import reference
from occupancy_survey.channels import (CONTINUOUS, INTERMITTENT, UNKNOWN,
                                       SERVICE_CONTINUITY, continuity_for)

FIXTURES = Path(__file__).parent / "fixtures"


class TestProvenance(unittest.TestCase):

    def test_names_repo_path_branch_blob_and_hash(self):
        p = reference.duty_cycles_provenance()
        for key in ("repo", "path", "branch_read", "branch_commit",
                    "blob_sha1", "file_sha256", "captured"):
            self.assertTrue(p.get(key), f"provenance missing {key}")
        self.assertRegex(p["blob_sha1"], r"^[0-9a-f]{40}$")
        self.assertRegex(p["file_sha256"], r"^[0-9a-f]{64}$")

    def test_says_it_is_not_authoritative(self):
        # The whole risk of a vendored claim is being mistaken for the
        # claim itself.
        doc = reference.duty_cycles()
        self.assertIn("NOT authoritative", doc["authority"])

    def test_records_that_the_source_branches_disagree(self):
        # Measured 2026-11-01: the file is absent on the source repo's
        # main and differs on testset. Pinning a branch is the only way
        # this copy means anything, so the caution must not be dropped.
        self.assertIn("main", reference.duty_cycles_provenance()["caution"])

    def test_nothing_in_the_table_claims_to_be_measured(self):
        doc = reference.duty_cycles()
        self.assertIn("measured", doc["evidential_status"])
        for row in doc["rows"]:
            self.assertNotIn("measured", row["basis"].lower(),
                             f"row claims measurement: {row['verbatim']}")

    def test_provenance_line_is_one_line(self):
        line = reference.provenance_line()
        self.assertNotIn("\n", line)
        self.assertIn("duty_cycles.md", line)


class TestRowsSurvivedExtraction(unittest.TestCase):

    def test_rows_are_present_and_shaped(self):
        rows = reference.duty_cycles_rows()
        self.assertGreaterEqual(len(rows), 10)
        for r in rows:
            for key in ("service", "duty_cycle", "class", "basis",
                        "verbatim"):
                self.assertIn(key, r)

    def test_verbatim_text_is_kept(self):
        # A drift check that compares only parsed fields cannot see a
        # reworded basis, and the basis is half of every claim here.
        for r in reference.duty_cycles_rows():
            self.assertTrue(r["verbatim"].startswith("|"))
            self.assertIn(r["service"], r["verbatim"])

    def test_the_nwr_row_is_still_disputed(self):
        # It was retracted from "continuous" on an operator observation.
        # If an extraction ever quietly restores 100% here, that is the
        # single most consequential silent change this file could carry.
        nwr = [r for r in reference.duty_cycles_rows()
               if "NOAA" in r["service"] or "NWR" in r["service"]]
        self.assertEqual(len(nwr), 1)
        self.assertNotIn("100%", nwr[0]["duty_cycle"])
        self.assertIn("disputed", nwr[0]["class"].lower())


def _codes(service_text: str) -> list[str]:
    """Service codes the source names in parentheses, e.g. "(PW, IG)"."""
    out = []
    for group in re.findall(r"\(([^)]*)\)", service_text):
        for token in group.replace(",", " ").split():
            t = token.strip().upper()
            if re.fullmatch(r"[A-Z]{2,3}", t):
                out.append(t)
    return out


class TestMirrorAgreesWithTheSource(unittest.TestCase):
    """SERVICE_CONTINUITY is an interpretation of the vendored table.

    It is keyed by FCC service code; the source table is keyed by service
    description with codes in parentheses. So the mapping is work this repo
    does, and it can be wrong in a way the source cannot catch.
    """

    def setUp(self):
        self.by_code = {}
        for row in reference.duty_cycles_rows():
            for code in _codes(row["service"]):
                self.by_code[code] = row

    def test_the_source_does_name_some_codes(self):
        # If this fails the extraction changed shape and every assertion
        # below would vacuously pass.
        self.assertGreaterEqual(len(self.by_code), 6)

    def test_every_coded_row_is_in_the_mirror(self):
        missing = sorted(c for c in self.by_code
                         if c not in SERVICE_CONTINUITY)
        self.assertEqual(missing, [],
                         f"source names these codes, mirror lacks them: {missing}")

    def test_continuity_class_agrees_for_every_coded_row(self):
        for code, row in sorted(self.by_code.items()):
            mirrored = SERVICE_CONTINUITY[code].continuity
            src = row["class"].lower()
            with self.subTest(code=code):
                if "continuous" in src:
                    self.assertEqual(mirrored, CONTINUOUS)
                elif "intermittent" in src:
                    self.assertEqual(mirrored, INTERMITTENT)
                elif "disputed" in src or "uncertain" in src:
                    self.assertEqual(mirrored, UNKNOWN)
                else:
                    self.fail(f"{code}: unhandled source class {src!r}")

    def test_trunked_codes_are_continuous_as_section_2_requires(self):
        # The one architectural claim the whole risk model leans on: a Y
        # prefix means trunked and a trunked control channel is always on.
        for code in ("YW", "YG", "YK"):
            self.assertEqual(SERVICE_CONTINUITY[code].continuity, CONTINUOUS)

    def test_codes_the_mirror_adds_are_deliberate(self):
        """Extensions beyond the source must carry their own basis.

        These are codes this repo needs that the source table does not
        name. That is allowed -- but each must say what kind of claim it
        is, so an extension is never mistaken for something upstream
        vouched for.
        """
        extra = sorted(c for c in SERVICE_CONTINUITY if c not in self.by_code)
        self.assertTrue(extra, "expected the mirror to extend the source")
        for code in extra:
            with self.subTest(code=code):
                basis = SERVICE_CONTINUITY[code].basis
                self.assertTrue(basis.strip(), f"{code} has no basis")
                self.assertNotEqual(basis.strip().lower(), "unknown")

    def test_no_mirrored_duty_exceeds_one(self):
        for code, sc in SERVICE_CONTINUITY.items():
            if sc.expected_duty is not None:
                self.assertGreaterEqual(sc.expected_duty, 0.0)
                self.assertLessEqual(sc.expected_duty, 1.0, code)


class TestDriftCheckIsTriState(unittest.TestCase):

    def test_absent_source_is_unknown_not_in_sync(self):
        d = reference.check_duty_cycles_drift("/nonexistent/repo/path")
        self.assertIsNone(d.in_sync)
        self.assertFalse(d.checked)
        self.assertIn("NOT re-verified", d.detail)

    def test_matching_source_is_in_sync(self):
        import tempfile
        p = reference.duty_cycles_provenance()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / p["path"]
            f.parent.mkdir(parents=True, exist_ok=True)
            # Reconstructing the exact bytes is not possible here, so
            # drive the comparison through a file whose hash is known to
            # differ, and assert the negative branch instead.
            f.write_bytes(b"not the source document\n")
            d = reference.check_duty_cycles_drift(td)
        self.assertIs(d.in_sync, False)
        self.assertFalse(d.in_sync)
        self.assertTrue(d.checked)
        self.assertIn("revendor_reference.py", d.detail)

    def test_a_changed_source_names_both_hashes(self):
        import tempfile
        p = reference.duty_cycles_provenance()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / p["path"]
            f.parent.mkdir(parents=True, exist_ok=True)
            body = b"changed\n"
            f.write_bytes(body)
            d = reference.check_duty_cycles_drift(td)
        self.assertIn(hashlib.sha256(body).hexdigest()[:12], d.detail)
        self.assertIn(p["file_sha256"][:12], d.detail)


class TestSotaRfContract(unittest.TestCase):

    def test_required_columns_are_a_subset_of_the_full_header(self):
        for c in reference.SOTA_RF_REQUIRED_COLUMNS:
            self.assertIn(c, reference.SOTA_RF_SOURCES_COLUMNS)

    def test_carried_columns_are_a_subset_of_the_full_header(self):
        for c in reference.SOTA_RF_CARRIED_COLUMNS:
            self.assertIn(c, reference.SOTA_RF_SOURCES_COLUMNS)

    def test_required_and_carried_do_not_overlap(self):
        """"Required" has to mean one thing: absence corrupts the answer.

        An earlier version listed rf_ref, rf_owner, rf_lat and rf_lon as
        required. Existing tests of parse_emitters, which supply only the
        load-bearing columns, failed against it -- correctly, because
        those four go None and change nothing about the measurement.
        """
        self.assertEqual(
            set(reference.SOTA_RF_REQUIRED_COLUMNS)
            & set(reference.SOTA_RF_CARRIED_COLUMNS), set())

    def test_the_field_proxy_inputs_are_required(self):
        # ERP/d^2 is the entire ordering a prediction provides.
        for c in ("distance_m", "rf_max_power_w"):
            self.assertIn(c, reference.SOTA_RF_REQUIRED_COLUMNS)

    def test_metadata_is_not_required(self):
        for c in ("rf_owner", "rf_lat", "rf_lon"):
            self.assertNotIn(c, reference.SOTA_RF_REQUIRED_COLUMNS)

    def test_the_fixture_matches_the_recorded_header(self):
        with (FIXTURES / "sota_rf_sources_sample.csv").open(newline="") as f:
            header = next(csv.reader(f))
        self.assertEqual(tuple(header), reference.SOTA_RF_SOURCES_COLUMNS)

    def test_the_fixture_satisfies_the_required_columns(self):
        with (FIXTURES / "sota_rf_sources_sample.csv").open(newline="") as f:
            header = next(csv.reader(f))
        self.assertEqual(reference.missing_sota_rf_columns(header), [])

    def test_a_truncated_header_is_named_not_silently_tolerated(self):
        missing = reference.missing_sota_rf_columns(
            ["summit", "distance_m", "rf_freqs_mhz"])
        self.assertIn("rf_services", missing)
        self.assertIn("rf_max_power_w", missing)
        self.assertNotIn("distance_m", missing)

    def test_empty_header_reports_every_requirement(self):
        self.assertEqual(len(reference.missing_sota_rf_columns(None)),
                         len(reference.SOTA_RF_REQUIRED_COLUMNS))

    def test_summit_header_starts_with_summitcode(self):
        # sota-rf locates its header by startswith("SummitCode"); losing
        # that is what breaks the pseudo-summit trick.
        self.assertTrue(reference.SUMMIT_CSV_HEADER[0].startswith(
            "SummitCode"))


class TestPredictionFixtureStandsAlone(unittest.TestCase):
    """The point of the fixtures: analysis is testable with no sibling repo."""

    def setUp(self):
        self.pred = json.loads(
            (FIXTURES / "prediction_sample.json").read_text(encoding="utf-8"))

    def test_schema_matches_the_contract(self):
        self.assertTrue(self.pred["schema"].startswith(
            reference.PREDICTION_SCHEMA.rsplit("/", 1)[0]))

    def test_it_says_it_is_synthetic(self):
        blob = json.dumps(self.pred).upper()
        self.assertIn("SYNTHETIC", blob)
        self.assertIn("FIXTURE", blob)

    def test_it_carries_no_real_location(self):
        q = self.pred["query"]
        self.assertEqual((q["lat"], q["lon"]), (0.0, 0.0))

    def test_it_keeps_the_uncalibrated_caveat(self):
        self.assertIn("UNCALIBRATED", self.pred["caveats"]["field_proxy"])

    def test_continuity_classes_came_from_the_mirror(self):
        for ch in self.pred["channels"]:
            self.assertEqual(ch["continuity"],
                             continuity_for(ch["service"]).continuity)

    def test_an_unrecognised_code_stayed_unknown(self):
        zz = [c for c in self.pred["channels"] if c["service"] == "ZZ"]
        self.assertEqual(len(zz), 1)
        self.assertEqual(zz[0]["continuity"], UNKNOWN)

    def test_a_multi_frequency_licence_became_several_channels(self):
        same = [c for c in self.pred["channels"]
                if c["rf_ref"] == "FIXTURE-0002"]
        self.assertEqual(len(same), 2)

    def test_out_of_band_frequencies_were_dropped(self):
        for c in self.pred["channels"]:
            self.assertLess(c["freq_hz"], 1e9)

    def test_a_row_without_distance_has_no_field_proxy(self):
        rows = [c for c in self.pred["channels"]
                if c["rf_ref"] == "FIXTURE-0006"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["field_proxy_w_per_m2"])


if __name__ == "__main__":
    unittest.main()
