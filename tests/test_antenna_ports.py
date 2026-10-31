"""Antenna port selection, and the statistic the port probe ranks on."""

import unittest

import numpy as np

from occupancy_survey.backends.base import Backend
from occupancy_survey.backends.synthetic import SyntheticBackend

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.probe_antennas import (describe, STRUCTURE_MIN_EXCESS_DB,
                                  PEAK_THRESHOLD_DB,
                                  SATURATION_CLIP_FRACTION,
                                  port_verdicts, FLAT_FLOOR_SPREAD_DB,
                                  MIN_FREQ_RATIO_FOR_SPREAD)

RNG = np.random.default_rng(20261101)


def _flat(bins=2048, sweeps=4, floor=-95.0):
    """An unterminated port: noise and nothing else."""
    return [floor + RNG.normal(0.0, 1.2, bins) for _ in range(sweeps)]


def _with_carriers(bins=2048, sweeps=4, floor=-95.0, excess=25.0):
    out = []
    for _ in range(sweeps):
        s = floor + RNG.normal(0.0, 1.2, bins)
        for centre in (300, 900, 1500):
            s[centre] += excess
        out.append(s)
    return out


class TestPortDiscrimination(unittest.TestCase):

    def test_a_flat_port_shows_no_structure(self):
        """The unplugged-cable case, which is the whole point of the tool."""
        d = describe(_flat(), [0.0] * 4)
        self.assertFalse(d["sees_structure"],
                         f"flat noise read as structure at "
                         f"{d['peak_excess_db']:.1f} dB excess; "
                         f"STRUCTURE_MIN_EXCESS_DB may be too low")
        self.assertEqual(d["bins_above_floor"], 0)

    def test_a_port_with_carriers_shows_structure(self):
        d = describe(_with_carriers(), [0.0] * 4)
        self.assertTrue(d["sees_structure"])
        self.assertGreaterEqual(d["peak_excess_db"],
                                STRUCTURE_MIN_EXCESS_DB)

    def test_the_floor_alone_does_not_discriminate(self):
        """A port can sit 20 dB high and still be dead.

        This is why the tool ranks on peak excess and not on the floor:
        an attenuator, a different internal path or a preamp all move the
        floor without an antenna being attached.
        """
        high_but_flat = describe(_flat(floor=-75.0), [None] * 4)
        low_with_signal = describe(_with_carriers(floor=-105.0), [None] * 4)
        self.assertGreater(high_but_flat["floor_db"],
                           low_with_signal["floor_db"])
        self.assertFalse(high_but_flat["sees_structure"])
        self.assertTrue(low_with_signal["sees_structure"])

    def test_peak_threshold_sits_above_noise_excursions(self):
        """PEAK_THRESHOLD_DB must clear the floor's own tail."""
        d = describe(_flat(bins=8192, sweeps=8), [None] * 8)
        self.assertLess(d["peak_excess_db"], PEAK_THRESHOLD_DB,
                        "noise-only excursions reach the peak threshold; "
                        "bins_above_floor will count noise as carriers")

    def test_no_sweeps_is_reported_not_guessed(self):
        self.assertEqual(describe([], [])["sweeps"], 0)

    def test_clip_fraction_is_the_max_not_the_mean(self):
        """One clipping sweep in ten is still a clipping front end."""
        d = describe(_flat(), [0.0, 0.0, 0.0, 3e-3])
        self.assertAlmostEqual(d["clip_fraction_max"], 3e-3)

    def test_absent_clip_data_stays_absent(self):
        self.assertIsNone(describe(_flat(), [None] * 4)["clip_fraction_max"])


class TestBackendDefaults(unittest.TestCase):

    def test_a_single_input_receiver_reports_no_ports(self):
        b = SyntheticBackend(emitters=[], sample_rate_hz=2e6, fft_size=256,
                             integration_s=0.05, realtime=False)
        self.assertEqual(b.antenna_ports(), [])

    def test_switching_a_port_that_does_not_exist_raises(self):
        """Not a silent no-op: three identical results under three names
        would read as three working ports."""
        b = SyntheticBackend(emitters=[], sample_rate_hz=2e6, fft_size=256,
                             integration_s=0.05, realtime=False)
        with self.assertRaises(NotImplementedError):
            b.set_antenna_port("Antenna A")

    def test_the_base_class_declares_both(self):
        for name in ("antenna_ports", "set_antenna_port"):
            self.assertTrue(callable(getattr(Backend, name, None)), name)


