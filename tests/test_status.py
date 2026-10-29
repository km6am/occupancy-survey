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

    def test_the_control_band_verdict_is_shown(self):
        em = [SyntheticEmitter(101_300_000.0, 34.0, duty=1.0,
                               bandwidth_hz=170_000.0)]
        with tempfile.TemporaryDirectory() as d:
            text = _survey(em, ["bcast_fm"], d, cycles=6).format_status()
        self.assertIn("control bcast_fm", text)
        self.assertIn("pass", text)

    def test_long_lists_are_truncated_with_a_count(self):
        em = [SyntheticEmitter(151_000_000.0 + i * 100_000.0, 26.0, duty=1.0)
              for i in range(12)]
        with tempfile.TemporaryDirectory() as d:
            s = _survey(em, ["vhf_lmr_low"], d, cycles=6)
            text = s.format_status(max_rows=4)
        self.assertLessEqual(len([l for l in text.splitlines()
                                  if "MHz" in l]), 4)
        self.assertIn("and", text)
        self.assertIn("more", text)

    def test_a_band_never_visited_is_not_reported(self):
        with tempfile.TemporaryDirectory() as d:
            snap = _survey([], ["nwr"], d, cycles=1).status_snapshot()
        self.assertEqual(snap["bands_visited"], ["nwr"])


if __name__ == "__main__":
    unittest.main()
