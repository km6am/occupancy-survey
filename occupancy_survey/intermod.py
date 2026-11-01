"""Telling a real signal from an intermodulation product, by stepping gain.

WHY THIS EXISTS. Intermod is the dominant error source for wideband
occupancy measurement near a city, and it fails in the flattering
direction. Third-order products of two strong broadcast or land-mobile
carriers land on channels where nothing is transmitting, and they appear
EXACTLY WHEN THOSE CARRIERS KEY UP -- so they correlate with the
predicted-strong emitters and make the prediction-versus-measurement plot
look better while being wrong.

That is worse than the thermal false-alarm rate in detector.py, which is at
least random. A structured false alarm survives averaging, survives a long
run, and looks like traffic.

THE DISCRIMINATOR. Change the front-end gain by G dB and watch what happens
to a channel's level RELATIVE TO THE NOISE FLOOR:

    a real signal       falls by G dB; the noise floor falls by G dB too,
                        so its excess over the floor does not move.
    a 2nd-order product falls by 2G; excess drops by about G.
    a 3rd-order product falls by 3G; excess drops by about 2G.

So the implied order is

    order = 1 + (excess_before - excess_after) / G

and anything materially above 1 is being generated inside the receiver, not
transmitted. This is a *discriminating* test rather than a consistency
check: a real signal and an intermod product predict different numbers, and
the measurement can come down on either side.

WHAT IT CANNOT DO. It cannot find a product that sits on top of a real
signal on the same channel -- the sum behaves like whatever dominates. It
also assumes the gain change is accurate and that nothing in the band
changed between the two measurements, which for intermittent land mobile is
a real risk: a dispatch channel that stops transmitting between the two
measurements looks exactly like a high-order product. So a channel is only
flagged when it was present in BOTH measurements, and the result is a
suspicion to be resolved with a filter, not a verdict.

THE FIX IS A FILTER, not a threshold. Raising the threshold to hide
products also hides weak real signals. The answer is a bandpass ahead of
the receiver, a notch on the dominant carrier, or less gain -- in that
order, because the first two cost no sensitivity and the third does.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# A real signal implies order 1. Allow for measurement scatter and a gain
# step that is not exactly what was asked for before calling anything
# suspect; 1.6 sits well clear of 1 and well below the 3 of an IM3 product.
SUSPECT_ORDER = 1.6

# A product's order is a small integer: 2 or 3, occasionally 5 and weakly.
# An order of 4.6 means the excess fell by 3.6 gain steps, which no simple
# product predicts -- the parsimonious explanation is that the channel
# stopped transmitting between the two measurements, and the same holds in
# reverse for an order near or below zero. Reporting those as "likely 3rd
# order" dresses an unexplained number up as a mechanism, so they get their
# own bucket. They are NOT excluded from the advice: an unstable measurement
# is a reason to repeat the run, not a reason to call the band clean.
IMPLAUSIBLE_ORDER_MAX = 3.5
IMPLAUSIBLE_ORDER_MIN = 0.4


@dataclass(frozen=True)
class ChannelOrder:
    """One channel's implied nonlinearity order."""
    freq_hz: float
    excess_before_db: float
    excess_after_db: float
    gain_step_db: float
    implied_order: float
    present_in_both: bool

    @property
    def unexplained(self) -> bool:
        """Order outside the range any simple product can produce."""
        if not self.present_in_both or not np.isfinite(self.implied_order):
            return False
        return (self.implied_order > IMPLAUSIBLE_ORDER_MAX
                or self.implied_order < IMPLAUSIBLE_ORDER_MIN)

    @property
    def suspect(self) -> bool:
        """Scales like a product, by an order a product can actually have."""
        return (self.present_in_both and not self.unexplained
                and self.implied_order >= SUSPECT_ORDER)

    @property
    def verdict(self) -> str:
        if not self.present_in_both:
            return "not assessable"
        if self.implied_order > IMPLAUSIBLE_ORDER_MAX:
            return "too steep for a product -- signal probably stopped"
        if self.implied_order < IMPLAUSIBLE_ORDER_MIN:
            return "level rose with less gain -- signal probably started"
        if self.implied_order >= 2.5:
            return "likely 3rd-order product"
        if self.implied_order >= SUSPECT_ORDER:
            return "likely 2nd-order product"
        return "consistent with a real signal"


