"""--engine report: ingesting a location report downloaded from the map UI.

This is the path a survey volunteer actually has. qth_cli.mjs is on an
unmerged sota-rf branch and is not served, so on any machine without that
worktree the UI download is the ONLY way to get a /2 prediction. The tests
that matter here are the two that would let a wrong answer look right:

  * the header is taken from the FILE, so a prediction cannot end up
    describing a different point than its rows were traced at; and
  * nothing from the file's geometry -- the exact QTH in query.lat/lon, the
    per-row distance and bearing -- reaches a feedback body. The how-to
    warns volunteers that the download holds their exact coordinates, so
    this file is the thing standing between that warning and a leak.

No Node and no network: the fixture is synthetic.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

try:                                    # pytest, from the repo root
    from tests import server_contract
except ImportError:                     # python tests/test_x.py
    import server_contract


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qth = _load("qth_predict")
sf = _load("submit_feedback")

from occupancy_survey import reference
from occupancy_survey.channels import resolve_bands

BANDS = resolve_bands(["vhf_lmr_low", "vhf_lmr_high", "uhf_lmr", "nwr",
                       "bcast_fm"])
REPORT = FIXTURES / "qth_rfi_report_sample.json"


def _ui_report(blank_refs=True, **overrides):
    """The fixture as the deployed UI would hand it over.

    The live page stamps its deploy hash rather than the CLI's node-<sha1>,
    and the 2026-10-28 transmitter export behind it has no ref/call column,
    so every row's ref comes back blank (measured against
    km6am.com/summitRFI/map/data/ on 2026-11-01).
    """
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    data["model"]["engine"] = "91f2a3daca"
    data["model"]["data"] = "2026-10-28"
    if blank_refs:
        for row in data["channels"]:
            row["ref"] = ""
            row["call"] = ""
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    tmp = Path(tempfile.mkdtemp()) / "qth-rfi-2026-11-01.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    return tmp


def _run(report, *extra):
    out = Path(tempfile.mkdtemp()) / "prediction.json"
    rc = qth.main(["--engine", "report", "--report", str(report),
                   "--out", str(out), *extra])
    assert rc == 0
    return json.loads(out.read_text(encoding="utf-8"))


class TestTheFileIsTheInput(unittest.TestCase):
    """No sota-rf, no Node, no FCC cache, and no --lat/--lon."""

    def test_a_download_alone_produces_a_v2_prediction(self):
        pred = _run(_ui_report())
        self.assertEqual(pred["schema"], reference.PREDICTION_SCHEMA_V2)
        self.assertTrue(pred["channels"])

    def test_the_point_comes_from_the_file_not_the_command_line(self):
        pred = _run(_ui_report())
        self.assertEqual(pred["query"]["lat"], 40.0)
        self.assertEqual(pred["query"]["lon"], -120.0)
        self.assertEqual(pred["query"]["ant_agl_m"], 10.0)

    def test_the_page_engine_hash_is_carried_not_rewritten(self):
        """Pooling a page prediction with a CLI one is the error
        CAVEATS_V2 engine_mixing names, and it is only detectable if the
        stamp survives."""
        pred = _run(_ui_report())
        self.assertEqual(pred["model"]["engine"], "91f2a3daca")
        self.assertEqual(pred["model"]["data"], "2026-10-28")
        self.assertIn("engine_mixing", pred["caveats"])

    def test_a_blank_span_stays_blank(self):
        """The how-to tells volunteers to leave From/To empty, so the
        report covers everything. Substituting the band list would claim
        it had never looked outside the surveyed bands."""
        pred = _run(_ui_report(query={"f_min_mhz": None,
                                      "f_max_mhz": None}))
        self.assertIsNone(pred["query"]["f_min_mhz"])
        self.assertIsNone(pred["query"]["f_max_mhz"])

    def test_a_typed_span_is_carried(self):
        pred = _run(_ui_report())
        self.assertEqual(pred["query"]["f_min_mhz"], 88.1)
        self.assertEqual(pred["query"]["f_max_mhz"], 470.0)

    def test_the_rows_match_the_shared_parser(self):
        report = _ui_report()
        pred = _run(report)
        self.assertEqual(
            [r["freq_hz"] for r in pred["channels"]],
            [r["freq_hz"] for r in qth.parse_qth_report(report, BANDS)])


class TestItRefusesRatherThanGuesses(unittest.TestCase):

    def _fails(self, *argv):
        with self.assertRaises(SystemExit) as cm:
            qth.main(list(argv))
        return str(cm.exception)

    def test_report_engine_without_a_file(self):
        msg = self._fails("--engine", "report")
        self.assertIn("--report", msg)
        self.assertIn("Download JSON", msg)

    def test_a_prediction_file_is_not_a_report(self):
        bad = Path(tempfile.mkdtemp()) / "pred.json"
        bad.write_text(json.dumps({
            "schema": reference.PREDICTION_SCHEMA_V2, "channels": []}))
        msg = self._fails("--engine", "report", "--report", str(bad))
        self.assertIn("sota-rf/qth-rfi", msg)

    def test_a_disagreeing_latitude_is_refused_not_resolved(self):
        report = _ui_report()
        msg = self._fails("--engine", "report", "--report", str(report),
                          "--lat", "37.8")
        self.assertIn("40.0", msg)
        self.assertIn("wrong station", msg)

    def test_a_matching_latitude_is_accepted(self):
        pred = _run(_ui_report(), "--lat", "40.0", "--lon", "-120.0",
                    "--ant-agl", "10")
        self.assertEqual(pred["query"]["lat"], 40.0)

    def test_a_disagreeing_antenna_height_is_refused(self):
        """It moves every diffraction loss, so a wrong one changes which
        channels are listed at all -- it does not just shift levels."""
        report = _ui_report()
        msg = self._fails("--engine", "report", "--report", str(report),
                          "--ant-agl", "3")
        self.assertIn("--ant-agl", msg)

    def test_a_report_with_no_antenna_height_is_refused(self):
        report = _ui_report(query={"ant_agl_m": None})
        msg = self._fails("--engine", "report", "--report", str(report))
        self.assertIn("ant_agl_m", msg)

    def test_the_other_engines_still_require_a_point(self):
        self.assertIn("--lat", self._fails("--engine", "pipeline"))
        self.assertIn("--lat", self._fails("--engine", "qth-cli"))


class TestItStatesWhatTheLiveExportCost(unittest.TestCase):
    """A report built from the deployed tiles is complete in row count and
    short in content. That is invisible downstream unless it is counted."""

    def test_blank_refs_are_counted_and_named(self):
        pred = _run(_ui_report(blank_refs=True))
        note = pred["query"]["emitter_refs"]
        self.assertIsNotNone(note)
        self.assertIn("blank emitter ref", note)
        self.assertIn(str(len(pred["channels"])), note)

    def test_a_full_export_leaves_no_note(self):
        pred = _run(_ui_report(blank_refs=False))
        self.assertIsNone(pred["query"]["emitter_refs"])

    def test_the_data_date_is_recorded_for_the_submission(self):
        pred = _run(_ui_report())
        self.assertEqual(pred["query"]["report_data_date"], "2026-10-28")
        self.assertEqual(pred["query"]["data_source"],
                         "downloaded from the map UI")


HIST = {"lo_db": 0.0, "bin_db": 1.0, "n_bins": 40}


def _measured_for(pred):
    """Synthetic occupancy for exactly the channels the report predicted."""
    out = {}
    for i, row in enumerate(pred["channels"]):
        out[row["freq_hz"]] = {
            "freq_hz": row["freq_hz"], "band": row["band"],
            "band_role": "target", "occupancy": 0.4 if i % 2 else 0.0,
            "n_decided": 1000, "threshold_db": 10.0, "sites": ["s1"],
            "comparable": True, "excess_db_p50": 12.0,
            "excess_db_p90": 22.0}
    return out


def _dataset():
    return {
        "schema": "occupancy-survey/1",
        "interval": {"start_utc": "2026-11-01T12:00:00+00:00",
                     "end_utc": "2026-11-01T13:00:00+00:00"},
        "acquisition": {
            "site": {"site_id": "site-a", "grid_square": "CM87",
                     "receiver_model": "RSPdx", "antenna": "discone",
                     "antenna_height_m": 10.0, "preamp": "none",
                     "feedline": "RG-6", "front_end_filters": "notches"},
            "detection": {"threshold_db": 10.0, "estimator": "pooled",
                          "baseline_percentile": 25.0},
            "receiver": {"gain": "IF 20 / RF 20", "agc_enabled": False,
                         "backend": "rspdx_gr"},
            "sampling": {"dwell_s": 20.0, "integration_s": 0.1},
            "histogram": HIST,
        },
        "bands": {"vhf_lmr_low": {"role": "target", "bins_per_channel": 16},
                  "vhf_lmr_high": {"role": "target",
                                   "bins_per_channel": 16},
                  "uhf_lmr": {"role": "target", "bins_per_channel": 16},
                  "nwr": {"role": "control", "bins_per_channel": 16},
                  "bcast_fm": {"role": "control",
                               "bins_per_channel": 16}},
        "channels": [],
    }


class TestNothingFromTheDownloadLeaves(unittest.TestCase):
    """The end-to-end check, on the real ingest path rather than a
    hand-built prediction: UI download -> /2 prediction -> feedback body."""

    def _body(self):
        pred = _run(_ui_report())
        return pred, sf.build_body(pred, _measured_for(pred), [_dataset()])

    def test_the_body_carries_no_geometry(self):
        _, body = self._body()
        self.assertEqual(sf.forbidden_in(body), [])

    def test_the_exact_qth_does_not_survive_the_build(self):
        """query.lat/lon is the one field in the prediction that IS the
        home address, and the prediction header is otherwise copied
        wholesale into the body."""
        pred, body = self._body()
        self.assertEqual(pred["query"]["lat"], 40.0)
        blob = json.dumps(body)
        self.assertNotIn("40.0", blob)
        self.assertNotIn("-120.0", blob)

    def test_per_row_distance_and_bearing_do_not_survive(self):
        _, body = self._body()
        for row in body["prediction"]["channels"]:
            self.assertEqual(
                [k for k in row if k in sf.SERVER_GEOMETRY_KEYS], [])

    def test_the_disclosure_check_passes(self):
        pred, body = self._body()
        self.assertEqual(sf.disclosure_problems(body), [])

    def test_every_predicted_channel_is_submitted(self):
        pred, body = self._body()
        self.assertEqual(len(body["prediction"]["channels"]),
                         len(pred["channels"]))


@unittest.skipIf(server_contract.SERVER is None,
                 server_contract.SKIP_REASON)
class TestTheServerAcceptsAnIngestedReport(unittest.TestCase):
    """The only test here that can tell us the volunteer path actually
    works end to end. Everything above is this repo agreeing with itself."""

    def _cleaned(self):
        pred = _run(_ui_report())
        body = sf.build_body(pred, _measured_for(pred), [_dataset()])
        return pred, server_contract.SERVER.clean_survey(body)

    def test_a_ui_download_survives_the_round_trip(self):
        pred, cleaned = self._cleaned()
        self.assertEqual(cleaned["schema"], sf.SCHEMA)
        self.assertEqual(len(cleaned["prediction"]["channels"]),
                         len(pred["channels"]))

    def test_the_page_hash_is_what_gets_stored(self):
        """The server stores engine as a column and keys re-reads on it, so
        a page prediction has to arrive stamped as a page prediction."""
        _, cleaned = self._cleaned()
        self.assertEqual(cleaned["prediction"]["engine"], "91f2a3daca")
        self.assertEqual(cleaned["prediction"]["data"], "2026-10-28")

    def test_the_server_strips_nothing_we_needed(self):
        _, cleaned = self._cleaned()
        for row in cleaned["prediction"]["channels"]:
            self.assertIsNotNone(row["freq_mhz"])
            self.assertIn(row["path"], sf.SERVER_PATHS)


class TestTheSummaryDoesNotMislabelItsOrdering(unittest.TestCase):
    """The rows are field-ranked, not dBm-ranked. A heading that says
    "strongest" over a dBm column asserts something the data does not
    support: on a real report the first row read -32.4 dBm while a TV
    carrier further down read -29.0."""

    def _heading(self):
        import contextlib
        import io
        pred = _run(_ui_report())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            qth._summarise(pred["channels"], "report")
        return buf.getvalue()

    def test_the_heading_names_the_ranking_it_shows(self):
        text = self._heading()
        self.assertIn("by FIELD", text)
        self.assertIn("not by dBm", text)

    def test_a_stronger_dbm_row_can_sit_below_the_first(self):
        """The case that makes the heading necessary, built explicitly:
        field order and dBm order disagree, so the first row shown is not
        the strongest level. The synthetic fixture's own rows happen to
        agree, which is why this cannot be left to it."""
        data = json.loads(REPORT.read_text(encoding="utf-8"))
        rows = [r for r in data["channels"]
                if r.get("p_dbm_ref") is not None][:2]
        rows[0]["field_vm"], rows[0]["p_dbm_ref"] = 1.0, -80.0
        rows[1]["field_vm"], rows[1]["p_dbm_ref"] = 0.5, -20.0
        data["channels"] = rows
        report = _ui_report(channels=rows)
        pred = _run(report)
        levels = [r["p_dbm_ref"] for r in pred["channels"]]
        self.assertEqual(levels[0], -80.0)
        self.assertEqual(max(levels), -20.0)
        self.assertNotEqual(levels[0], max(levels))


if __name__ == "__main__":
    unittest.main()
