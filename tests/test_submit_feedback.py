"""What leaves the machine, and what refuses to.

The allowlist tests are the ones that matter. A /2 prediction row carries
distance_m and emitter coordinates, and two of those trilaterate the
receiver, so a leak here is not a cosmetic defect -- it publishes someone's
address. forbidden_in() is the second net under the allowlist, and it is
tested separately from the allowlist so that neither alone is load-bearing.
"""

import gzip
import importlib.util
import json
import os
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


sf = _load("submit_feedback")


def _pred_channel(freq_hz, proxy, **kw):
    """A /2 prediction row, with every local-only field present."""
    row = {"freq_hz": freq_hz, "band": "vhf_lmr_low", "band_role": "target",
           "rf_ref": "SYN001", "rf_lat": 37.9, "rf_lon": -122.3,
           "owner": "Synthetic County", "service": "PW",
           "continuity": "intermittent", "expected_duty": None,
           "continuity_basis": "architectural",
           "distance_m": 5000.0, "erp_w": 100.0,
           "field_proxy_w_per_m2": proxy, "callsign": "WX0TEST",
           "erp_basis": "licensed maximum", "field_vm": 0.01,
           "p_dbm_ref": -62.0, "error_db": 8.0,
           "error_basis": "assumed, unvalidated", "path": "los",
           "diffraction_db": 0.0, "report_rank": 9999, "in_totals": True,
           "db": "ULS"}
    row.update(kw)
    return row


def _prediction(*channels):
    return {"schema": "occupancy-survey/qth-prediction/2",
            "model": {"engine": "node-abc1234567", "data": "2026-11-01",
                      "dem": 'SRTM 2"'},
            "channels": list(channels)}


HIST = {"lo_db": 0.0, "bin_db": 1.0, "n_bins": 40}


def _measured(freq_hz, occ, n=1000, band="vhf_lmr_low", p50=12.0, p90=22.0):
    return {freq_hz: {"freq_hz": freq_hz, "band": band,
                      "band_role": "target", "occupancy": occ,
                      "n_decided": n, "threshold_db": 10.0, "sites": ["s1"],
                      "comparable": True, "excess_db_p50": p50,
                      "excess_db_p90": p90}}


def _dataset(site_id="site-a", grid="CM87", agc=False, **site_over):
    site = {"site_id": site_id, "grid_square": grid,
            "receiver_model": "RSPdx", "antenna": "discone",
            "antenna_height_m": 10.0, "preamp": "none",
            "feedline": "RG-6", "front_end_filters": "notches",
            "operator_note": "behind the garage at 12 Elm Street"}
    site.update(site_over)
    return {
        "schema": "occupancy-survey/1",
        "interval": {"start_utc": "2026-11-01T12:00:00+00:00",
                     "end_utc": "2026-11-01T13:00:00+00:00"},
        "acquisition": {
            "site": site,
            "detection": {"threshold_db": 10.0, "estimator": "pooled",
                          "baseline_percentile": 25.0},
            "receiver": {"gain": "IF 20 / RF 20", "agc_enabled": agc,
                         "backend": "rspdx_gr"},
            "sampling": {"dwell_s": 20.0, "integration_s": 0.1},
            "histogram": {"lo_db": 0.0, "bin_db": 1.0, "n_bins": 40},
        },
        "bands": {"vhf_lmr_low": {"role": "target", "bins_per_channel": 16,
                                  "channels": []}},
    }


# ------------------------------------------------------------------- grid #

class TestGridForSubmission(unittest.TestCase):

    def test_four_characters_pass(self):
        self.assertEqual(sf.grid_for_submission("CM87")[0], "CM87")

    def test_six_characters_pass_with_the_usual_case(self):
        grid, note = sf.grid_for_submission("cm87uk")
        self.assertEqual(grid, "CM87uk")
        self.assertIn("6-character", note)

    def test_eight_characters_are_withheld_not_truncated(self):
        """An operator who wrote 8 may not have meant to share even 6;
        shortening it silently would decide that for them."""
        grid, note = sf.grid_for_submission("CM87uk12")
        self.assertIsNone(grid)
        self.assertIn("withheld", note)
        self.assertIn("8-character", note)

    def test_absent_is_withheld(self):
        self.assertEqual(sf.grid_for_submission(None)[0], None)
        self.assertEqual(sf.grid_for_submission("")[0], None)

    def test_an_odd_length_is_withheld(self):
        self.assertIsNone(sf.grid_for_submission("CM8")[0])
        self.assertIsNone(sf.grid_for_submission("CM87u")[0])


