"""Capturing the SDRplay API's overload reports off fd 2.

These messages are the authoritative overload signal and the only one that
exists for this driver. The clip counter in the GNU Radio backend saw none
of nine reported overloads in one run, because the IF attenuator sits
downstream of the stage that compresses.
"""

import os
import sys
import unittest

from occupancy_survey.backends.base import Backend
from occupancy_survey.overload_watch import OverloadWatcher
from occupancy_survey.preflight import Preflight, _check_overload_reports

DETECTED = b"rspdx :warning: overload detected - please reduce gain\n"
CORRECTED = b"rspdx :warning: overload corrected\n"


class TestCounting(unittest.TestCase):

    def _run(self, payload: bytes) -> dict:
        w = OverloadWatcher().start()
        try:
            os.write(2, payload)
        finally:
            return w.stop()

    def test_a_detected_report_is_counted(self):
        self.assertEqual(self._run(DETECTED)["overload_detected"], 1)

    def test_the_observed_run_of_nine_is_counted_as_nine(self):
        """The real case: nine detect/correct pairs in one --check."""
        c = self._run((DETECTED + CORRECTED) * 9 + CORRECTED)
        self.assertEqual(c["overload_detected"], 9)
        self.assertEqual(c["overload_corrected"], 10)

    def test_a_clean_run_counts_zero(self):
        c = self._run(b"rspdx :info: total samples: [4216464,0]\n")
        self.assertEqual(c["overload_detected"], 0)

    def test_unterminated_output_is_still_counted(self):
        """The API's last line may arrive without its newline."""
        self.assertEqual(
            self._run(DETECTED.rstrip(b"\n"))["overload_detected"], 1)

    def test_stop_is_idempotent(self):
        w = OverloadWatcher().start()
        first = w.stop()
        self.assertEqual(w.stop(), first)

    def test_fd_2_is_restored(self):
        """A diagnostic that leaks a redirect would break every later run."""
        before = os.fstat(2)
        OverloadWatcher().start().stop()
        after = os.fstat(2)
        self.assertEqual((before.st_dev, before.st_ino),
                         (after.st_dev, after.st_ino))

    def test_unrelated_stderr_is_passed_through_not_swallowed(self):
        r, w_fd = os.pipe()
        saved = os.dup(2)
        try:
            os.dup2(w_fd, 2)
            watcher = OverloadWatcher().start()
            os.write(2, b"an unrelated diagnostic\n" + DETECTED)
            counts = watcher.stop()
            os.dup2(saved, 2)
            os.close(w_fd)
            seen = os.read(r, 65536)
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(r)
        self.assertIn(b"an unrelated diagnostic", seen,
                      "the watcher must tee, not swallow")
        self.assertIn(b"overload detected", seen)
        self.assertEqual(counts["overload_detected"], 1)


class TestMetadata(unittest.TestCase):

    def test_an_overloaded_run_is_labelled_overloaded(self):
        w = OverloadWatcher().start()
        os.write(2, DETECTED)
        w.stop()
        m = w.as_metadata()
        self.assertEqual(m["verdict"], "overloaded")
        self.assertIn("RAISE", m["interpretation"],
                      "the direction of the bias must be in the dataset")

    def test_a_clean_run_is_not_labelled_the_same_as_unobserved(self):
        w = OverloadWatcher().start()
        w.stop()
        self.assertEqual(w.as_metadata()["verdict"], "no_overload_reported")

    def test_the_base_backend_reports_not_observed_not_zero(self):
        m = Backend.overload_reports(object())
        self.assertFalse(m["supported"])
        self.assertEqual(m["verdict"], "not_observed")


class TestPreflightVerdict(unittest.TestCase):

    def _check(self, reports):
        class B:
            def overload_reports(self):
                return reports
        pf = Preflight()
        _check_overload_reports(pf, B())
        return {c.name: c for c in pf.checks}["front-end overload"]

    def test_overload_fails_and_names_the_right_knob(self):
        # Shape taken from OverloadWatcher.counts, not hand-rolled: a
        # fixture that cannot occur in practice tests nothing, and an
        # earlier version of this one passed while the code was broken.
        c = self._check({"supported": True, "overload_detected": 9,
                         "overload_corrected": 9, "overload_events": 9})
        self.assertFalse(c.ok)
        self.assertIn("9 time(s)", c.detail)
        self.assertIn("RF", c.remedy)

    def test_a_clean_sweep_passes(self):
        self.assertTrue(
            self._check({"supported": True, "overload_detected": 0,
                         "overload_corrected": 0,
                         "overload_events": 0}).ok)

    def test_unobservable_is_a_warning_not_a_pass(self):
        self.assertIsNone(self._check({"supported": False}).ok)


class TestABareCorrectedStillCounts(unittest.TestCase):
    """The API reports an onset from inside its own startup.

    Observed: a --check run printed exactly one `overload corrected`, with
    no `detected`, during create_source() -- before any watcher could be
    listening. Keying the verdict on `detected` alone printed "none reported
    in this sweep" three lines under the warning, which is the same class of
    error the watcher was built to end.
    """

    def _counts(self, payload):
        w = OverloadWatcher().start()
        try:
            os.write(2, payload)
        finally:
            return w.stop()

    def test_corrected_without_detected_is_an_event(self):
        c = self._counts(CORRECTED)
        self.assertEqual(c["overload_detected"], 0)
        self.assertEqual(c["overload_events"], 1)

    def test_the_unseen_onset_is_reported_as_such(self):
        self.assertEqual(self._counts(CORRECTED)["onset_before_watching"], 1)

    def test_matched_pairs_are_counted_once_each(self):
        """Nine detect/correct pairs are nine episodes, not eighteen."""
        c = self._counts((DETECTED + CORRECTED) * 9)
        self.assertEqual(c["overload_events"], 9)
        self.assertNotIn("onset_before_watching", c)

    def test_a_truly_clean_run_has_no_events(self):
        c = self._counts(b"rspdx :info: total samples: [1,0]\n")
        self.assertEqual(c["overload_events"], 0)
        self.assertNotIn("onset_before_watching", c)

    def test_metadata_verdict_follows_events_not_detected(self):
        w = OverloadWatcher().start()
        os.write(2, CORRECTED)
        w.stop()
        self.assertEqual(w.as_metadata()["verdict"], "overloaded")


class TestPreflightCountsEpisodes(unittest.TestCase):

    def _check(self, reports):
        class B:
            def overload_reports(self):
                return reports
        pf = Preflight()
        _check_overload_reports(pf, B())
        return {c.name: c for c in pf.checks}["front-end overload"]

    def test_a_bare_corrected_fails_the_check(self):
        c = self._check({"supported": True, "overload_detected": 0,
                         "overload_corrected": 1, "overload_events": 1,
                         "onset_before_watching": 1})
        self.assertFalse(c.ok, "an overload the API corrected is still an "
                               "overload")
        self.assertIn("before the device was opened", c.detail)

    def test_zero_events_still_passes(self):
        self.assertTrue(self._check({"supported": True,
                                     "overload_events": 0}).ok)


if __name__ == "__main__":
    unittest.main()
