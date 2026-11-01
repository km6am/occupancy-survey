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

    def __init__(self, if_db=40.0, rf_db=0.0):
        self.if_gain_reduction_db = if_db
        self.rf_gain_reduction_db = rf_db


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


from occupancy_survey.backends.rspdx_gr import GnuRadioBackend  # noqa: E402


class _Backend:
    """GnuRadioBackend's gain methods without a flowgraph behind them.

    `accepted` stands for the readback taken at setup, before any samples
    flow -- the baseline drift is measured from.
    """
    GAIN_LOCK_TOLERANCE_DB = 0.5

    _gain_stage_names = GnuRadioBackend._gain_stage_names
    _read_gain_stages = GnuRadioBackend._read_gain_stages
    gain_in_effect = GnuRadioBackend.gain_in_effect

    def __init__(self, source, accepted=None, config=None):
        self._source = source
        self._config = config or _Cfg()
        self._gain_accepted = (dict(accepted) if accepted is not None
                               else self._read_gain_stages())


def _verdict(pf):
    return {c.name: c for c in pf.checks}["gain lock"]


class TestGainReadback(unittest.TestCase):

    def test_gain_that_held_reads_locked(self):
        g = _Backend(_Source({"IF": -40.0, "RF": -0.0})).gain_in_effect()
        self.assertTrue(g["locked"])
        self.assertEqual(g["worst_drift_db"], 0.0)

    def test_the_api_moving_if_gain_is_caught(self):
        """The case the overload messages point at.

        Baseline is the setup readback, so the move has to happen after it.
        """
        source = _Source({"IF": -40.0, "RF": -0.0})
        b = _Backend(source)
        source._gains["IF"] = -52.0
        g = b.gain_in_effect()
        self.assertFalse(g["locked"])
        self.assertAlmostEqual(g["worst_drift_db"], 12.0)

    def test_agc_reported_on_by_the_device_is_never_locked(self):
        """Even with the gains sitting exactly where we put them."""
        g = _Backend(_Source({"IF": -40.0, "RF": -0.0},
                             agc=True)).gain_in_effect()
        self.assertFalse(g["locked"])
        self.assertTrue(g["agc_mode"])

    def test_sub_step_drift_is_not_reported_as_movement(self):
        source = _Source({"IF": -40.0, "RF": -0.0})
        b = _Backend(source)
        source._gains["IF"] = -40.3
        self.assertTrue(b.gain_in_effect()["locked"])

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
        source = _Source({"IF": -40.0, "RF": -0.0})
        b = _Backend(source)
        source._gains["IF"] = -52.0
        c = self._run(b)
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


class TestQuantisationIsNotDrift(unittest.TestCase):
    """The RSPdx's RF attenuator moves in discrete LNA states.

    Measured: a request for 20 dB of RF reduction came back as 24 dB. The
    first version of this check compared the reading against the REQUESTED
    value, called the 4 dB step "the device changed gain by itself", and
    failed the preflight -- which would have blocked a campaign over the
    attenuator working exactly as built.
    """

    _ASKED_20 = dict(config=_Cfg(if_db=20.0, rf_db=20.0))

    def test_a_stepped_request_is_not_a_failure(self):
        # Asked RF -20, device landed on -24, and stayed there.
        b = _Backend(_Source({"IF": -20.0, "RF": -24.0}), **self._ASKED_20)
        g = b.gain_in_effect()
        self.assertTrue(g["locked"],
                        "an attenuator step must not read as gain drift")
        self.assertEqual(g["worst_drift_db"], 0.0)

    def test_the_step_is_still_reported(self):
        """Visible, not silently normalised: it changes the real gain."""
        g = _Backend(_Source({"IF": -20.0, "RF": -24.0}),
                     **self._ASKED_20).gain_in_effect()
        self.assertAlmostEqual(g["stages"]["RF"]["quantisation_db"], -4.0)
        self.assertAlmostEqual(g["stages"]["RF"]["configured_db"], -20.0)
        self.assertAlmostEqual(g["stages"]["RF"]["accepted_db"], -24.0)

    def test_movement_after_setup_is_still_caught(self):
        """The check must not go blind to the thing it exists for."""
        source = _Source({"IF": -20.0, "RF": -24.0})
        b = _Backend(source, accepted={"IF": -20.0, "RF": -24.0})
        source._gains["RF"] = -36.0          # the API intervening
        g = b.gain_in_effect()
        self.assertFalse(g["locked"])
        self.assertAlmostEqual(g["worst_drift_db"], 12.0)

    def test_drift_is_measured_from_accepted_not_from_requested(self):
        source = _Source({"IF": -20.0, "RF": -24.0})
        b = _Backend(source, accepted={"IF": -20.0, "RF": -24.0})
        source._gains["RF"] = -20.0          # back to what was asked for
        g = b.gain_in_effect()
        self.assertFalse(g["locked"],
                         "moving TO the requested value is still movement")
        self.assertAlmostEqual(g["stages"]["RF"]["drift_db"], 4.0)

    def test_a_device_without_readback_at_setup_still_works(self):
        """No baseline available: fall back to the requested value."""
        b = _Backend(_Source({"IF": -20.0, "RF": -20.0}), accepted={},
                     **self._ASKED_20)
        self.assertTrue(b.gain_in_effect()["locked"])


if __name__ == "__main__":
    unittest.main()