# -------------------------------------------------------------- allowlist #

class TestPredictionRows(unittest.TestCase):

    def test_only_allowlisted_keys_are_copied(self):
        rows = sf.prediction_rows(_prediction(_pred_channel(155e6, 1e-3)))
        self.assertEqual(set(rows[0]), set(sf.PREDICTION_FIELDS))

    def test_the_local_only_fields_are_gone(self):
        rows = sf.prediction_rows(_prediction(_pred_channel(155e6, 1e-3)))
        for leak in ("distance_m", "rf_lat", "rf_lon", "field_vm",
                     "diffraction_db", "erp_w", "owner", "callsign"):
            self.assertNotIn(leak, rows[0], leak)

    def test_frequency_is_converted_to_mhz(self):
        rows = sf.prediction_rows(_prediction(_pred_channel(155_250_000.0,
                                                            1e-3)))
        self.assertAlmostEqual(rows[0]["freq_mhz"], 155.25)

    def test_rank_is_one_to_n_by_proxy_within_the_submission(self):
        pred = _prediction(_pred_channel(155e6, 1e-6),
                           _pred_channel(156e6, 1e-3),
                           _pred_channel(157e6, 1e-5))
        rows = sf.prediction_rows(pred)
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertAlmostEqual(rows[0]["freq_mhz"], 156.0)

    def test_rank_is_not_the_reports_own_rank(self):
        """The report ranks over the whole queried span before the band
        filter, so it is neither 1..N here nor comparable between
        submissions with different band sets."""
        rows = sf.prediction_rows(
            _prediction(_pred_channel(155e6, 1e-3, report_rank=9999)))
        self.assertEqual(rows[0]["rank"], 1)

    def test_a_band_filter_restricts_the_rows_and_the_ranks(self):
        pred = _prediction(
            _pred_channel(155e6, 1e-3),
            _pred_channel(98.1e6, 1e-2, band="bcast_fm"))
        rows = sf.prediction_rows(pred, bands={"vhf_lmr_low"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], 1)

    def test_a_blank_reference_becomes_null_not_an_empty_string(self):
        rows = sf.prediction_rows(
            _prediction(_pred_channel(155e6, 1e-3, rf_ref="")))
        self.assertIsNone(rows[0]["ref"])


class TestMeasuredRows(unittest.TestCase):

    def test_duty_is_a_float_when_decided(self):
        rows = sf.measured_rows(_measured(155e6, 0.42))
        self.assertAlmostEqual(rows[0]["duty"], 0.42)
        self.assertEqual(rows[0]["decided_n"], 1000)

    def test_nothing_decided_is_null_not_zero(self):
        """A zero duty is a measurement; an undecided channel is not. The
        shared model must not be fed confident silence nobody observed."""
        rows = sf.measured_rows(_measured(155e6, 0.0, n=0))
        self.assertIsNone(rows[0]["duty"])

    def test_a_none_occupancy_is_null(self):
        rows = sf.measured_rows(_measured(155e6, None))
        self.assertIsNone(rows[0]["duty"])

    def test_a_real_zero_duty_is_sent_as_zero(self):
        rows = sf.measured_rows(_measured(155e6, 0.0, n=1000))
        self.assertEqual(rows[0]["duty"], 0.0)

    def test_the_excess_percentiles_are_carried(self):
        rows = sf.measured_rows(_measured(155e6, 0.4, p50=11.0, p90=25.0),
                                hist_spec=HIST)
        self.assertEqual(rows[0]["excess_db_p50"], 11.0)
        self.assertEqual(rows[0]["excess_db_p90"], 25.0)

    def test_a_percentile_in_the_underflow_is_sent_as_null(self):
        """percentile_from_hist returns lo_db exactly for an underflow --
        a bound, not a value. The server does arithmetic on these, so a
        clamped 0.0 dB would enter a residual as a measured excess."""
        rows = sf.measured_rows(_measured(155e6, 0.4, p50=0.0, p90=25.0),
                                hist_spec=HIST)
        self.assertIsNone(rows[0]["excess_db_p50"])
        self.assertEqual(rows[0]["excess_db_p90"], 25.0)

    def test_a_percentile_in_the_overflow_is_sent_as_null(self):
        rows = sf.measured_rows(_measured(155e6, 0.4, p50=11.0, p90=40.0),
                                hist_spec=HIST)
        self.assertIsNone(rows[0]["excess_db_p90"])

    def test_a_bin_centre_at_the_same_magnitude_is_not_confused(self):
        """A genuine in-bin result is always lo + (i+0.5)*bin, so it can
        never coincide with either edge. 0.5 and 39.5 are real values."""
        rows = sf.measured_rows(_measured(155e6, 0.4, p50=0.5, p90=39.5),
                                hist_spec=HIST)
        self.assertEqual(rows[0]["excess_db_p50"], 0.5)
        self.assertEqual(rows[0]["excess_db_p90"], 39.5)


