"""Capturing the SDRplay API's overload reports off fd 2.

These messages are the authoritative overload signal and the only one that
exists for this driver. The clip counter in the GNU Radio backend saw none
of nine reported overloads in one run, because the IF attenuator sits
downstream of the stage that compresses.
"""

import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from occupancy_survey.backends.base import Backend
from occupancy_survey.overload_watch import (GR_LOG_ENV, GnuRadioLogCapture,
                                             OverloadWatcher)
from occupancy_survey.preflight import Preflight, _check_overload_reports


def _watcher():
    """A watcher on a non-console stderr, so the counting tests behave the
    same from a Windows console as from a redirected run -- on a console
    the real watcher rightly declines to start."""
    return OverloadWatcher(stderr=io.StringIO())


DETECTED =b"rspdx :warning: overload detected - please reduce gain\n"
CORRECTED = b"rspdx :warning: overload corrected\n"


class TestCounting(unittest.TestCase):

    def _run(self, payload: bytes) -> dict:
        w = _watcher().start()
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
        w = _watcher().start()
        first = w.stop()
        self.assertEqual(w.stop(), first)

    def test_fd_2_is_restored(self):
        """A diagnostic that leaks a redirect would break every later run."""
        before = os.fstat(2)
        _watcher().start().stop()
        after = os.fstat(2)
        self.assertEqual((before.st_dev, before.st_ino),
                         (after.st_dev, after.st_ino))

    def test_unrelated_stderr_is_passed_through_not_swallowed(self):
        r, w_fd = os.pipe()
        saved = os.dup(2)
        try:
            os.dup2(w_fd, 2)
            watcher = _watcher().start()
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


class TestWindowsConsole(unittest.TestCase):
    """Redirecting fd 2 under a Windows console broke every log line.

    Measured 2026-10-31: `--- Logging error ---` in place of each message,
    from `OSError: [WinError 1] Incorrect function`, for as long as the
    watcher ran -- which is a backend's whole lifetime.
    """

    class _WindowsConsoleIO:
        pass

    class _Stderr:
        def __init__(self, raw):
            self.buffer = type("B", (), {"raw": raw})()

    def _console_watcher(self):
        return OverloadWatcher(stderr=self._Stderr(self._WindowsConsoleIO()))

    def test_fd_2_is_not_touched(self):
        before = os.fstat(2)
        w = self._console_watcher().start()
        self.assertIsNone(w._saved_fd)
        w.stop()
        after = os.fstat(2)
        self.assertEqual((before.st_dev, before.st_ino),
                         (after.st_dev, after.st_ino))

    def test_it_says_not_observed_rather_than_zero(self):
        w = self._console_watcher().start()
        c = w.stop()
        self.assertFalse(c["supported"])
        self.assertIn("Windows console", c["note"])
        self.assertEqual(w.as_metadata()["verdict"], "not_observed")


class TestMetadata(unittest.TestCase):

    def test_an_overloaded_run_is_labelled_overloaded(self):
        w = _watcher().start()
        os.write(2, DETECTED)
        w.stop()
        m = w.as_metadata()
        self.assertEqual(m["verdict"], "overloaded")
        self.assertIn("RAISE", m["interpretation"],
                      "the direction of the bias must be in the dataset")

    def test_a_clean_run_is_not_labelled_the_same_as_unobserved(self):
        w = _watcher().start()
        w.stop()
        self.assertEqual(w.as_metadata()["verdict"], "none_captured")

    def test_a_zero_does_not_read_as_a_healthy_front_end(self):
        """Measured: 0 captured while 3,321 reports reached the same log.
        The first wording, "no_overload_reported", read as a clean bill of
        health beside them."""
        w = _watcher().start()
        w.stop()
        m = w.as_metadata()
        self.assertNotIn("no_overload", m["verdict"])
        self.assertIn("NOT EVIDENCE", m["interpretation"])

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

    def test_a_clean_sweep_is_a_warning_not_a_pass(self):
        """Zero captured is not zero occurred.

        Measured 2026-11-01: a run printed `overload corrected` to the
        console with the watcher running from before the device opened, and
        the count came back zero. Reporting that as [ok] put a green check
        under a visible warning for the second time.
        """
        c = self._check({"supported": True, "overload_detected": 0,
                         "overload_corrected": 0, "overload_events": 0})
        self.assertIsNone(c.ok)
        self.assertIn("gain lock", c.detail)

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
        w = _watcher().start()
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
        w = _watcher().start()
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

    def test_zero_events_does_not_pass(self):
        self.assertIsNone(self._check({"supported": True,
                                       "overload_events": 0}).ok)


