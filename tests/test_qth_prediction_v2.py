"""The /2 prediction path: reading sota-rf's location report.

The tests that matter here are the two that would let a wrong answer look
right: that a /2 row carries every key a /1 row does (so one validator
reads both, and nothing downstream silently gets None), and that the field
proxy is the SAME quantity in both engines rather than merely a number in
the same place.

No Node and no network: every test reads the fixture, which is synthetic.
"""

import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qth = _load("qth_predict")
validate = _load("validate_occupancy")

from occupancy_survey import reference
from occupancy_survey.channels import resolve_bands

BANDS = resolve_bands(["vhf_lmr_low", "vhf_lmr_high", "uhf_lmr", "nwr",
                       "bcast_fm"])
REPORT = FIXTURES / "qth_rfi_report_sample.json"


def _report(**overrides):
    """The fixture, optionally mutated, written to a temp file."""
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    data.update(overrides)
    tmp = Path(tempfile.mkdtemp()) / "report.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    return tmp


class TestReportContract(unittest.TestCase):

    def test_rejects_a_pipeline_csv_schema(self):
        bad = _report(schema="occupancy-survey/qth-prediction/1")
        with self.assertRaises(SystemExit) as cm:
            qth.parse_qth_report(bad, BANDS)
        self.assertIn("sota-rf/qth-rfi", str(cm.exception))

    def test_rejects_a_report_with_no_schema(self):
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        del data["schema"]
        tmp = Path(tempfile.mkdtemp()) / "r.json"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SystemExit):
            qth.parse_qth_report(tmp, BANDS)

    def test_accepts_a_future_minor_version(self):
        ok = _report(schema="sota-rf/qth-rfi/1.1")
        self.assertTrue(qth.parse_qth_report(ok, BANDS))

    def test_a_report_without_p_dbm_ref_is_refused_by_name(self):
        """A /1 wearing a 2 must not validate -- it would support the
        audibility floor and the path residual without the data for them."""
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        for row in data["channels"]:
            row.pop("p_dbm_ref")
        tmp = Path(tempfile.mkdtemp()) / "r.json"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            qth.parse_qth_report(tmp, BANDS)
        self.assertIn("p_dbm_ref", str(cm.exception))

    def test_a_report_without_path_is_refused_by_name(self):
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        for row in data["channels"]:
            row.pop("path")
        tmp = Path(tempfile.mkdtemp()) / "r.json"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            qth.parse_qth_report(tmp, BANDS)
        self.assertIn("path", str(cm.exception))

    def test_an_empty_channel_list_is_an_answer_not_a_failure(self):
        """Nothing within the cap above the floor is a legitimate result;
        it must not be reported as every required field missing."""
        empty = _report(channels=[], n_channels=0)
        self.assertEqual(qth.parse_qth_report(empty, BANDS), [])

    def test_required_fields_are_a_subset_of_the_recorded_shape(self):
        self.assertTrue(
            set(reference.QTH_RFI_REQUIRED_FIELDS)
            <= set(reference.QTH_RFI_CHANNEL_FIELDS))

    def test_missing_qth_rfi_fields_names_only_what_is_absent(self):
        present = set(reference.QTH_RFI_CHANNEL_FIELDS) - {"path"}
        self.assertEqual(reference.missing_qth_rfi_fields(present), ["path"])
        self.assertEqual(
            reference.missing_qth_rfi_fields(
                reference.QTH_RFI_CHANNEL_FIELDS), [])

    def test_withheld_fields_are_all_fields_the_report_actually_emits(self):
        """A withheld-field list that names something the report does not
        emit gives false assurance."""
        for f in reference.QTH_RFI_WITHHELD_FIELDS:
            self.assertIn(f, reference.QTH_RFI_CHANNEL_FIELDS, f)