class TestForbiddenFields(unittest.TestCase):

    def test_finds_a_key_at_the_top(self):
        self.assertEqual(sf.forbidden_in({"lat": 37.9}), ["$.lat"])

    def test_finds_a_key_nested_in_a_list_of_dicts(self):
        found = sf.forbidden_in({"predicted": [{"a": 1},
                                               {"distance_m": 5000.0}]})
        self.assertEqual(found, ["$.predicted[1].distance_m"])

    def test_finds_a_key_several_levels_down(self):
        found = sf.forbidden_in({"a": {"b": {"c": {"bearing_deg": 42.0}}}})
        self.assertEqual(found, ["$.a.b.c.bearing_deg"])

    def test_operator_note_is_forbidden_because_free_text_holds_addresses(self):
        self.assertIn("operator_note", sf.FORBIDDEN_FIELDS)

    def test_a_clean_body_has_nothing(self):
        self.assertEqual(sf.forbidden_in({"freq_mhz": 155.0, "duty": 0.4}),
                         [])


# ------------------------------------------------------------ body + check #

class TestBuildBody(unittest.TestCase):

    def _body(self, **kw):
        return sf.build_body(
            _prediction(_pred_channel(155e6, 1e-3)),
            _measured(155e6, 0.4), [_dataset(**kw)])

    def test_a_built_body_carries_no_forbidden_field(self):
        """The end-to-end property: a prediction row stuffed with every
        local-only field produces a body with none of them."""
        self.assertEqual(sf.forbidden_in(self._body()), [])

    def test_the_operator_note_never_reaches_the_body(self):
        body = self._body()
        self.assertNotIn("operator_note", json.dumps(body))
        self.assertNotIn("Elm Street", json.dumps(body))

    def test_the_model_header_travels_with_the_measurements(self):
        body = self._body()
        self.assertEqual(body["model"]["engine"], "node-abc1234567")
        self.assertEqual(body["prediction_schema"],
                         "occupancy-survey/qth-prediction/2")

    def test_hours_come_from_the_interval_timestamps(self):
        self.assertAlmostEqual(self._body()["acquisition"]["hours"], 1.0)

    def test_receiver_model_moves_from_site_to_receiver(self):
        """Where the qth-feedback schema puts it, not where the dataset
        does."""
        acq = self._body()["acquisition"]
        self.assertEqual(acq["receiver"]["receiver_model"], "RSPdx")
        self.assertNotIn("receiver_model", acq["site"])

    def test_intermod_unchecked_is_false_and_means_not_evidenced(self):
        self.assertFalse(self._body()["acquisition"]["intermod_checked"])

    def test_an_eight_character_grid_is_withheld_from_the_body(self):
        body = self._body(grid="CM87uk12")
        self.assertIsNone(body["acquisition"]["site"]["grid_square"])
        self.assertIn("withheld",
                      body["acquisition"]["site"]["location_precision"])