class TestGnuRadioLogCapture(unittest.TestCase):
    """The reports are GNU Radio log lines; capture them where they start.

    Found 2026-11-02: radioconda's GNU Radio logs to STDOUT, through a
    spdlog sink that took the Win32 handle when logging started, so no fd 2
    capture could ever see an RSPdx report. The pipe here stands in for the
    Win32 std handle install() redirects.
    """

    def setUp(self):
        GnuRadioLogCapture._reset_for_tests()
        self._env = os.environ.get(GR_LOG_ENV)
        self._dir = tempfile.TemporaryDirectory()
        self._fds = []

    def tearDown(self):
        for fd in self._fds:                # EOF ends the capture thread
            try:
                os.close(fd)
            except OSError:
                pass
        GnuRadioLogCapture._reset_for_tests()
        if self._env is None:
            os.environ.pop(GR_LOG_ENV, None)
        else:
            os.environ[GR_LOG_ENV] = self._env
        self._dir.cleanup()

    def _install(self):
        def fake_handle():
            r, w = os.pipe()
            self._fds.append(w)
            return r, w
        log = Path(self._dir.name) / "logs" / "gnuradio_test.log"
        with mock.patch.dict(sys.modules):
            sys.modules.pop("gnuradio.gr", None)
            cap = GnuRadioLogCapture.install(log, platform="win32",
                                             set_std_handle=fake_handle)
        return cap, self._fds[-1]

    def _wait_for(self, cond):
        deadline = time.time() + 2.0
        while time.time() < deadline and not cond():
            time.sleep(0.02)
        self.assertTrue(cond(), "capture thread did not deliver in time")

    def test_gnuradio_is_told_to_log_to_stderr(self):
        self._install()
        self.assertEqual(os.environ[GR_LOG_ENV], "stderr")

    def test_off_windows_it_only_sets_the_route(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop("gnuradio.gr", None)
            self.assertIsNone(GnuRadioLogCapture.install(
                Path(self._dir.name) / "x.log", platform="linux"))
        self.assertEqual(os.environ[GR_LOG_ENV], "stderr")

    def test_too_late_after_gnuradio_is_imported(self):
        with mock.patch.dict(sys.modules, {"gnuradio.gr": object()}):
            self.assertIsNone(GnuRadioLogCapture.install(
                Path(self._dir.name) / "x.log", platform="win32",
                set_std_handle=lambda: self.fail("must not redirect")))

    def test_reports_are_counted_and_supported_on_a_console(self):
        cap, w = self._install()
        watcher = OverloadWatcher(capture=cap).start()
        os.write(w, DETECTED + CORRECTED)
        self._wait_for(lambda: watcher.counts["overload_corrected"] == 1)
        c = watcher.stop()
        self.assertEqual(c["overload_events"], 1)
        self.assertTrue(c["supported"])
        self.assertEqual(c["route"], "gnuradio_log")

    def test_lines_go_to_a_timestamped_file_not_the_console(self):
        cap, w = self._install()
        watcher = OverloadWatcher(capture=cap).start()
        os.write(w, b"rspdx :info: total samples: [1,0]\r\n" + DETECTED)
        self._wait_for(lambda: watcher.counts["overload_detected"] == 1)
        watcher.stop()
        text = Path(cap.log_path).read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}"
                                   r"\+00:00 rspdx :warning: overload detected")
        self.assertIn("[startup]", lines[1])
        self.assertNotIn("\r", text)

    def _phase_after(self, seconds, last="retune"):
        cap, w = self._install()
        t_change = time.time() - seconds
        ctx = {"band": "uhf_lmr", "center_hz": 463.856375e6,
               "retune": t_change if last == "retune" else t_change - 60,
               "gain": t_change - 60 if last == "retune" else t_change}
        watcher = OverloadWatcher(capture=cap, context=lambda: ctx).start()
        os.write(w, DETECTED)
        self._wait_for(lambda: watcher.counts["overload_detected"] == 1)
        watcher.stop()
        return watcher.as_metadata()

    def test_an_overload_just_after_a_retune_is_settling(self):
        m = self._phase_after(0.5)
        e = m["recent_events"][0]
        self.assertEqual(e["phase"], "settling")
        self.assertEqual(e["band"], "uhf_lmr")
        self.assertAlmostEqual(e["center_mhz"], 463.856375)
        self.assertEqual(e["last_change"], "retune")
        self.assertEqual(m["by_phase"], {"settling": 1})
        self.assertEqual(m["by_band"], {"uhf_lmr": 1})

    def test_an_overload_mid_dwell_is_dwell(self):
        """The kind that biases a duty cycle."""
        e = self._phase_after(10.0)["recent_events"][0]
        self.assertEqual(e["phase"], "dwell")

    def test_a_gain_change_counts_as_a_change(self):
        e = self._phase_after(0.3, last="gain")["recent_events"][0]
        self.assertEqual((e["phase"], e["last_change"]), ("settling", "gain"))

    def test_metadata_names_the_log_without_its_directory(self):
        """Datasets go to volunteers; an absolute path carries the account
        name (CLAUDE.md, minimum disclosure)."""
        cap, w = self._install()
        m = OverloadWatcher(capture=cap).start().as_metadata()
        self.assertEqual(m["log_file"], "gnuradio_test.log")
        self.assertEqual(m["verdict"], "none_captured")
        self.assertIn("GNU Radio's log", m["interpretation"])

    def test_the_watcher_finds_an_installed_capture_by_itself(self):
        """The backend constructs OverloadWatcher() with no capture
        argument; it must still use the capture __main__ installed."""
        cap, _ = self._install()
        self.assertEqual(OverloadWatcher().counts["route"], "gnuradio_log")


if __name__ == "__main__":
    unittest.main()
