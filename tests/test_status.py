"""The live status display.

Exists because without it an operator starting a run sees NOTHING until
the first interval file is written -- an hour, at the default interval.
That is precisely the window in which they want to know whether the
receiver is working, so the status must read live accumulator state rather
than written files.
"""

import tempfile
import unittest

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import resolve_bands
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter

DESIGN = dict(integration_s=0.01, dwell_s=0.25, interval_s=100000.0,
              usable_bandwidth_hz=6e6)


def _survey(emitters, bands, out_dir, cycles):
    design = SamplingDesign(**DESIGN)
    s = Survey(resolve_bands(bands),
               SyntheticBackend(emitters, integration_s=0.01,
                                sample_rate_hz=6e6),
               SiteMetadata("qth", "RSPdx", "discone"), design,
               out_dir=out_dir, seed=5)
    s.run(cycles=cycles, status_interval_s=0.0)
    return s


class TestStatusBeforeAnyFileIsWritten(unittest.TestCase):

    def test_detections_are_printed_during_the_run(self):
        """The whole point: an hourly interval must not mean an hour blind.

        Captures what the operator actually sees on stdout WHILE the run is
        in progress, with the logging interval set far longer than the run
        so no file is written until it ends.
        """
        import contextlib
        import io

        em = [SyntheticEmitter(151_475_000.0, 26.0, duty=0.4,
                               period_sweeps=50)]
        design = SamplingDesign(**DESIGN)      # interval_s = 100000
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            s = Survey(resolve_bands(["vhf_lmr_low"]),
                       SyntheticBackend(em, integration_s=0.01,
                                        sample_rate_hz=6e6),
                       SiteMetadata("qth", "RSPdx", "discone"), design,
                       out_dir=d, seed=5)
            with contextlib.redirect_stdout(buf):
                # status every 0 s -> on every visit, which is what a long
                # real run shows once a minute.
                s.run(cycles=6, status_interval_s=0.001)
        printed = buf.getvalue()
        self.assertIn("151.4750 MHz", printed,
                      "the detected carrier must appear on stdout during "
                      "the run, not only in a file written at the end")
        self.assertIn("channel(s) with a carrier", printed)

    def test_live_state_is_readable_before_the_interval_closes(self):
        em = [SyntheticEmitter(151_475_000.0, 26.0, duty=0.4,
                               period_sweeps=50)]
        with tempfile.TemporaryDirectory() as d:
            snap = _survey(em, ["vhf_lmr_low"], d, cycles=6).status_snapshot()
        self.assertGreater(snap["n_active"], 0)

    def test_the_strongest_channel_is_listed_first(self):
        em = [SyntheticEmitter(151_475_000.0, 26.0, duty=0.4,
                               period_sweeps=50),
              SyntheticEmitter(155_160_000.0, 22.0, duty=0.1,
                               period_sweeps=80)]
        with tempfile.TemporaryDirectory() as d:
            rows = _survey(em, ["vhf_lmr_low"], d, cycles=8
                           ).status_snapshot()["channels"]
        self.assertGreaterEqual(len(rows), 2)
        self.assertGreaterEqual(rows[0]["occupancy"], rows[-1]["occupancy"])

    def test_occupancy_shown_matches_what_was_injected(self):
        em = [SyntheticEmitter(151_475_000.0, 26.0, duty=0.4,
                               period_sweeps=50)]
        with tempfile.TemporaryDirectory() as d:
            rows = _survey(em, ["vhf_lmr_low"], d, cycles=10
                           ).status_snapshot()["channels"]
        hit = [r for r in rows if abs(r["freq_hz"] - 151_475_000.0) < 1][0]
        self.assertAlmostEqual(hit["occupancy"], 0.4, delta=0.08)


class TestNothingDetected(unittest.TestCase):

    def test_an_empty_band_says_so_and_says_what_to_check(self):
        """Silence is the case an operator most needs explained."""
        with tempfile.TemporaryDirectory() as d:
            text = _survey([], ["nwr"], d, cycles=2).format_status()
        self.assertIn("0 channel(s)", text)
        self.assertIn("nothing detected yet", text)
        for hint in ("antenna", "gain", "threshold"):
            self.assertIn(hint, text)

    def test_it_does_not_claim_a_fault_too_early(self):
        """Early silence is normal; the wording must not send someone
        chasing hardware that is fine."""
        with tempfile.TemporaryDirectory() as d:
            text = _survey([], ["nwr"], d, cycles=2).format_status()
        self.assertIn("normal in the first minutes", text)