class TestBandFiltering(unittest.TestCase):

    def setUp(self):
        self.rows = qth.parse_qth_report(REPORT, BANDS)
        self.mhz = sorted(round(r["freq_hz"] / 1e6, 4) for r in self.rows)

    def test_keeps_only_in_band_channels(self):
        self.assertEqual(self.mhz,
                         [98.1, 151.0, 155.0, 158.7, 162.475, 460.5])

    def test_drops_a_channel_in_the_gap_between_two_bands(self):
        """156.5 MHz is inside the queried span but in neither band, so a
        span filter alone would keep it."""
        self.assertNotIn(156.5, self.mhz)

    def test_drops_a_channel_outside_every_band(self):
        self.assertNotIn(300.0, self.mhz)

    def test_drops_a_source_with_no_frequency_on_record(self):
        self.assertEqual(
            [r for r in self.rows if r["rf_ref"] == "SYN0008"], [])

    def test_assigns_the_band_and_its_role(self):
        by_mhz = {round(r["freq_hz"] / 1e6, 4): r for r in self.rows}
        self.assertEqual(by_mhz[155.0]["band"], "vhf_lmr_low")
        self.assertEqual(by_mhz[158.7]["band"], "vhf_lmr_high")
        self.assertEqual(by_mhz[162.475]["band"], "nwr")
        self.assertEqual(by_mhz[460.5]["band"], "uhf_lmr")
        self.assertEqual(by_mhz[98.1]["band"], "bcast_fm")
        self.assertEqual(by_mhz[98.1]["band_role"], "control")
        self.assertEqual(by_mhz[155.0]["band_role"], "target")

    def test_sorted_by_field_proxy_descending(self):
        proxies = [r["field_proxy_w_per_m2"] for r in self.rows]
        self.assertEqual(proxies, sorted(proxies, reverse=True))
        self.assertEqual(self.rows[0]["band"], "bcast_fm")


class TestFieldProxyCommensurability(unittest.TestCase):
    """The load-bearing claim: field_vm**2 / 30 is the SAME quantity as
    /1's ERP/d^2, not just a number in the same key."""

    def test_proxy_equals_erp_over_distance_squared_at_unity_gain(self):
        rows = qth.parse_qth_report(REPORT, BANDS)
        row = next(r for r in rows if r["rf_ref"] == "SYN0001")
        # The fixture builds this row's field_vm from sqrt(30*ERP)/d with
        # g = 1 and L = 0, which is sota-rf's own formula.
        expected = row["erp_w"] / row["distance_m"] ** 2
        self.assertAlmostEqual(row["field_proxy_w_per_m2"], expected,
                               delta=expected * 1e-9)

    def test_proxy_is_field_squared_over_thirty(self):
        rows = qth.parse_qth_report(REPORT, BANDS)
        for r in rows:
            self.assertAlmostEqual(r["field_proxy_w_per_m2"],
                                   r["field_vm"] ** 2 / 30.0,
                                   delta=1e-18)

    def test_a_missing_field_gives_none_not_zero(self):
        """Zero would rank as the weakest real emitter; None is absent."""
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        data["channels"][0]["field_vm"] = None
        tmp = Path(tempfile.mkdtemp()) / "r.json"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        rows = qth.parse_qth_report(tmp, BANDS)
        row = next(r for r in rows if r["rf_ref"] == "SYN0001")
        self.assertIsNone(row["field_proxy_w_per_m2"])
        self.assertIsNone(row["field_vm"])


class TestSchemaCompatibility(unittest.TestCase):
    """What makes one validator read both engines."""

    def test_v2_rows_carry_every_key_a_v1_row_carries(self):
        v1 = qth.parse_emitters(FIXTURES / "sota_rf_sources_sample.csv",
                                BANDS)
        self.assertTrue(v1, "the /1 fixture produced no rows")
        v2 = qth.parse_qth_report(REPORT, BANDS)
        missing = set(v1[0]) - set(v2[0])
        self.assertEqual(missing, set(),
                         f"/2 rows are missing /1 keys: {sorted(missing)}")

    def test_v2_adds_the_fields_that_justify_it(self):
        row = qth.parse_qth_report(REPORT, BANDS)[0]
        for key in ("p_dbm_ref", "path", "error_db", "field_vm", "callsign",
                    "erp_basis", "diffraction_db", "report_rank"):
            self.assertIn(key, row)

    def test_validate_occupancy_accepts_a_v2_prediction(self):
        rows = qth.parse_qth_report(REPORT, BANDS)
        with tempfile.TemporaryDirectory() as d:
            out = qth.write_prediction(
                rows, Path(d) / "p.json", {"bands": ["vhf_lmr_low"]},
                schema=reference.PREDICTION_SCHEMA_V2,
                caveats=qth.CAVEATS_V2, model={"engine": "node-test"})
            loaded = validate.load_prediction(out)
        self.assertEqual(loaded["schema"], "occupancy-survey/qth-prediction/2")
        self.assertEqual(loaded["n_predicted_channels"], len(rows))

    def test_the_two_schemas_are_distinct_strings(self):
        self.assertNotEqual(reference.PREDICTION_SCHEMA,
                            reference.PREDICTION_SCHEMA_V2)
        for s in (reference.PREDICTION_SCHEMA,
                  reference.PREDICTION_SCHEMA_V2):
            self.assertTrue(s.startswith("occupancy-survey/qth-prediction"))


