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


# --------------------------------------------------------------------------- #
# The RF-step form, on absolute levels. This is what survey.intermod_check()
# runs; the excess-based functions above remain the derivation.
# --------------------------------------------------------------------------- #
#
# WHY ABSOLUTE LEVELS. The excess form assumes the noise floor falls by the
# full gain step, which holds only when external noise dominates the floor.
# Measured 2026-11-01 on an RSPdx at CM87, it does not: an 8.3 dB RF step
# moved the UHF floor 0.7 dB and the NWR/VHF-high floor 0. Under the excess
# form every real signal there would read order ~1.9, "likely 2nd-order
# product". In absolute terms a real signal falls by exactly the step G and
# an n-th-order product by nG, whatever the floor does.
#
# WHY A-B-A. Land mobile keys up and down between measurements, and a channel
# that stops looks like a steep product. Measuring at the base gain before
# AND after the reduced one and requiring the two to agree sets those aside
# as "unstable" instead of classifying them.
#
# WHY G IS MEASURED. The RSPdx readback overstated a UHF step by 45% (12 dB
# read, 8.3 dB real, on three tunings). The median drop of strong, stable
# channels is the step the signals actually saw; a product among them is
# outvoted. The readback is used only when fewer than two strong channels
# exist, and the result says so.
#
# A FOURTH CASE the excess form had no name for: a channel that does not move
# with RF gain at all is generated after the LNA -- a spur. Measured: 157.200
# moved 0.05 dB and 456.000 0.8 dB for 6-8 dB steps, and both read the same
# with no antenna on the port.

STABLE_DB = 1.5          # A1 and A2 must agree this closely
VISIBLE_AT_B_DB = 6.0    # B must clear its own floor, or its level is noise
STRONG_EXCESS_DB = 25.0  # channels trusted to measure the step itself
INTERNAL_ORDER_MAX = 0.4


def classify_rf_step(freqs_hz, a1_db, b_db, a2_db, base_a1_db: float,
                     base_b_db: float, base_a2_db: float,
                     threshold_db: float = 10.0,
                     readback_step_db: float | None = None) -> dict:
    """Classify every channel present at base gain from an A-B-A RF step."""
    f = np.asarray(freqs_hz, float)
    a1, b, a2 = (np.asarray(x, float) for x in (a1_db, b_db, a2_db))
    if not (f.shape == a1.shape == b.shape == a2.shape):
        raise ValueError("freqs and the three level arrays must match")
    a = 0.5 * (a1 + a2)
    excess = a - 0.5 * (base_a1_db + base_a2_db)
    present = ((a1 - base_a1_db >= threshold_db)
               & (a2 - base_a2_db >= threshold_db))
    stable = np.abs(a1 - a2) <= STABLE_DB
    visible = b - base_b_db >= VISIBLE_AT_B_DB
    drop = a - b
    strong = present & stable & visible & (excess >= STRONG_EXCESS_DB)

    if strong.sum() >= 2:
        g, source = float(np.median(drop[strong])), "measured"
    elif readback_step_db:
        g, source = float(readback_step_db), "readback"
    else:
        g, source = float("nan"), "none"

    floor_drop = 0.5 * (base_a1_db + base_a2_db) - base_b_db
    rows = []
    for j in np.where(present)[0]:
        order = drop[j] / g if np.isfinite(g) and g > 0.5 else float("nan")
        # A channel that sinks into the floor at B is not unassessable if a
        # REAL signal of its level would have stayed clearly visible: the
        # vanishing bounds its order from below. Products are mostly weak,
        # so without this a 30 dB IM3 under an 8 dB step -- which falls 25
        # dB and disappears -- would be filed as "too weak" and the check
        # would miss most of what it exists to find.
        lower = (excess[j] + floor_drop - VISIBLE_AT_B_DB) / g \
            if np.isfinite(g) and g > 0.5 else float("nan")
        real_would_show = (np.isfinite(g)
                           and excess[j] - g + floor_drop
                           >= VISIBLE_AT_B_DB + 2.0)
        if not stable[j]:
            v = "unstable: changed between the two base-gain readings"
        elif not visible[j] and real_would_show and lower >= SUSPECT_ORDER:
            v = (f"vanished at reduced gain where a real signal would show: "
                 f"product-like, order >= {lower:.1f}")
            order = lower
        elif not visible[j]:
            v = "too weak at reduced gain to assess"
        elif not np.isfinite(order):
            v = "not assessable: gain step unknown"
        elif order < INTERNAL_ORDER_MAX:
            v = "internal: does not follow RF gain (receiver spur)"
        elif order > IMPLAUSIBLE_ORDER_MAX:
            v = "too steep for a product -- signal probably stopped"
        elif order >= 2.5:
            v = "likely 3rd-order product"
        elif order >= SUSPECT_ORDER:
            v = "likely 2nd-order product"
        else:
            v = "real signal"
        rows.append({"freq_hz": float(f[j]), "excess_db": round(float(excess[j]), 1),
                     "drop_db": round(float(drop[j]), 2),
                     "order": None if not np.isfinite(order) else round(float(order), 2),
                     "verdict": v})
    return {"gain_step_db_used": g, "gain_step_source": source,
            "gain_step_readback_db": readback_step_db,
            "floor_drop_db": round(float(floor_drop), 2),
            "channels": rows}