class TestCheck(unittest.TestCase):

    def _body(self, **kw):
        return sf.build_body(
            _prediction(_pred_channel(155e6, 1e-3)),
            _measured(155e6, 0.4), [_dataset(**kw)])

    def test_a_clean_body_has_no_problems(self):
        self.assertEqual(sf.check(self._body()), [])

    def test_agc_enabled_is_refused(self):
        problems = sf.check(self._body(agc=True))
        self.assertTrue(any("agc_enabled" in p for p in problems))

    def test_a_missing_gain_is_refused(self):
        body = self._body()
        body["acquisition"]["receiver"]["gain"] = None
        self.assertTrue(any("receiver.gain" in p for p in sf.check(body)))

    def test_a_missing_antenna_is_refused(self):
        body = self._body()
        body["acquisition"]["site"]["antenna"] = ""
        self.assertTrue(any("site.antenna" in p for p in sf.check(body)))

    def test_a_long_locator_is_refused_if_it_got_in(self):
        body = self._body()
        body["acquisition"]["site"]["grid_square"] = "CM87uk12"
        self.assertTrue(any("6 characters" in p for p in sf.check(body)))

    def test_an_injected_forbidden_field_is_caught(self):
        body = self._body()
        body["predicted"][0]["distance_m"] = 5000.0
        problems = sf.check(body)
        self.assertTrue(any("forbidden" in p for p in problems))
        self.assertTrue(any("$.predicted[0].distance_m" in p
                            for p in problems),
                        "the refusal must name where the leak is")

    def test_over_the_row_cap_is_refused_and_names_the_way_out(self):
        body = self._body()
        body["predicted"] = [{"freq_mhz": 150.0 + i * 1e-4}
                             for i in range(sf.MAX_ROWS + 1)]
        problems = sf.check(body)
        self.assertTrue(any("--top-per-band" in p for p in problems))


class TestDisclosure(unittest.TestCase):

    def test_a_callsign_shaped_site_id_is_refused(self):
        """A callsign resolves to a street address in the public FCC
        database. On the survey path this is a warning, because the
        operator's own files are their business. Here the data leaves the
        machine."""
        body = sf.build_body(
            _prediction(_pred_channel(155e6, 1e-3)),
            _measured(155e6, 0.4), [_dataset(site_id="W6ABC")])
        problems = sf.disclosure_problems(body)
        self.assertTrue(problems)
        self.assertTrue(any("callsign" in p for p in problems))

    def test_an_arbitrary_site_label_is_fine(self):
        body = sf.build_body(
            _prediction(_pred_channel(155e6, 1e-3)),
            _measured(155e6, 0.4), [_dataset(site_id="site-a")])
        self.assertEqual(sf.disclosure_problems(body), [])


# ------------------------------------------------------------------- size #

class TestBodySize(unittest.TestCase):

    def test_size_is_measured_not_estimated(self):
        body = {"predicted": [{"freq_mhz": 150.0 + i * 1e-4}
                              for i in range(100)]}
        size = sf.body_size(body)
        self.assertEqual(
            size["json_bytes"],
            len(json.dumps(body, allow_nan=False).encode("utf-8")))
        self.assertEqual(size["gzip_bytes"],
                         len(gzip.compress(
                             json.dumps(body, allow_nan=False)
                             .encode("utf-8"), compresslevel=9)))

    def test_the_row_cap_binds_before_the_size_cap(self):
        """MEASURED 2026-11-01: a full 100,000-row predicted list with
        10,000 measured rows of random values gzips to about 1.6 MB,
        inside the 2 MB limit. Random refs and floats compress WORSE than
        real data, which clusters by licensee and sits on a channel grid,
        so this is an upper bound. The 100,000-row cap is what binds."""
        import random
        random.seed(11)
        body = {
            "schema": sf.SCHEMA,
            "predicted": [
                {"freq_mhz": round(150.0 + (i % 320000) * 0.000625, 6),
                 "ref": f"K{random.randrange(100, 999)}"
                        f"{random.randrange(100, 999)}",
                 "db": "ULS", "service": "PW",
                 "p_dbm_ref": round(random.uniform(-130, -25), 1),
                 "error_db": 8.0, "path": "los", "rank": i + 1}
                for i in range(sf.MAX_ROWS)],
            "measured": [
                {"freq_mhz": round(150.0 + i * 0.0125, 6),
                 "duty": round(random.random(), 4),
                 "decided_n": 1000, "excess_db_p50": 12.0,
                 "excess_db_p90": 22.0}
                for i in range(10_000)],
        }
        size = sf.body_size(body)
        self.assertLess(size["gzip_bytes"], sf.MAX_GZIP_BYTES)
        self.assertGreater(size["gzip_bytes"], 1_000_000,
                           "unexpectedly small -- check the row builder")


# ------------------------------------------------------------------- main #