class TestFormatting(unittest.TestCase):

    def test_the_control_band_verdict_is_shown_on_its_row(self):
        em = [SyntheticEmitter(101_300_000.0, 34.0, duty=1.0,
                               bandwidth_hz=170_000.0)]
        with tempfile.TemporaryDirectory() as d:
            text = _survey(em, ["bcast_fm"], d, cycles=6).format_status()
        row = [l for l in text.splitlines() if l.strip().startswith("bcast_fm")]
        self.assertTrue(row, "the band must have its own summary row")
        self.assertIn("[pass]", row[0])

    def test_long_lists_are_truncated_with_a_count(self):
        em = [SyntheticEmitter(151_000_000.0 + i * 100_000.0, 26.0, duty=1.0)
              for i in range(12)]
        with tempfile.TemporaryDirectory() as d:
            s = _survey(em, ["vhf_lmr_low"], d, cycles=6)
            text = s.format_status(max_rows=4)
        # Only the indented "busiest channels" rows are capped; the per-band
        # table has one row per band regardless.
        detail = [l for l in text.splitlines()
                  if l.startswith("    ") and "MHz" in l]
        self.assertLessEqual(len(detail), 4)
        self.assertIn("more", text)

    def test_a_band_never_visited_is_not_reported(self):
        with tempfile.TemporaryDirectory() as d:
            snap = _survey([], ["nwr"], d, cycles=1).status_snapshot()
        self.assertEqual(snap["bands_visited"], ["nwr"])


if __name__ == "__main__":
    unittest.main()


class TestPerBandSummary(unittest.TestCase):
    """A row per band, so 'which band is busy' is answerable at a glance."""

    def test_every_configured_band_gets_a_row(self):
        with tempfile.TemporaryDirectory() as d:
            text = _survey([], ["vhf_lmr_low", "nwr"], d,
                           cycles=2).format_status()
        for band in ("vhf_lmr_low", "nwr"):
            self.assertTrue(
                [l for l in text.splitlines() if l.strip().startswith(band)],
                f"{band} has no summary row")

    def test_a_row_shows_visits_channel_count_and_active_count(self):
        em = [SyntheticEmitter(151_475_000.0, 26.0, duty=1.0)]
        with tempfile.TemporaryDirectory() as d:
            text = _survey(em, ["vhf_lmr_low"], d, cycles=4).format_status()
        row = [l for l in text.splitlines()
               if l.strip().startswith("vhf_lmr_low")][0]
        self.assertIn("417", row, "total channel count in the band")
        self.assertIn("151.4750 MHz", row, "the busiest channel")

    def test_a_band_with_nothing_detected_says_so_on_its_row(self):
        with tempfile.TemporaryDirectory() as d:
            text = _survey([], ["nwr"], d, cycles=2).format_status()
        row = [l for l in text.splitlines() if l.strip().startswith("nwr")][0]
        self.assertIn("nothing detected", row)


class TestInitialStatement(unittest.TestCase):
    """Printed before any data exists, so an unattended run can be checked
    against what was intended without waiting for the first frame."""

    def _statement(self, bands=("vhf_lmr_low", "nwr")):
        design = SamplingDesign(**DESIGN)
        s = Survey(resolve_bands(list(bands)),
                   SyntheticBackend([], integration_s=0.01,
                                    sample_rate_hz=6e6),
                   SiteMetadata("qth", "RSPdx", "discone", "CM87"),
                   design, out_dir="/tmp")
        return s.initial_statement()

    def test_it_names_the_site_receiver_bands_and_threshold(self):
        t = self._statement()
        for expected in ("qth", "CM87", "RSPdx", "discone",
                         "vhf_lmr_low", "nwr", "threshold", "output"):
            self.assertIn(expected, t)

    def test_it_states_the_blind_spot_before_the_run_not_after(self):
        self.assertIn("blind spot", self._statement())

    def test_a_withheld_location_is_stated_as_such(self):
        design = SamplingDesign(**DESIGN)
        s = Survey(resolve_bands(["nwr"]),
                   SyntheticBackend([], integration_s=0.01,
                                    sample_rate_hz=6e6),
                   SiteMetadata("qth", "RSPdx", "discone"),
                   design, out_dir="/tmp")
        self.assertIn("location withheld", s.initial_statement())

    def test_the_synthetic_backend_is_flagged_in_the_opening_block(self):
        """Nobody should get minutes into a run before noticing."""
        self.assertIn("NO HARDWARE", self._statement())