def implied_order(excess_before_db: float, excess_after_db: float,
                  gain_step_db: float) -> float:
    """Nonlinearity order implied by how excess moved with gain.

    `gain_step_db` is the magnitude of the REDUCTION applied between the
    two measurements, and must be positive.
    """
    if gain_step_db <= 0:
        raise ValueError("gain_step_db must be positive")
    return 1.0 + (float(excess_before_db) - float(excess_after_db)) \
        / float(gain_step_db)


def classify(freqs_hz, excess_before, excess_after, gain_step_db: float,
             present_threshold_db: float = 10.0) -> list[ChannelOrder]:
    """Classify every channel from a paired-gain measurement."""
    before = np.asarray(excess_before, dtype=np.float64)
    after = np.asarray(excess_after, dtype=np.float64)
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    if not (freqs.shape == before.shape == after.shape):
        raise ValueError("freqs, before and after must be the same length")

    out = []
    for f, b, a in zip(freqs, before, after, strict=True):
        both = bool(np.isfinite(b) and np.isfinite(a)
                    and b >= present_threshold_db)
        order = (implied_order(b, a, gain_step_db)
                 if np.isfinite(b) and np.isfinite(a) else float("nan"))
        out.append(ChannelOrder(freq_hz=float(f), excess_before_db=float(b),
                                excess_after_db=float(a),
                                gain_step_db=float(gain_step_db),
                                implied_order=float(order),
                                present_in_both=both))
    return out


def summarise(results) -> dict:
    """Run-level summary, for the dataset and for the operator."""
    assessable = [r for r in results if r.present_in_both]
    suspect = [r for r in assessable if r.suspect]
    unexplained = [r for r in assessable if r.unexplained]
    out = {
        "gain_step_db": (results[0].gain_step_db if results else None),
        "channels_assessable": len(assessable),
        "channels_suspect": len(suspect),
        "channels_unexplained": len(unexplained),
        "suspect_fraction": (len(suspect) / len(assessable)
                             if assessable else None),
        "unexplained_fraction": (len(unexplained) / len(assessable)
                                 if assessable else None),
        # Both buckets are listed, steepest first, so an operator reading
        # the output cannot mistake one for the other.
        "suspect_channels": [
            {"freq_hz": r.freq_hz, "implied_order": round(r.implied_order, 2),
             "verdict": r.verdict} for r in sorted(
                suspect + unexplained, key=lambda r: r.implied_order,
                reverse=True)[:40]
        ],
    }
    frac = out["suspect_fraction"]
    unex = out["unexplained_fraction"] or 0.0
    if frac is None:
        out["advice"] = ("no channel was strong enough in both measurements "
                         "to assess; the test says nothing either way")
    elif frac >= 0.2:
        out["advice"] = (
            f"{100*frac:.0f}% of detected channels scale faster than 1:1 "
            f"with gain. The front end is being driven into "
            f"nonlinearity and a large share of this band's occupancy is "
            f"probably generated inside the receiver. Fit the band's "
            f"recommended bandpass, enable the notch filters, or reduce "
            f"gain -- in that order, because the first two cost no "
            f"sensitivity and the last one does.")
    elif frac > 0.0:
        out["advice"] = (
            f"{len(suspect)} channel(s) scale faster than 1:1 with gain and "
            f"are probably products rather than transmissions. Treat their "
            f"occupancy as unmeasured until a filter change clears them.")
    elif unex > 0.0:
        out["advice"] = (
            f"no channel scaled like a product, but {len(unexplained)} of "
            f"{len(assessable)} moved by more than any product order "
            f"explains -- traffic starting or stopping between the two "
            f"measurements looks exactly like this. The band is NOT shown "
            f"clean; repeat the run. A real product reproduces on the same "
            f"channel at the same order, intermittent traffic does not.")
    else:
        out["advice"] = ("every assessable channel scaled 1:1 with gain, "
                         "which is what a linear front end does. No "
                         "evidence of intermod at this gain setting.")

    if unex > 0.0 and frac is not None and frac > 0.0:
        out["advice"] += (
            f" Separately, {len(unexplained)} channel(s) moved by more than "
            f"any product order explains, which is the signature of traffic "
            f"starting or stopping mid-measurement: repeat the run and keep "
            f"the channels that reproduce at the same order.")
    return out