class TestMainDryRun(unittest.TestCase):

    def _setup(self, tmp, **ds):
        pred = Path(tmp) / "prediction.json"
        pred.write_text(json.dumps({
            **_prediction(_pred_channel(155e6, 1e-3)),
            "n_predicted_channels": 1}), encoding="utf-8")

        data = _dataset(**ds)
        data["bands"]["vhf_lmr_low"]["channels"] = [{
            "freq_hz": 155e6, "service": "PW", "n_decided": 1000,
            "hist": [0] * 40, "underflow": 600, "overflow": 400,
        }]
        path = Path(tmp) / "occ_site-a_20261101T120000Z.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(json.dumps(data))
        return str(pred), str(path)

    def test_a_dry_run_writes_the_body_and_sends_nothing(self):
        def _explode(*a, **k):
            raise AssertionError("a dry run must not post")
        original, sf.post = sf.post, _explode
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pred, ds = self._setup(tmp)
                out = Path(tmp) / "body.json"
                rc = sf.main(["--prediction", pred, ds, "--out", str(out)])
                self.assertEqual(rc, 0)
                body = json.loads(out.read_text(encoding="utf-8"))
        finally:
            sf.post = original
        self.assertEqual(body["schema"], sf.SCHEMA)
        self.assertEqual(sf.forbidden_in(body), [])
        self.assertEqual(len(body["predicted"]), 1)

    def test_a_callsign_site_id_makes_main_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            pred, ds = self._setup(tmp, site_id="W6ABC")
            rc = sf.main(["--prediction", pred, ds,
                          "--out", str(Path(tmp) / "body.json")])
        self.assertEqual(rc, 2)

    def test_send_without_a_token_refuses_before_posting(self):
        def _explode(*a, **k):
            raise AssertionError("must not post without a token")
        original, sf.post = sf.post, _explode
        saved = os.environ.pop("SUMMITRFI_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pred, ds = self._setup(tmp)
                rc = sf.main(["--prediction", pred, ds, "--send",
                              "--out", str(Path(tmp) / "body.json")])
                self.assertEqual(rc, 4)
        finally:
            sf.post = original
            if saved is not None:
                os.environ["SUMMITRFI_TOKEN"] = saved

    def test_a_suspect_intermod_band_blocks_the_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            pred, ds = self._setup(tmp)
            im = Path(tmp) / "im.json"
            im.write_text(json.dumps({"steps": [
                {"band": "vhf_lmr_low", "channels_assessable": 8,
                 "channels_suspect": 3, "channels_unexplained": 0,
                 "channels_unstable": 0}]}), encoding="utf-8")
            rc = sf.main(["--prediction", pred, ds,
                          "--intermod-report", str(im),
                          "--out", str(Path(tmp) / "body.json")])
            self.assertEqual(rc, 3)

            rc = sf.main(["--prediction", pred, ds,
                          "--intermod-report", str(im),
                          "--allow-suspect-intermod",
                          "--out", str(Path(tmp) / "body.json")])
            self.assertEqual(rc, 0)

    def test_a_clean_intermod_report_sets_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            pred, ds = self._setup(tmp)
            im = Path(tmp) / "im.json"
            im.write_text(json.dumps({"steps": [
                {"band": "vhf_lmr_low", "channels_assessable": 8,
                 "channels_suspect": 0, "channels_unexplained": 0,
                 "channels_unstable": 0}]}), encoding="utf-8")
            out = Path(tmp) / "body.json"
            sf.main(["--prediction", pred, ds, "--intermod-report", str(im),
                     "--out", str(out)])
            body = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(body["acquisition"]["intermod_checked"])


class TestTokenHandling(unittest.TestCase):

    def test_there_is_no_token_argument_to_accept(self):
        """It would land in shell history. Tested by offering one and
        requiring the parser to reject it, rather than by matching the
        help text, which legitimately mentions the name while explaining
        why there is no such flag."""
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                sf.main(["--token", "secret", "--prediction", "p.json",
                         "d.json.gz"])
        self.assertEqual(cm.exception.code, 2)

    def test_the_help_says_where_the_token_comes_from(self):
        """An operator who cannot find out how to authenticate will put it
        somewhere worse."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                sf.main(["--help"])
        self.assertIn("SUMMITRFI_TOKEN", buf.getvalue())

    def test_the_endpoint_is_https(self):
        self.assertTrue(sf.ENDPOINT.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