class TestSaturationIsNotAbsence(unittest.TestCase):
    """A saturated port and an unterminated port are both FLAT.

    Measured case, from the first run against a real antenna: Antenna A at
    98 MHz read floor +31.1 dB, peak excess 5.2 dB, 0 bins above floor, clip
    3.8e-02. Forty dB above the other two ports and perfectly flat -- the
    broadcast-FM band compressing the front end, not a disconnected cable.
    The tool called it unterminated.
    """

    def _saturated(self):
        # Flat and clipping: the shape of a compressed front end.
        return describe(_flat(floor=31.1), [3.8e-2] * 4)

    def test_a_saturated_port_is_not_called_unterminated(self):
        self.assertIsNone(self._saturated()["sees_structure"],
                          "a clipping port must read as indeterminate, not "
                          "as showing no structure")

    def test_a_saturated_port_is_flagged_saturated(self):
        self.assertTrue(self._saturated()["saturated"])

    def test_an_unterminated_port_is_still_a_definite_no(self):
        """The gate must not swallow the case the tool exists for."""
        d = describe(_flat(), [0.0] * 4)
        self.assertIs(d["sees_structure"], False)
        self.assertFalse(d["saturated"])

    def test_a_live_port_that_clips_still_reads_as_live(self):
        """Clipping does not erase structure that is plainly there."""
        d = describe(_with_carriers(), [1e-3] * 4)
        self.assertIs(d["sees_structure"], True)
        self.assertTrue(d["saturated"],
                        "levels on a clipping port are still suspect")

    def test_clipping_just_under_the_gate_does_not_trip_it(self):
        d = describe(_flat(), [SATURATION_CLIP_FRACTION] * 4)
        self.assertFalse(d["saturated"])
        self.assertIs(d["sees_structure"], False)

    def test_missing_clip_data_cannot_claim_saturation(self):
        """A backend that cannot measure clipping must not be presumed sick."""
        d = describe(_flat(), [None] * 4)
        self.assertFalse(d["saturated"])
        self.assertIs(d["sees_structure"], False)


def _row(freq_hz, port, floor, excess=12.0, clip=0.0):
    """A probe row with the fields port_verdicts() reads."""
    return {"freq_hz": freq_hz, "port_requested": port,
            "port_reported": port, "sweeps": 4, "floor_db": floor,
            "p99_9_db": floor + excess, "peak_excess_db": excess,
            "bins_above_floor": 10, "bins": 2048,
            "clip_fraction_max": clip,
            "saturated": clip > SATURATION_CLIP_FRACTION,
            "sees_structure": True}


# The two runs this threshold was calibrated on, at 162.475 and 465 MHz.
MEASURED = [
    _row(162.475e6, "Antenna A", -3.7, 45.4),
    _row(162.475e6, "Antenna B", -11.2, 11.2),
    _row(162.475e6, "Antenna C", -14.2, 15.1),
    _row(465.0e6, "Antenna A", -13.6, 43.2),
    _row(465.0e6, "Antenna B", -12.0, 17.7),
    _row(465.0e6, "Antenna C", -14.3, 12.2),
]


class TestFloorSpreadFindsTheAntenna(unittest.TestCase):
    """The discriminator peak excess could not provide.

    All three ports showed 11-45 dB of peak excess and read "structure".
    Only the floor across frequency separated the connected port, because
    an open connector picks up strong local signals perfectly well.
    """

    def test_the_connected_port_is_identified(self):
        v = port_verdicts(MEASURED)
        self.assertTrue(v["Antenna A"]["likely_connected"])
        self.assertFalse(v["Antenna B"]["likely_connected"])
        self.assertFalse(v["Antenna C"]["likely_connected"])

    def test_peak_excess_alone_would_not_have(self):
        """Documents why this test exists: every port passes that one."""
        for r in MEASURED:
            self.assertTrue(r["peak_excess_db"] >= STRUCTURE_MIN_EXCESS_DB,
                            "the measured case has all ports showing "
                            "structure; that is the point")

    def test_the_measured_spreads_sit_either_side_with_margin(self):
        v = port_verdicts(MEASURED)
        self.assertGreater(v["Antenna A"]["floor_spread_db"],
                           FLAT_FLOOR_SPREAD_DB * 2.0)
        for open_port in ("Antenna B", "Antenna C"):
            self.assertLess(v[open_port]["floor_spread_db"],
                            FLAT_FLOOR_SPREAD_DB / 2.0)

    def test_a_narrow_span_refuses_to_judge(self):
        """153 vs 162 MHz is no lever arm; a real antenna is flat there too."""
        narrow = [_row(153.4e6, "Antenna A", -3.7),
                  _row(162.475e6, "Antenna A", -3.9)]
        self.assertEqual(port_verdicts(narrow), {},
                         "a 1.06x span must not produce a verdict")

    def test_one_frequency_refuses_to_judge(self):
        self.assertEqual(port_verdicts([_row(162.475e6, "Antenna A", -3.7)]),
                         {})

    def test_the_minimum_ratio_is_a_real_lever(self):
        self.assertGreater(MIN_FREQ_RATIO_FOR_SPREAD, 1.0)

    def test_saturated_rows_are_excluded_from_the_spread(self):
        """A compressed floor is a gain artefact, not a noise floor.

        Antenna A at 98 MHz read +31.1 dB while clipping. Folding that into
        the spread would invent 45 dB of frequency dependence out of the
        overload and call an open port connected.
        """
        rows = [_row(98.0e6, "Antenna B", 31.1, 5.2, clip=3.8e-2),
                _row(162.475e6, "Antenna B", -11.2),
                _row(465.0e6, "Antenna B", -12.0)]
        v = port_verdicts(rows)
        self.assertFalse(v["Antenna B"]["likely_connected"])
        self.assertEqual(v["Antenna B"]["n_frequencies"], 2)


if __name__ == "__main__":
    unittest.main()
