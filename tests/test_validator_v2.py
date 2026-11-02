"""The /2 validator: the dBm floor, the three-way split, the FM residual.

The FM residual is the only test in this file that DISCRIMINATES. The
others make the predicted-but-silent list honest; they do not add evidence
for or against the model. That distinction is the point of the whole
exercise, so the tests are organised by it.
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validate = _load("validate_occupancy")


def _row(freq_hz=155e6, *, occupancy=0.0, role="target", matched=True,
         p_dbm=None, err=None, proxy=1e-5, path=None, excess=None,
         cont="intermittent", service="PW", owner="owner", band="vhf_lmr_low"):
    """One joined row, as validate.join() would produce it."""
    return {"freq_hz": freq_hz, "band": band, "band_role": role,
            "matched": matched, "occupancy": occupancy, "n_decided": 1000,
            "continuity": cont, "service": service, "owner": owner,
            "expected_duty": None, "continuity_basis": "architectural",
            "distance_m": 5000.0, "erp_w": 100.0,
            "field_proxy_w_per_m2": proxy,
            "p_dbm_ref": p_dbm, "error_db": err, "path": path,
            "excess_db_p50": excess, "excess_db_p90": excess}


def _ctrl(freq_hz, *, p_dbm, occupancy=1.0, **kw):
    kw.setdefault("proxy", 1e-3)
    return _row(freq_hz, role="control", band="bcast_fm", occupancy=occupancy,
                p_dbm=p_dbm, cont="continuous", service="FM", **kw)


def _conditions():
    return {"sites": ["s1"], "intervals": 1, "grid_square": "CM87",
            "first_utc": "2026-11-01T12:00:00+00:00",
            "last_utc": "2026-11-01T13:00:00+00:00",
            "baseline_percentile": 25.0, "dwell_s": 20.0,
            "integration_s": 0.1, "revisit_s": 287.0, "min_burst_s": 0.025,
            "receiver_model": "RSPdx", "antenna": "discone", "warnings": []}


# ------------------------------------------------------------------ floor #

class TestAudibilityFloor(unittest.TestCase):

    def test_dbm_when_the_prediction_carries_levels(self):
        rows = [_ctrl(98.1e6, p_dbm=-31.0), _ctrl(104.5e6, p_dbm=-48.0)]
        floor = validate.audibility_floor(rows)
        self.assertEqual(floor.kind, "dbm")
        self.assertEqual(floor.value, -48.0)       # the WEAKEST one heard
        self.assertEqual(floor.n_controls, 2)
        self.assertEqual(floor.unit, "dBm")

    def test_proxy_when_the_prediction_has_no_levels(self):
        rows = [_ctrl(98.1e6, p_dbm=None, proxy=4e-3),
                _ctrl(104.5e6, p_dbm=None, proxy=9e-4)]
        floor = validate.audibility_floor(rows)
        self.assertEqual(floor.kind, "proxy")
        self.assertAlmostEqual(floor.value, 9e-4)

    def test_dbm_is_preferred_when_both_are_available(self):
        """A /2 file has both; the dBm floor is the less frequency-blind."""
        rows = [_ctrl(98.1e6, p_dbm=-31.0, proxy=4e-3)]
        self.assertEqual(validate.audibility_floor(rows).kind, "dbm")

    def test_none_when_no_control_was_heard(self):
        rows = [_ctrl(98.1e6, p_dbm=-31.0, occupancy=0.0),
                _row(155e6, occupancy=0.9)]
        floor = validate.audibility_floor(rows)
        self.assertEqual(floor.kind, "none")
        self.assertFalse(floor.available)
        self.assertIsNone(floor.value)

    def test_a_barely_heard_control_does_not_set_the_floor(self):
        """Above 50% is the bar: a control heard a tenth of the time is not
        evidence that this path hears its level reliably."""
        rows = [_ctrl(98.1e6, p_dbm=-31.0, occupancy=1.0),
                _ctrl(104.5e6, p_dbm=-70.0, occupancy=0.1)]
        self.assertEqual(validate.audibility_floor(rows).value, -31.0)

    def test_a_target_channel_never_sets_the_floor(self):
        rows = [_row(155e6, occupancy=1.0, p_dbm=-90.0, role="target")]
        self.assertEqual(validate.audibility_floor(rows).kind, "none")

    def test_describe_names_the_units_and_the_control_count(self):
        rows = [_ctrl(98.1e6, p_dbm=-31.0)]
        text = validate.audibility_floor(rows).describe()
        self.assertIn("dBm", text)
        self.assertIn("1 control", text)

    def test_the_legacy_proxy_accessor_is_unchanged(self):
        """empirical_audibility_floor stays proxy-only: the figure's left
        panel has a proxy x axis and a dBm floor cannot be drawn on it."""
        rows = [_ctrl(98.1e6, p_dbm=-31.0, proxy=4e-3)]
        self.assertEqual(validate.empirical_audibility_floor(rows), (4e-3, 1))


# --------------------------------------------------------- three-way split #

class TestClassifySilent(unittest.TestCase):

    FLOOR = -60.0

    def _with_floor(self, *silent):
        return [_ctrl(98.1e6, p_dbm=self.FLOOR), *silent]

    def test_above_the_floor_beyond_the_error_band_is_an_anomaly(self):
        rows = self._with_floor(_row(155e6, p_dbm=-40.0, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(len(out[validate.SILENT_ANOMALY]), 1)
        self.assertAlmostEqual(
            out[validate.SILENT_ANOMALY][0]["margin_db"], 20.0)

    def test_inside_the_error_band_is_undetermined(self):
        """The whole reason for a third class: a two-way split calls this
        an anomaly, when the model's own error band says it cannot tell."""
        rows = self._with_floor(_row(155e6, p_dbm=-55.0, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(len(out[validate.SILENT_UNDETERMINED]), 1)
        self.assertEqual(out[validate.SILENT_ANOMALY], [])

    def test_below_the_floor_beyond_the_band_is_expected_silence(self):
        rows = self._with_floor(_row(155e6, p_dbm=-90.0, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(len(out[validate.SILENT_EXPECTED]), 1)

    def test_the_band_is_the_rows_own_error_not_a_constant(self):
        """A diffracted path carries a wider band, so the same margin is an
        anomaly on one row and undetermined on another."""
        rows = self._with_floor(
            _row(155e6, p_dbm=-49.0, err=8.0, path="los"),
            _row(158e6, p_dbm=-49.0, err=14.0, path="diffracted"))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual([r["freq_hz"] for r in out[validate.SILENT_ANOMALY]],
                         [155e6])
        self.assertEqual(
            [r["freq_hz"] for r in out[validate.SILENT_UNDETERMINED]],
            [158e6])

    def test_exactly_at_the_band_edge_is_undetermined(self):
        """Inclusive band: the edge is not yet a finding."""
        rows = self._with_floor(_row(155e6, p_dbm=-52.0, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(len(out[validate.SILENT_UNDETERMINED]), 1)

    def test_a_missing_error_band_is_undetermined_not_an_anomaly(self):
        """error_db is a carried field, so it can be absent. Calling the
        margin significant without it would manufacture a finding."""
        rows = self._with_floor(_row(155e6, p_dbm=-20.0, err=None))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(out[validate.SILENT_ANOMALY], [])
        self.assertEqual(len(out[validate.SILENT_UNDETERMINED]), 1)
        self.assertIsNone(out[validate.SILENT_UNDETERMINED][0]["margin_db"])

    def test_a_missing_level_is_undetermined(self):
        rows = self._with_floor(_row(155e6, p_dbm=None, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(len(out[validate.SILENT_UNDETERMINED]), 1)

    def test_a_proxy_floor_gives_a_two_way_split(self):
        """No error band exists in a /1 prediction, so there is no middle
        class to put anything in."""
        rows = [_ctrl(98.1e6, p_dbm=None, proxy=1e-3),
                _row(155e6, p_dbm=None, proxy=9e-3),
                _row(156e6, p_dbm=None, proxy=1e-7)]
        floor = validate.audibility_floor(rows)
        self.assertEqual(floor.kind, "proxy")
        out = validate.classify_silent(rows, floor)
        self.assertEqual(len(out[validate.SILENT_ANOMALY]), 1)
        self.assertEqual(len(out[validate.SILENT_EXPECTED]), 1)
        self.assertEqual(out[validate.SILENT_UNDETERMINED], [])

    def test_no_floor_puts_everything_in_the_unfiltered_class(self):
        rows = [_row(155e6, p_dbm=-20.0, err=8.0),
                _row(156e6, p_dbm=-120.0, err=8.0)]
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(len(out[validate.SILENT_UNFILTERED]), 2)
        self.assertEqual(out[validate.SILENT_ANOMALY], [])
        self.assertEqual(out[validate.SILENT_EXPECTED], [])

    def test_an_active_channel_is_in_no_silent_class(self):
        rows = self._with_floor(_row(155e6, occupancy=0.3, p_dbm=-20.0,
                                     err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(sum(len(v) for v in out.values()), 0)

    def test_an_unmatched_prediction_is_in_no_silent_class(self):
        """Never measured at all is not the same as measured and silent."""
        rows = self._with_floor(_row(155e6, matched=False, occupancy=None,
                                     p_dbm=-20.0, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        self.assertEqual(sum(len(v) for v in out.values()), 0)

    def test_anomalies_are_sorted_worst_first(self):
        rows = self._with_floor(_row(155e6, p_dbm=-40.0, err=8.0),
                                _row(156e6, p_dbm=-20.0, err=8.0))
        out = validate.classify_silent(rows, validate.audibility_floor(rows))
        margins = [r["margin_db"] for r in out[validate.SILENT_ANOMALY]]
        self.assertEqual(margins, sorted(margins, reverse=True))

    def test_every_class_has_a_stated_meaning(self):
        for cls in (validate.SILENT_ANOMALY, validate.SILENT_UNDETERMINED,
                    validate.SILENT_EXPECTED, validate.SILENT_UNFILTERED):
            self.assertIn(cls, validate.SILENT_MEANING)
            self.assertTrue(validate.SILENT_MEANING[cls])


# ------------------------------------------------- the discriminating test #

class TestFmPathResidual(unittest.TestCase):
    """Measured excess minus predicted level, by path class."""

    def _controls(self, los, diffracted, *, offset=70.0):
        """Controls whose residual is `offset` plus the listed wobble."""
        rows = []
        f = 88.1e6
        for i, w in enumerate(los):
            rows.append(_ctrl(f + i * 2e5, p_dbm=-40.0, path="los",
                              excess=-40.0 + offset + w))
        for i, w in enumerate(diffracted):
            rows.append(_ctrl(f + 1e7 + i * 2e5, p_dbm=-60.0,
                              path="diffracted", excess=-60.0 + offset + w))
        return rows

    def test_cannot_run_without_two_path_classes(self):
        out = validate.fm_path_residual(self._controls([0, 0, 0], []))
        self.assertFalse(out["runnable"])
        self.assertIn("diffracted class is often empty", out["reason"])

    def test_cannot_run_with_too_few_in_a_class(self):
        out = validate.fm_path_residual(self._controls([0, 0, 0], [0]))
        self.assertFalse(out["runnable"])
        self.assertEqual(out["classes"]["diffracted"]["n"], 1)

    def test_cannot_run_without_predicted_levels(self):
        """A /1 prediction carries no p_dbm_ref, so there is no residual."""
        rows = [_ctrl(98.1e6 + i * 2e5, p_dbm=None, path="los", excess=30.0)
                for i in range(4)]
        out = validate.fm_path_residual(rows)
        self.assertFalse(out["runnable"])
        self.assertIn("p_dbm_ref", out["reason"])

    def test_agreeing_classes_give_a_gap_inside_the_scatter(self):
        """If the diffraction model is right the classes share the offset,
        so the gap is small against the within-class spread."""
        out = validate.fm_path_residual(
            self._controls([-1, 0, 1, 0], [0, 1, -1, 0]))
        self.assertTrue(out["runnable"])
        self.assertAlmostEqual(out["gap_db"], 0.0, places=6)
        self.assertFalse(out["gap_exceeds_spread"])

    def test_a_systematic_gap_is_reported_and_exceeds_the_scatter(self):
        """Diffracted carriers measured 12 dB hotter than predicted
        relative to LOS: diffraction loss was overestimated."""
        out = validate.fm_path_residual(
            self._controls([-1, 0, 1, 0], [11, 12, 13, 12]))
        self.assertTrue(out["runnable"])
        self.assertAlmostEqual(out["gap_db"], 12.0, places=6)
        self.assertTrue(out["gap_exceeds_spread"])

    def test_the_gap_sign_is_diffracted_minus_los(self):
        out = validate.fm_path_residual(
            self._controls([0, 0, 0], [-9, -9, -9]))
        self.assertLess(out["gap_db"], 0.0)

    def test_the_absolute_offset_does_not_change_the_gap(self):
        """The offset is the receive chain's unknown constant. If it moved
        the verdict, the test would be measuring the feedline."""
        a = validate.fm_path_residual(
            self._controls([0, 0, 0], [6, 6, 6], offset=70.0))
        b = validate.fm_path_residual(
            self._controls([0, 0, 0], [6, 6, 6], offset=-130.0))
        self.assertAlmostEqual(a["gap_db"], b["gap_db"])

    def test_target_channels_are_excluded(self):
        """On an intermittent channel the excess median is the noise floor,
        so its residual would measure duty cycle and be read as path
        error."""
        rows = self._controls([0, 0, 0], [0, 0, 0])
        rows += [_row(155e6 + i * 1e5, occupancy=0.2, p_dbm=-50.0,
                      path="los", excess=5.0, role="target")
                 for i in range(20)]
        out = validate.fm_path_residual(rows)
        self.assertEqual(out["n_usable"], 6)

    def test_a_control_that_was_not_continuously_on_is_excluded(self):
        rows = self._controls([0, 0, 0], [0, 0, 0])
        rows.append(_ctrl(107.9e6, p_dbm=-50.0, occupancy=0.2, path="los",
                          excess=99.0))
        out = validate.fm_path_residual(rows)
        self.assertEqual(out["n_usable"], 6)

    def test_rows_without_a_path_class_are_excluded(self):
        rows = self._controls([0, 0, 0], [0, 0, 0])
        rows.append(_ctrl(107.9e6, p_dbm=-50.0, path=None, excess=10.0))
        self.assertEqual(validate.fm_path_residual(rows)["n_usable"], 6)


# ------------------------------------------------------------ provenance #

class TestProvenance(unittest.TestCase):

    def test_names_the_engine_and_the_data_date(self):
        line = validate._provenance_line({
            "schema": "occupancy-survey/qth-prediction/2",
            "model": {"engine": "node-abc1234567", "data": "2026-11-01",
                      "dem": 'SRTM 2"'}})
        self.assertIn("node-abc1234567", line)
        self.assertIn("2026-11-01", line)
        self.assertIn('SRTM 2"', line)

    def test_says_so_when_there_is_no_model_header(self):
        line = validate._provenance_line(
            {"schema": "occupancy-survey/qth-prediction/1"})
        self.assertIn("no model header", line)
        self.assertIn("ERP/d^2", line)


class TestIntermodReports(unittest.TestCase):

    def _write(self, steps):
        d = Path(tempfile.mkdtemp())
        p = d / "im.json"
        p.write_text(json.dumps(
            {"schema": "occupancy-survey/intermod-check/1", "steps": steps}),
            encoding="utf-8")
        return p

    def test_absent_is_recorded_as_not_supplied(self):
        self.assertFalse(validate.load_intermod_reports([])["supplied"])
        self.assertFalse(validate.load_intermod_reports(None)["supplied"])

    def test_counts_are_summed_per_band_across_steps(self):
        p = self._write([
            {"band": "vhf_lmr_low", "channels_assessable": 8,
             "channels_suspect": 3, "channels_unexplained": 1,
             "channels_unstable": 0},
            {"band": "vhf_lmr_low", "channels_assessable": 6,
             "channels_suspect": 0, "channels_unexplained": 2,
             "channels_unstable": 1},
            {"band": "bcast_fm", "channels_assessable": 4,
             "channels_suspect": 0, "channels_unexplained": 0,
             "channels_unstable": 0}])
        out = validate.load_intermod_reports([p])
        self.assertTrue(out["supplied"])
        low = out["bands"]["vhf_lmr_low"]
        self.assertEqual((low["steps"], low["assessable"], low["suspect"],
                          low["unexplained"], low["unstable"]),
                         (2, 14, 3, 3, 1))
        self.assertEqual(out["bands"]["bcast_fm"]["steps"], 1)


# ---------------------------------------------------------- report wiring #

class TestReportText(unittest.TestCase):

    def _report(self, rows, **kw):
        return validate.report(rows, [], {}, _conditions(), 10.0, **kw)

    def test_a_dbm_floor_announces_a_three_way_split(self):
        rows = [_ctrl(98.1e6, p_dbm=-60.0),
                _row(155e6, p_dbm=-40.0, err=8.0),
                _row(156e6, p_dbm=-58.0, err=8.0),
                _row(157e6, p_dbm=-99.0, err=8.0)]
        text = self._report(rows)
        self.assertIn("three-way", text)
        self.assertIn("ANOMALY: 1", text)
        self.assertIn("UNDETERMINED: 1", text)
        self.assertIn("EXPECTED: 1", text)

    def test_a_proxy_floor_says_it_is_two_way_and_why(self):
        rows = [_ctrl(98.1e6, p_dbm=None, proxy=1e-3),
                _row(155e6, p_dbm=None, proxy=9e-3)]
        text = self._report(rows)
        self.assertIn("two-way", text)
        self.assertIn("no error band in a /1 prediction", text)

    def test_the_dbm_floor_is_not_claimed_to_be_frequency_correct(self):
        rows = [_ctrl(98.1e6, p_dbm=-60.0)]
        text = self._report(rows)
        self.assertIn("NOT frequency-correct", text)
        self.assertIn("reference gain", text)

    def test_a_missing_intermod_check_is_stated_with_its_direction(self):
        text = self._report([_ctrl(98.1e6, p_dbm=-60.0)])
        self.assertIn("NO INTERMOD CHECK SUPPLIED", text)
        self.assertIn("FLATTERS", text)

    def test_a_supplied_check_lists_bands_and_flags_the_unchecked(self):
        rows = [_ctrl(98.1e6, p_dbm=-60.0),
                _row(155e6, occupancy=0.4, p_dbm=-40.0, err=8.0)]
        intermod = {"supplied": True, "bands": {
            "bcast_fm": {"steps": 1, "assessable": 4, "suspect": 0,
                         "unexplained": 0, "unstable": 0}}}
        text = self._report(rows, intermod=intermod)
        self.assertIn("bcast_fm", text)
        self.assertIn("NOT CHECKED: vhf_lmr_low", text)

    def test_a_suspect_band_is_not_called_clean(self):
        intermod = {"supplied": True, "bands": {
            "bcast_fm": {"steps": 1, "assessable": 8, "suspect": 3,
                         "unexplained": 0, "unstable": 0}}}
        text = self._report([_ctrl(98.1e6, p_dbm=-60.0)],
                            intermod=intermod)
        self.assertIn("NOT shown clean", text)

    def test_the_provenance_warns_against_pooling_engines(self):
        text = self._report([_ctrl(98.1e6, p_dbm=-60.0)], prediction={
            "schema": "occupancy-survey/qth-prediction/2",
            "model": {"engine": "node-abc1234567"}})
        self.assertIn("node-abc1234567", text)
        self.assertIn("must", text)
        self.assertIn("not be pooled", text)

    def test_an_unrunnable_residual_says_cannot_run_not_a_number(self):
        text = self._report([_ctrl(98.1e6, p_dbm=-60.0, path="los",
                                   excess=10.0)])
        self.assertIn("CANNOT RUN", text)
        self.assertNotIn("nan", text.lower())

    def test_the_absolute_offset_is_said_to_mean_nothing(self):
        text = self._report([_ctrl(98.1e6, p_dbm=-60.0)])
        self.assertIn("means NOTHING", text)

    def test_the_old_five_argument_call_still_works(self):
        """Existing callers pass no prediction and no intermod report."""
        text = validate.report([], [], {}, _conditions(), 10.0)
        self.assertIn("QTH OCCUPANCY VALIDATION", text)


class TestPlotSmoke(unittest.TestCase):

    def test_a_v2_figure_renders_with_all_three_silent_classes(self):
        rows = [_ctrl(98.1e6, p_dbm=-60.0),
                _row(155e6, p_dbm=-40.0, err=8.0),
                _row(156e6, p_dbm=-58.0, err=8.0),
                _row(157e6, p_dbm=-99.0, err=8.0),
                _row(158e6, occupancy=0.4, p_dbm=-45.0, err=8.0)]
        with tempfile.TemporaryDirectory() as d:
            out = validate.plot(
                rows, [], _conditions(), 10.0, Path(d) / "f.png",
                prediction={"schema": "occupancy-survey/qth-prediction/2",
                            "model": {"engine": "node-abc1234567"}},
                intermod={"supplied": False, "bands": {}})
            self.assertTrue(Path(out).stat().st_size > 1000)


# ------------------------------------------------- the histogram percentile #

class TestExcessPercentile(unittest.TestCase):

    def test_a_single_populated_bin_returns_its_centre(self):
        from occupancy_survey.presence import percentile_from_hist
        self.assertAlmostEqual(
            percentile_from_hist([0, 0, 10, 0], 0, 0, 0.0, 1.0, 50.0), 2.5)

    def test_a_percentile_in_the_underflow_returns_the_bottom_edge(self):
        """A bound reported as a value: the caller must compare it against
        lo_db rather than reading it as a measurement."""
        from occupancy_survey.presence import percentile_from_hist
        self.assertAlmostEqual(
            percentile_from_hist([0, 0, 10], 90, 0, -5.0, 1.0, 50.0), -5.0)

    def test_a_percentile_in_the_overflow_returns_the_top_edge(self):
        from occupancy_survey.presence import percentile_from_hist
        self.assertAlmostEqual(
            percentile_from_hist([10, 0, 0], 0, 90, 0.0, 2.0, 50.0), 6.0)

    def test_no_counts_gives_none_not_zero(self):
        from occupancy_survey.presence import percentile_from_hist
        self.assertIsNone(percentile_from_hist([], 0, 0, 0.0, 1.0, 50.0))
        self.assertIsNone(
            percentile_from_hist([0, 0, 0], 0, 0, 0.0, 1.0, 50.0))

    def test_merged_channel_method_agrees_with_the_helper(self):
        from occupancy_survey.dataset import MergedChannel
        from occupancy_survey.presence import percentile_from_hist
        m = MergedChannel(
            freq_hz=155e6, service="PW",
            hist=np.array([1, 2, 7, 0], dtype=np.int64), underflow=0,
            overflow=0, n_decided=10, sites={"s1"}, bins_per_channel={4},
            hist_lo_db=0.0, hist_bin_db=1.0)
        for pct in (10.0, 50.0, 90.0):
            self.assertAlmostEqual(
                m.excess_db_percentile(pct),
                percentile_from_hist(m.hist, 0, 0, 0.0, 1.0, pct))

    def test_the_private_record_form_still_works(self):
        from occupancy_survey.presence import _percentile_from_hist
        self.assertAlmostEqual(
            _percentile_from_hist({"hist": [0, 0, 10, 0], "underflow": 0,
                                   "overflow": 0},
                                  {"lo_db": 0.0, "bin_db": 1.0}, 50.0), 2.5)


if __name__ == "__main__":
    unittest.main()
