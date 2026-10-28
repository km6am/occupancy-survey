"""Presence as a validation reference: absence of NWR indicts the receiver.

A SECOND, WEAKER, MORE ROBUST USE OF NOAA WEATHER RADIO. The duty-cycle
version -- "NWR is 100% occupied, so calibrate the threshold against it" --
was retracted on 2026-10-28 because carrier keying is not established. This
one survives that, because it does not depend on knowing whether the
carrier keys:

    over a long enough observation, NWR must APPEAR.

If an extended look at 162.400-162.550 shows nothing, the fault is on the
measuring end -- threshold too high, gain wrong, antenna or feed broken,
predicted channel wrong -- because the NWS publishes the transmitter list
and its coverage. Absence contradicts PUBLISHED DATA rather than an
inference, which is why it holds where the duty-cycle claim did not.

THE PRECONDITION, which constrains the sampling design rather than
following from it: absence is only evidence if the observation could
plausibly have caught a keyed carrier. A survey dwelling 1 s every 10
minutes could miss one legitimately, and "we did not see NWR" would then be
ambiguous between a dead receive path and an aliased schedule -- the worse
outcome, because it points the operator at hardware that is fine.

Rather than a flat "observe for 15-30 minutes", this computes what the
observation actually bought, using the same catch-probability model as
docs/sampling-design.md:

    MINIMUM DETECTABLE DUTY CYCLE -- the smallest duty a carrier could have
    had and still be caught, at the stated confidence, by N visits of this
    dwell against a cycle of this period.

So the verdict is bounded rather than binary. "Nothing seen, and this run
could have detected a duty cycle of 17% or more" is an honest statement
that indicts the receive path only down to 17%. The same run extended to
four hours brings that to about 1%, at which point absence is damning.

THE STRONGER VERSION, free from data already in hand. NWR is seven fixed
channels and the NWS publishes each transmitter's location and power, so
the prediction can say WHICH channel should be strongest from a given QTH.
Checking predicted-strongest against observed-strongest turns one outcome
into three:

    nothing at all            -> the receive path
    wrong channel strongest   -> the prediction, or the antenna pattern
    right channel, wrong level-> the path loss

A bare presence test gives only the first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# NWS documents a 3-6 minute programme cycle. The LONGER bound is used for
# detectability, because it is the pessimistic one: a longer cycle means a
# given duty cycle is concentrated into a longer transmission with a longer
# silence, which a short dwell is more likely to fall into.
NWR_CYCLE_S_MIN = 180.0
NWR_CYCLE_S_MAX = 360.0


@dataclass(frozen=True)
class PresenceReference:
    """Declares that a band's absence indicts the receiver, not the band."""
    enabled: bool = False
    cycle_s_min: float = NWR_CYCLE_S_MIN
    cycle_s_max: float = NWR_CYCLE_S_MAX
    confidence: float = 0.95
    # How small a detectable duty cycle an observation must reach before
    # ABSENCE counts as evidence against the receive path.
    #
    # 10% is not arbitrary. A service broadcasting on a documented 3-6
    # minute cycle, 24 hours a day, has a duty cycle of at least tens of
    # percent IF IT TRANSMITS AT ALL -- that much survives the retraction,
    # because it follows from the published cycle rather than from any
    # claim about carrier continuity. So an observation able to detect 10%
    # can meaningfully say "this should have shown up and did not", while
    # one that can only detect 60% cannot distinguish a dead receive path
    # from a carrier that happened to be off.
    #
    # The bound itself is always reported, so a reader can apply a
    # different cutoff without re-running anything.
    conclusive_duty: float = 0.10
    basis: str = ""

    def as_metadata(self) -> dict:
        return {"enabled": self.enabled, "cycle_s_min": self.cycle_s_min,
                "cycle_s_max": self.cycle_s_max,
                "confidence": self.confidence,
                "conclusive_duty": self.conclusive_duty,
                "basis": self.basis}


def min_detectable_duty(n_visits: int, dwell_s: float, period_s: float,
                        confidence: float = 0.95) -> float:
    """Smallest duty cycle this much observation could have caught.

    Inverts the catch-probability model. A visit of length D overlaps a
    transmission of length L in a cycle of period P with probability
    (L+D)/P, so for N independent visits

        1 - (1 - (L+D)/P)^N = confidence

    solves to L = P * (1 - (1-confidence)^(1/N)) - D, and the duty is L/P.

    Returns 0.0 when the dwell alone spans the required window -- any duty
    cycle would be caught -- and 1.0 when even a continuous carrier could
    be missed, which means the observation says nothing at all.

    ASSUMES the visits are independent in phase relative to the broadcast
    cycle. That is what the schedule's jitter buys; without it a revisit
    period commensurate with the broadcast cycle can miss indefinitely and
    this figure is meaningless. See docs/sampling-design.md section 5.
    """
    if n_visits <= 0 or period_s <= 0:
        return 1.0
    frac = 1.0 - (1.0 - confidence) ** (1.0 / int(n_visits))
    on_time = period_s * frac - float(dwell_s)
    if on_time <= 0.0:
        return 0.0
    return min(1.0, on_time / period_s)