class _FakeTTY:
    """A stream that claims to be a terminal, so the in-place path runs."""

    def __init__(self):
        self.buf = []

    def isatty(self):
        return True

    def write(self, s):
        self.buf.append(s)

    def flush(self):
        pass

    def text(self):
        return "".join(self.buf)


class TestNonScrollingDisplay(unittest.TestCase):
    """In-place redraw, and the four ways it can go wrong."""

    def test_a_terminal_gets_cursor_movement_not_repeated_blocks(self):
        from occupancy_survey.display import StatusDisplay
        tty = _FakeTTY()
        d = StatusDisplay(stream=tty)
        if not d.in_place:
            self.skipTest("in-place unavailable on this host")
        d.render("one\ntwo")
        d.render("one\ntwo")
        text = tty.text()
        self.assertIn("\033[2F", text,
                      "the second frame must move the cursor up two lines")

    def test_a_non_terminal_emits_no_escape_codes(self):
        """Escapes in a log file under nohup or systemd are garbage."""
        import io
        from occupancy_survey.display import StatusDisplay
        buf = io.StringIO()
        d = StatusDisplay(stream=buf)
        self.assertFalse(d.in_place)
        d.render("a\nb")
        d.render("c\nd")
        self.assertNotIn("\033", buf.getvalue())
        self.assertIn("c\nd", buf.getvalue())

    def test_long_lines_are_truncated_to_the_terminal_width(self):
        """A wrapped line occupies two rows, which breaks the cursor maths
        and corrupts every later frame."""
        from occupancy_survey.display import StatusDisplay
        tty = _FakeTTY()
        d = StatusDisplay(stream=tty)
        if not d.in_place:
            self.skipTest("in-place unavailable on this host")
        width = d._width()
        d.render("x" * (width + 200))
        longest = max((len(l) for l in tty.text().split("\n")), default=0)
        self.assertLessEqual(longest - len("\033[K"), width)

    def test_a_shrinking_block_clears_the_rows_it_vacates(self):
        from occupancy_survey.display import StatusDisplay
        tty = _FakeTTY()
        d = StatusDisplay(stream=tty)
        if not d.in_place:
            self.skipTest("in-place unavailable on this host")
        d.render("a\nb\nc\nd")
        tty.buf.clear()
        d.render("a")
        self.assertIn("\033[K", tty.text(),
                      "vacated rows must be cleared or stale text remains")

    def test_invalidate_forces_a_fresh_draw(self):
        """Log output moves the cursor without the display knowing, so the
        next frame must not try to move up over it."""
        from occupancy_survey.display import StatusDisplay
        tty = _FakeTTY()
        d = StatusDisplay(stream=tty)
        if not d.in_place:
            self.skipTest("in-place unavailable on this host")
        d.render("a\nb")
        d.invalidate()
        tty.buf.clear()
        d.render("a\nb")
        self.assertNotIn("\033[2F", tty.text(),
                         "after invalidation the frame must be drawn fresh")

    def test_force_plain_overrides_a_terminal(self):
        from occupancy_survey.display import StatusDisplay
        d = StatusDisplay(stream=_FakeTTY(), force_plain=True)
        self.assertFalse(d.in_place)


class TestNoVerdictBeforeData(unittest.TestCase):

    def test_an_unvisited_control_band_shows_no_verdict(self):
        """A verdict computed from no data is noise beside "not yet
        visited", and briefly looks like a failure."""
        design = SamplingDesign(**DESIGN)
        s = Survey(resolve_bands(["bcast_fm", "nwr"]),
                   SyntheticBackend([], integration_s=0.01,
                                    sample_rate_hz=6e6),
                   SiteMetadata("qth", "RSPdx", "discone"),
                   design, out_dir="/tmp")
        text = s.format_status()
        row = [l for l in text.splitlines()
               if l.strip().startswith("bcast_fm")][0]
        self.assertIn("not yet visited", row)
        self.assertNotIn("no_channels_observed", row)
