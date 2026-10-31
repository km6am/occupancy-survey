"""Fixed gain is the premise; this checks the tool can tell when it breaks.

Refusing the AGC *setting* is not the same as having fixed gain. The SDRplay
API corrects overloads by itself and says so on stderr. If it does that by
moving a gain stage then the gain is not fixed, the noise floor is not
stable, and the gain recorded in the dataset is a fiction -- while every
`agc_enabled: false` check in the project still passes.
"""

import unittest

from occupancy_survey.backends.base import Backend
from occupancy_survey.preflight import Preflight, _check_gain_lock


class _Cfg:
    device_type = "rspdx"
    if_gain_reduction_db = 40.0
    rf_gain_reduction_db = 0.0


class _Source:
    """Stands in for sdrplay3.rspdx's gain readback."""

    def __init__(self, gains, agc=False, raise_on=None):
        self._gains = gains
        self._agc = agc
        self._raise_on = raise_on

    def get_gain(self, name):
        if self._raise_on == name:
            raise RuntimeError("device busy")
        return self._gains[name]

    def get_gain_mode(self):
        return self._agc


class _Backend:
    """GnuRadioBackend.gain_in_effect() without a flowgraph behind it."""
    GAIN_LOCK_TOLERANCE_DB = 0.5

    def __init__(self, source):
        self._source = source
        self._config = _Cfg()

    gain_in_effect = None  # bound below


from occupancy_survey.backends.rspdx_gr import GnuRadioBackend  # noqa: E402
_Backend.gain_in_effect = GnuRadioBackend.gain_in_effect


def _verdict(pf):
    return {c.name: c for c in pf.checks}["gain lock"]


class TestGainReadback(unittest.TestCase):

    def test_gain_that_held_reads_locked(self):
        g = _Backend(_Source({"IF": -40.0, "RF": -0.0})).gain_in_effect()
        self.assertTrue(g["locked"])
        self.assertEqual(g["worst_drift_db"], 0.0)

    def test_the_api_moving_if_gain_is_caught(self):
        """The case the overload messages point at."""
        g = _Backend(_Source({"IF": -52.0, "RF": -0.0})).gain_in_effect()
        self.assertFalse(g["locked"])
        self.assertAlmostEqual(g["worst_drift_db"], 12.0)

    def test_agc_reported_on_by_the_device_is_never_locked(self):
        """Even with the gains sitting exactly where we put them."""
        g = _Backend(_Source({"IF": -40.0, "RF": -0.0},
                             agc=True)).gain_in_effect()
        self.assertFalse(g["locked"])
        self.assertTrue(g["agc_mode"])

    def test_sub_step_drift_is_not_reported_as_movement(self):
        g = _Backend(_Source({"IF": -40.3, "RF": -0.0})).gain_in_effect()
        self.assertTrue(g["locked"])

    def test_a_stage_that_cannot_be_read_is_not_called_locked(self):
        """Unknown must not read the same as verified."""
        g = _Backend(_Source({"IF": -40.0, "RF": -0.0},
                             raise_on="RF")).gain_in_effect()
        self.assertFalse(g["locked"])
        self.assertIn("error", g["stages"]["RF"])

    def test_a_device_without_readback_says_so(self):
        class Bare:
            pass
        g = _Backend(Bare()).gain_in_effect()
        self.assertFalse(g["queryable"])
        self.assertNotIn("locked", g)

    def test_the_base_class_default_refuses_to_claim_gain_held(self):
        self.assertFalse(Backend.gain_in_effect(object())["queryable"])


class TestPreflightVerdict(unittest.TestCase):

    def _run(self, backend):
        pf = Preflight()
        _check_gain_lock(pf, backend)
        return _verdict(pf)

    def test_locked_gain_passes(self):
        self.assertTrue(self._run(
            _Backend(_Source({"IF": -40.0, "RF": -0.0}))).ok)

    def test_moved_gain_fails_loudly(self):
        c = self._run(_Backend(_Source({"IF": -52.0, "RF": -0.0})))
        self.assertFalse(c.ok)
        self.assertIn("12.0 dB", c.detail)

    def test_agc_on_fails_with_its_own_message(self):
        c = self._run(_Backend(_Source({"IF": -40.0, "RF": -0.0}, agc=True)))
        self.assertFalse(c.ok)
        self.assertIn("AGC", c.detail)

    def test_unqueryable_is_neither_pass_nor_fail(self):
        """A warning, because unchecked gain is not failed gain."""
        class Bare:
            def gain_in_effect(self):
                return {"queryable": False}
        self.assertIsNone(self._run(Bare()).ok)


if __name__ == "__main__":
    unittest.main()