class TestMinimumDisclosure(unittest.TestCase):

    def setUp(self):
        self.rows = qth.parse_qth_report(REPORT, BANDS)

    def test_bearing_is_not_carried(self):
        """Bearing plus distance to an emitter at known coordinates
        trilaterates the receiver. Nothing downstream reads it."""
        for r in self.rows:
            self.assertNotIn("bearing_deg", r)

    def test_fspl_is_not_carried(self):
        for r in self.rows:
            self.assertNotIn("fspl_db", r)

    def test_the_payload_says_the_file_locates_the_station(self):
        self.assertIn("stays_local", qth.CAVEATS_V2)
        self.assertIn("trilaterate", qth.CAVEATS_V2["stays_local"])

    def test_distance_is_still_carried_and_still_a_hazard(self):
        """Carried deliberately: validate_occupancy reads it. The caveat,
        not its absence, is what keeps it local."""
        self.assertTrue(all(r["distance_m"] is not None for r in self.rows))
        self.assertIn("distance_m", reference.QTH_RFI_WITHHELD_FIELDS)


class TestCallsignDisclosureInteraction(unittest.TestCase):
    """/2 adds a callsign field, and the disclosure scanner treats
    callsign-shaped strings as high severity. Checked rather than assumed,
    because a flood of expected high findings would train the operator to
    ignore the scanner -- which is worse than not having it.

    It happens not to flood: the pattern is amateur-shaped, and the
    emitter `call` field holds ULS station calls, which are letters then
    digits. That is a property of two regexes in two files with nothing
    tying them together, so it is pinned here.
    """

    def test_emitter_callsigns_do_not_raise_high_findings(self):
        from occupancy_survey.disclosure import scan, worst_severity
        rows = [{"freq_hz": 155e6, "rf_ref": "X", "callsign": c,
                 "owner": "City of Somewhere"}
                for c in ("KMA367", "WPSW123", "WQGM842", "KNFJ456")]
        findings = scan({"channels": rows})
        self.assertNotEqual(worst_severity(findings), "high",
                            [f.kind for f in findings])

    def test_an_operator_shaped_callsign_is_still_caught(self):
        """The check that matters: adding this field must not have made the
        scanner blind to the operator's own callsign."""
        from occupancy_survey.disclosure import scan
        findings = scan({"site": {"site_id": "W6ABC"}})
        self.assertIn("callsign", [f.kind for f in findings])

    def test_the_fixture_itself_carries_no_operator_shaped_callsign(self):
        from occupancy_survey.disclosure import CALLSIGN_RE
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        for row in data["channels"]:
            self.assertIsNone(CALLSIGN_RE.match(row["call"] or "x"),
                              row["call"])


