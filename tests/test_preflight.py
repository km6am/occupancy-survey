"""Host readiness: the framework is built here and run elsewhere."""

import tempfile
import unittest
from pathlib import Path

from occupancy_survey.config import AppConfig
from occupancy_survey.preflight import BYTES_PER_INTERVAL, run_preflight

BASE = {
    "site": {"site_id": "s1", "receiver_model": "RSPdx", "antenna": "discone"},
    "sdr": {"sample_rate_hz": 2_048_000, "fft_size": 4096,
            "backend": "synthetic"},
    "bands": ["nwr"],
}


def _cfg(out_dir, **sdr):
    raw = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in BASE.items()}
    raw["sdr"].update(sdr)
    raw["output"] = {"dir": str(out_dir)}
    return AppConfig.from_dict(raw)


class TestPreflight(unittest.TestCase):

    def test_a_healthy_host_passes_without_opening_the_radio(self):
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight(_cfg(d))
        self.assertEqual(pf.failures, [])
        names = [c.name for c in pf.untested]
        self.assertIn("device access", names,
                      "the device must be reported as NOT TESTED rather "
                      "than silently passed; opening a radio is the "
                      "operator's call")

    def test_an_unknown_backend_fails_with_the_valid_options(self):
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight(_cfg(d, backend="nonesuch"))
        fail = [c for c in pf.failures if c.name == "backend"]
        self.assertTrue(fail)
        self.assertIn("gnuradio", fail[0].remedy)

    def test_every_failure_carries_a_remedy(self):
        """A verdict without a remedy is not actionable on a strange host."""
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight(_cfg(d, backend="gnuradio"))
        for c in pf.failures:
            self.assertTrue(c.remedy.strip(),
                            f"check '{c.name}' failed with no remedy")

    def test_remedies_are_not_specific_to_the_development_mac(self):
        """This package runs elsewhere, so advice must travel."""
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight(_cfg(d, backend="gnuradio"))
            pf2 = run_preflight(_cfg(d, backend="rtl_sdr", gain=40))
        text = " ".join(c.remedy for c in pf.checks + pf2.checks).lower()
        for host_specific in ("/users/", "radioconda's numpy", "this mac",
                              "rspdx-on-macos"):
            self.assertNotIn(host_specific, text,
                             f"remedy text mentions '{host_specific}', which "
                             f"will mislead on a different host")

    def test_an_unwritable_output_dir_fails(self):
        pf = run_preflight(_cfg("/proc/nonexistent/nope"))
        self.assertTrue([c for c in pf.failures if c.name == "output dir"])

    def test_disk_headroom_is_reported_in_days(self):
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight(_cfg(d))
        disk = [c for c in pf.checks if c.name == "disk space"][0]
        self.assertIn("days of headroom", disk.detail)
        self.assertIn("MB/day", disk.detail)

    def test_the_clock_check_does_not_claim_to_verify_ntp(self):
        with tempfile.TemporaryDirectory() as d:
            pf = run_preflight(_cfg(d))
        clock = [c for c in pf.checks if c.name == "clock"][0]
        self.assertIn("NTP sync NOT verified", clock.detail,
                      "cross-site merging joins on timestamps, so the "
                      "limits of this check must be stated rather than "
                      "implied away")

    def test_the_measured_interval_size_is_the_one_in_the_docs(self):
        """57 kB/interval was measured, and the README quotes it."""
        self.assertEqual(BYTES_PER_INTERVAL, 57_000)
        per_day = BYTES_PER_INTERVAL * 24
        self.assertAlmostEqual(per_day / 1e6, 1.4, delta=0.1)
        self.assertAlmostEqual(per_day * 365 / 1e9, 0.51, delta=0.05)


if __name__ == "__main__":
    unittest.main()