def summarise_rf_step(result: dict) -> dict:
    """Counts and advice for an RF-step check, in summarise()'s shape."""
    rows = result["channels"]
    assessable = [r for r in rows if r["order"] is not None
                  and not r["verdict"].startswith(("unstable", "too weak"))]
    products = [r for r in assessable if "product" in r["verdict"]
                or r["verdict"].startswith("vanished")]
    internal = [r for r in assessable if r["verdict"].startswith("internal")]
    steep = [r for r in assessable if r["verdict"].startswith("too steep")]
    unstable = [r for r in rows if r["verdict"].startswith("unstable")]
    out = dict(result)
    out.update({
        "channels_present": len(rows),
        "channels_assessable": len(assessable),
        "channels_suspect": len(products),
        "channels_internal": len(internal),
        "channels_unexplained": len(steep),
        "channels_unstable": len(unstable),
        "suspect_fraction": (len(products) / len(assessable)
                             if assessable else None),
        "suspect_channels": [
            {"freq_hz": r["freq_hz"], "implied_order": r["order"],
             "verdict": r["verdict"]}
            for r in sorted(products + steep + internal,
                            key=lambda r: -(r["order"] or 0))[:40]],
    })
    if result["gain_step_source"] == "none" or not assessable:
        out["advice"] = ("nothing was strong and stable enough to assess; "
                         "the test says nothing either way")
    elif products:
        out["advice"] = (
            f"{len(products)} of {len(assessable)} assessable channel(s) "
            f"fall faster than the RF step and are probably generated in "
            f"the front end. Treat their occupancy as unmeasured; fit the "
            f"band's bandpass, then reduce gain only if that is not enough.")
    else:
        out["advice"] = (f"every assessable channel followed the RF step "
                         f"like a real signal or a spur; no evidence of "
                         f"front-end intermod at this gain")
    if internal:
        out["advice"] += (f". {len(internal)} channel(s) did not follow RF "
                          f"gain at all: receiver spurs, which add to the "
                          f"spur list rather than to occupancy")
    if steep or unstable:
        out["advice"] += (f". {len(steep) + len(unstable)} channel(s) "
                          f"changed in a way traffic explains better than "
                          f"any product; repeat the run to see what "
                          f"reproduces")
    if result["gain_step_source"] == "readback":
        out["advice"] += (". Step taken from the device readback, not "
                          "measured: fewer than two strong channels. The "
                          "RSPdx readback was measured 45% high at UHF")
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