class TestLevelsAndClasses(unittest.TestCase):

    def setUp(self):
        self.by_ref = {r["rf_ref"]: r
                       for r in qth.parse_qth_report(REPORT, BANDS)}

    def test_model_dbm_and_error_band_are_carried(self):
        row = self.by_ref["SYN0001"]
        self.assertEqual(row["p_dbm_ref"], -62.0)
        self.assertEqual(row["error_db"], 8.0)
        self.assertEqual(row["error_basis"], "assumed, unvalidated")

    def test_a_row_with_no_model_level_is_kept_with_none(self):
        row = self.by_ref["SYN0009"]
        self.assertIsNone(row["p_dbm_ref"])
        self.assertIsNotNone(row["field_proxy_w_per_m2"])

    def test_path_class_is_carried_for_the_residual_split(self):
        self.assertEqual(self.by_ref["SYN0001"]["path"], "los")
        self.assertEqual(self.by_ref["SYN0002"]["path"], "diffracted")
        self.assertEqual(self.by_ref["SYN0002"]["diffraction_db"], 11.0)

    def test_continuity_comes_from_the_service_code(self):
        """Same table, same codes, same answers as the /1 path -- including
        NWR, whose 'continuous' was retracted to 'unknown' because the
        carrier keying is uncertain. A /2 reader that quietly restored it
        would put an unmeasured duty back into the risk model."""
        nwr = self.by_ref["SYN0003"]
        self.assertEqual(nwr["service"], "NWR")
        self.assertEqual(nwr["continuity"], "unknown")
        self.assertIsNone(nwr["expected_duty"])
        self.assertIn("retracted", nwr["continuity_basis"])

        self.assertEqual(self.by_ref["SYN0001"]["continuity"],
                         "intermittent")          # PW, TIA-603 test cycle
        self.assertEqual(self.by_ref["SYN0002"]["continuity"],
                         "continuous")            # YG, a control channel
        self.assertEqual(self.by_ref["SYN0005"]["continuity"],
                         "continuous")            # FM, programme on it

    def test_the_two_engines_agree_on_continuity_for_the_same_code(self):
        """The class must not depend on which engine named the service."""
        from occupancy_survey.channels import continuity_for
        for row in self.by_ref.values():
            self.assertEqual(row["continuity"],
                             continuity_for(row["service"]).continuity,
                             row["service"])

    def test_callsign_is_carried_and_blank_becomes_empty(self):
        self.assertEqual(self.by_ref["SYN0001"]["callsign"], "WX0TEST")

    def test_report_rank_is_not_called_rank(self):
        """The report ranks over the whole queried span, so after the band
        filter it is not 1..N and must not read as this file's order."""
        row = self.by_ref["SYN0001"]
        self.assertEqual(row["report_rank"], 3)
        self.assertNotIn("rank", row)
        ranks = [r["report_rank"] for r in self.by_ref.values()]
        self.assertNotEqual(sorted(ranks), list(range(1, len(ranks) + 1)))


class TestWritePrediction(unittest.TestCase):

    def test_v1_defaults_are_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            out = qth.write_prediction([], Path(d) / "p.json", {"a": 1})
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "occupancy-survey/qth-prediction/1")
        self.assertEqual(data["caveats"], qth.CAVEATS_V1)
        self.assertNotIn("model", data)

    def test_v2_carries_the_model_header(self):
        model = {"engine": "node-abc1234567", "data": "2026-11-01"}
        with tempfile.TemporaryDirectory() as d:
            out = qth.write_prediction(
                [], Path(d) / "p.json", {"a": 1},
                schema=reference.PREDICTION_SCHEMA_V2,
                caveats=qth.CAVEATS_V2, model=model)
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], model)

    def test_v2_caveats_warn_against_pooling_two_engines(self):
        self.assertIn("engine_mixing", qth.CAVEATS_V2)

    def test_v2_caveats_name_the_shared_sign_of_the_errors(self):
        text = qth.CAVEATS_V2["errors_share_a_sign"]
        self.assertIn("UP", text)

    def test_v2_caveats_keep_p_dbm_ref_a_prediction(self):
        text = qth.CAVEATS_V2["p_dbm_ref"]
        self.assertIn("not a", text.lower())