def _percentile_from_hist(record: dict, hist_spec: dict,
                          pct: float) -> float | None:
    """Excess-dB percentile from a stored histogram."""
    hist = np.asarray(record.get("hist") or [], dtype=np.float64)
    if hist.size == 0:
        return None
    under = float(record.get("underflow", 0))
    over = float(record.get("overflow", 0))
    total = hist.sum() + under + over
    if total <= 0:
        return None
    lo = float(hist_spec["lo_db"])
    bin_db = float(hist_spec["bin_db"])
    target = total * (pct / 100.0)
    cum = under
    if cum >= target:
        return lo
    for i, count in enumerate(hist):
        cum += count
        if cum >= target:
            return lo + (i + 0.5) * bin_db
    return lo + hist.size * bin_db


def strongest_channel(channel_records, hist_spec: dict,
                      pct: float = 90.0):
    """The channel with the highest excess level, and its level.

    Ranks on a high percentile of the excess distribution rather than the
    mean, because a channel present 10% of the time at a strong level
    should outrank one present 90% of the time at a marginal one -- the
    question is which transmitter is loudest, not which is busiest.
    """
    best, best_level = None, None
    for rec in channel_records:
        if not rec.get("n_present"):
            continue
        level = _percentile_from_hist(rec, hist_spec, pct)
        if level is None:
            continue
        if best_level is None or level > best_level:
            best, best_level = rec, level
    return best, best_level


def presence_check(channel_records, hist_spec: dict, integration_s: float,
                   n_visits: int, dwell_s: float,
                   reference: PresenceReference,
                   predicted_strongest_hz: float | None = None,
                   match_tolerance_hz: float = 1_000.0) -> dict:
    """Did the band appear, and was it the channel that should have?"""
    if not reference.enabled:
        return None

    observed_s = sum(int(r.get("n_decided", 0))
                     for r in channel_records) * float(integration_s)
    detectable = min_detectable_duty(n_visits, dwell_s,
                                     reference.cycle_s_max,
                                     reference.confidence)

    best, best_level = strongest_channel(channel_records, hist_spec)
    any_present = best is not None

    out = {
        "reference": reference.as_metadata(),
        "observed_channel_seconds": observed_s,
        "visits": int(n_visits),
        "dwell_s": float(dwell_s),
        "min_detectable_duty": detectable,
        "any_channel_present": any_present,
        "strongest_observed_hz": (best["freq_hz"] if best else None),
        "strongest_observed_excess_db": best_level,
        "predicted_strongest_hz": predicted_strongest_hz,
    }

    if not any_present:
        if detectable > reference.conclusive_duty:
            out["verdict"] = "inconclusive"
            out["meaning"] = (
                f"nothing detected, but this observation could only have "
                f"caught a carrier with a duty cycle of "
                f"{100*detectable:.0f}% or more -- weaker than the "
                f"{100*reference.conclusive_duty:.0f}% a service on a "
                f"documented 3-6 minute cycle would have if it transmits at "
                f"all. So this says NOTHING about the receive path, and in "
                f"particular is not grounds to go looking for a hardware "
                f"fault. Extend the run, or dwell longer on this band.")
        else:
            out["verdict"] = "absent"
            out["meaning"] = (
                f"nothing detected, and this run could have caught a "
                f"carrier with a duty cycle of {100*detectable:.1f}% or "
                f"more. The NWS publishes the transmitter list, so absence "
                f"at that sensitivity indicts the MEASURING END -- "
                f"threshold too high, gain wrong, antenna or feed broken, "
                f"or the predicted channel wrong. It is not evidence about "
                f"a duty cycle below {100*detectable:.1f}%.")
        return out

    if predicted_strongest_hz is None:
        out["verdict"] = "present"
        out["meaning"] = (
            "a carrier was detected, so the receive path reaches this band. "
            "Supply a prediction to also check WHICH channel is strongest, "
            "which separates a prediction error from a path-loss error.")
        return out

    matched = abs(best["freq_hz"] - predicted_strongest_hz) \
        <= match_tolerance_hz
    out["verdict"] = "present_as_predicted" if matched \
        else "present_wrong_channel"
    out["meaning"] = (
        "the predicted-strongest channel is the observed-strongest; the "
        "receive path and the prediction agree on this band"
        if matched else
        f"detected, but the strongest channel is "
        f"{best['freq_hz']/1e6:.4f} MHz where "
        f"{predicted_strongest_hz/1e6:.4f} MHz was predicted. That points "
        f"at the prediction or the antenna pattern rather than the receive "
        f"path, which is working.")
    return out
