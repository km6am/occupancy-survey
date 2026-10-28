"""Time-of-day stratification.

Land mobile is dispatch and business traffic, so its duty cycle is strongly
diurnal, and the operations this survey informs happen in daylight. A
figure pooled over the whole clock is diluted by the quiet hours and
UNDERSTATES what a daytime operation meets -- an error in the dangerous
direction, and the same mistake duty_cycles.md section 1 warns about:
averaging across a condition that varies and reporting one number.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from occupancy_survey.backends.synthetic import SyntheticBackend
from occupancy_survey.channels import resolve_bands
from occupancy_survey.dataset import (filter_by_local_hour,
                                      occupancy_by_local_hour, read_dataset)
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata
from occupancy_survey.survey import Survey
from tests.synthetic_band import SyntheticEmitter


def _intervals(n_intervals=30, minutes_per=70):
    """Datasets spread across the clock, one per `minutes_per`."""
    design = SamplingDesign(integration_s=0.01, dwell_s=0.3, interval_s=1.0,
                            usable_bandwidth_hz=6e6)
    clock = {"t": datetime(2026, 10, 29, 8, 0,
                           tzinfo=timezone.utc).astimezone()}

    def now_fn():
        clock["t"] += timedelta(minutes=minutes_per)
        return clock["t"]

    # The 7-channel NWR band, not a 400-channel grid: these tests are
    # about interval timestamps, and a wide band only makes them slow.
    em = [SyntheticEmitter(162_475_000.0, 26.0, duty=0.3, period_sweeps=20,
                           bandwidth_hz=14_000.0)]
    with tempfile.TemporaryDirectory() as d:
        paths = Survey(resolve_bands(["nwr"]),
                       SyntheticBackend(em, integration_s=0.01,
                                        sample_rate_hz=6e6),
                       SiteMetadata("s", "RSPdx", "discone"), design,
                       out_dir=d, seed=4).run(cycles=n_intervals,
                                              now_fn=now_fn)
        return [read_dataset(p) for p in paths]


class TestLocalTimeIsRecorded(unittest.TestCase):

    def test_every_interval_carries_its_local_hour_and_offset(self):
        """Without this, time of day cannot be recovered afterwards."""
        for d in _intervals(4):
            iv = d["interval"]
            self.assertIn("local_hour", iv)
            self.assertIn("utc_offset_s", iv)
            self.assertIsNotNone(iv["local_hour"])
            self.assertGreaterEqual(iv["local_hour"], 0.0)
            self.assertLess(iv["local_hour"], 24.0)

    def test_the_offset_is_recorded_per_interval_not_per_run(self):
        """DST changes mid-campaign; a per-run offset would be wrong after."""
        d = _intervals(2)[0]
        self.assertIn("utc_offset_s", d["interval"])


class TestHourFiltering(unittest.TestCase):

    def test_a_daytime_window_keeps_only_daytime_intervals(self):
        ds = _intervals(30)
        day = filter_by_local_hour(ds, 8, 18)
        self.assertTrue(day)
        self.assertLess(len(day), len(ds))
        for d in day:
            self.assertGreaterEqual(d["interval"]["local_hour"], 8.0)
            self.assertLess(d["interval"]["local_hour"], 18.0)

    def test_a_window_wrapping_midnight_is_handled(self):
        ds = _intervals(30)
        night = filter_by_local_hour(ds, 22, 6)
        for d in night:
            h = d["interval"]["local_hour"]
            self.assertTrue(h >= 22.0 or h < 6.0, f"hour {h} is not night")

    def test_day_and_night_partition_the_set(self):
        ds = _intervals(30)
        day = filter_by_local_hour(ds, 6, 22)
        night = filter_by_local_hour(ds, 22, 6)
        self.assertEqual(len(day) + len(night), len(ds),
                         "every interval must land in exactly one window")

    def test_an_interval_without_a_local_hour_is_dropped_not_assumed_utc(self):
        """Assuming UTC would silently shift a dataset by hours.

        The resulting figure would look entirely reasonable, which is what
        makes the assumption worse than the omission.
        """
        ds = _intervals(4)
        ds[0]["interval"].pop("local_hour")
        kept = filter_by_local_hour(ds, 0, 24)
        self.assertEqual(len(kept), len(ds) - 1)


class TestByHourBreakdown(unittest.TestCase):

    def test_the_diurnal_shape_is_reported_per_hour(self):
        ds = _intervals(30)
        out = occupancy_by_local_hour(ds, "nwr", min_decided=1)
        self.assertTrue(out["hours"])
        for _h, e in out["hours"].items():
            self.assertGreater(e["n_channel_intervals"], 0)
            self.assertGreaterEqual(e["mean_active_occupancy"], 0.0)
            self.assertLessEqual(e["mean_active_occupancy"], 1.0)
        self.assertIn("not what a daylight", out["note"])

    def test_an_unknown_band_yields_no_hours_rather_than_an_error(self):
        out = occupancy_by_local_hour(_intervals(2), "nonexistent")
        self.assertEqual(out["hours"], {})


if __name__ == "__main__":
    unittest.main()