class TestAntennaHeight(unittest.TestCase):
    """It moves every diffraction loss, so it is never defaulted."""

    class _Args:
        def __init__(self, ant_agl=None, config=None):
            self.ant_agl = ant_agl
            self.config = config

    def test_refuses_to_guess(self):
        with self.assertRaises(SystemExit) as cm:
            qth._resolve_ant_agl(self._Args())
        self.assertIn("--ant-agl", str(cm.exception))

    def test_explicit_height_wins(self):
        self.assertEqual(
            qth._resolve_ant_agl(self._Args(ant_agl=12.5)), 12.5)

    def test_reads_it_from_a_survey_config(self):
        cfg = ("site:\n"
               "  site_id: syn\n"
               "  receiver_model: synthetic\n"
               "  antenna: synthetic\n"
               "  antenna_height_m: 9.5\n")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.yaml"
            path.write_text(cfg, encoding="utf-8")
            self.assertEqual(
                qth._resolve_ant_agl(self._Args(config=str(path))), 9.5)

    def test_a_config_without_a_height_is_refused_not_defaulted(self):
        cfg = ("site:\n"
               "  site_id: syn\n"
               "  receiver_model: synthetic\n"
               "  antenna: synthetic\n")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.yaml"
            path.write_text(cfg, encoding="utf-8")
            with self.assertRaises(SystemExit) as cm:
                qth._resolve_ant_agl(self._Args(config=str(path)))
            self.assertIn("antenna_height_m", str(cm.exception))

    def test_an_explicit_height_overrides_the_config(self):
        cfg = ("site:\n"
               "  site_id: syn\n"
               "  receiver_model: synthetic\n"
               "  antenna: synthetic\n"
               "  antenna_height_m: 9.5\n")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.yaml"
            path.write_text(cfg, encoding="utf-8")
            args = self._Args(ant_agl=30.0, config=str(path))
            self.assertEqual(qth._resolve_ant_agl(args), 30.0)


class TestRunQthCli(unittest.TestCase):
    """No Node is launched here; only the refusals are exercised."""

    def test_a_missing_cli_names_the_branch_and_the_way_out(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError) as cm:
                qth.run_qth_cli(Path(d) / "r.json", 40.0, -120.0, 10.0,
                                88.1, 470.0, cli=Path(d) / "absent.mjs")
        msg = str(cm.exception)
        self.assertIn("qth-rfi-report", msg)
        self.assertIn("--engine pipeline", msg)

    def test_remote_terrain_needs_an_explicit_opt_in(self):
        """Fetching AWS terrain for a 120 km box tells a third party
        roughly where the receiver is."""
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                qth.main(["--engine", "qth-cli", "--lat", "40.0",
                          "--lon", "-120.0", "--ant-agl", "10",
                          "--work-dir", d, "--out", str(Path(d) / "p.json")])
        msg = str(cm.exception)
        self.assertIn("--allow-remote-terrain", msg)
        self.assertIn("--dem-2s", msg)

    def test_reusing_a_report_launches_nothing(self):
        """--reuse must read the saved report, not re-derive it: the test
        passes a cli path that does not exist."""
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            work.mkdir()
            (work / "qth_rfi_report.json").write_text(
                REPORT.read_text(encoding="utf-8"), encoding="utf-8")
            rc = qth.main(["--engine", "qth-cli", "--lat", "40.0",
                           "--lon", "-120.0", "--ant-agl", "10", "--reuse",
                           "--qth-cli", str(Path(d) / "absent.mjs"),
                           "--work-dir", str(work),
                           "--out", str(Path(d) / "p.json")])
            self.assertEqual(rc, 0)
            data = json.loads((Path(d) / "p.json").read_text(
                encoding="utf-8"))
        self.assertEqual(data["schema"], "occupancy-survey/qth-prediction/2")
        self.assertEqual(data["n_predicted_channels"], 6)
        self.assertEqual(data["model"]["engine"], "node-0000synth0")
        self.assertEqual(data["query"]["ant_agl_m"], 10.0)


class TestNumericCoercion(unittest.TestCase):

    def test_blank_and_nonsense_become_none(self):
        for bad in (None, "", "n/a", [], {}):
            self.assertIsNone(qth._num(bad))

    def test_nan_and_inf_become_none(self):
        self.assertIsNone(qth._num(float("nan")))
        self.assertIsNone(qth._num(float("inf")))
        self.assertIsNone(qth._num("NaN"))

    def test_numbers_and_numeric_strings_pass(self):
        self.assertEqual(qth._num("12.5"), 12.5)
        self.assertEqual(qth._num(0), 0.0)
        self.assertEqual(qth._num(-130), -130.0)


if __name__ == "__main__":
    unittest.main()
