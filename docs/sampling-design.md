# Sampling design, and what it cannot see

Every occupancy number from this tool is a sample of a process that was not
observed continuously. This document states what the default design can and
cannot see, because that bound belongs beside every result.

All figures here are **inferred** analytically from the sampling model in
`occupancy_survey/schedule.py`, and pinned by `tests/test_schedule.py` so
the document cannot drift away from the code. **None of it is measured.**

---

## 1. The four parameters

| parameter | default | what it sets |
|---|---|---|
| `integration_s` | 0.1 s | the **shortest detectable burst** |
| `dwell_s` | 20 s | samples per visit, and how many are independent |
| `revisit_target_s` | 600 s | the **catch probability** for periodic traffic |
| `jitter_fraction` | 0.25 | whether the bounds below apply at all |

The defaults are deliberately **dwell-heavy rather than revisit-heavy**. For
a duty-cycle statistic, time on channel is worth more than frequency of
return: a long dwell gives many samples of one channel, and occupancy is a
fraction of time, not a count of events.

With the default five-band schedule this comes to 14 tune steps, a **287 s
cycle**, and about **12 visits per tune step per hour**, with the receiver
listening 97% of the wall clock.

---

## 2. The shortest burst

A burst shorter than one integration period is averaged over the whole
period and loses `10·log10(burst / integration)` dB. It survives while that
loss is smaller than the margin the signal has above the threshold:

```
burst_min = integration_s × 10^(−margin_db/10)
```

At the default 0.1 s integration and a 6 dB margin, that is **25 ms**.
Anything shorter is not reliably registered whatever its duty cycle — a
pager burst or a trunked channel grant is in this blind spot.

Note the quantity is *margin above threshold*, not SNR. A signal sitting
exactly at threshold has no margin and cannot survive any averaging at all,
which the model correctly reports as the full integration period.

---

## 3. The catch probability, and the number that matters most

A visit of length `D` overlaps a burst of length `L` recurring on period `P`
with probability `min(1, (L+D)/P)`. Over `n` visits with independent phase:

```
P(at least one catch) = 1 − (1 − (L+D)/P)^n
```

The motivating example — **a channel keyed 3 s every 5 minutes, a 1% duty
cycle** — against the default design:

| design | visits in 1 h | P(seen at all in 1 h) |
|---|---|---|
| 1 s dwell every 10 min | 6 | **0.02** |
| **default: 20 s dwell, 287 s cycle** | **12** | **0.62** |

**The default design does not reliably detect a 1% channel in an hour.** It
sees it about three times in five. This is the single most important number
in this document and it is easy to miss, because 20 s of dwell twelve times
an hour *sounds* thorough.

### Detection and estimation are different budgets

| goal, for a 1% channel | visits | airtime |
|---|---|---|
| 95% sure of **seeing it at all** | 38 | **~3 hours** |
| duty cycle to **±30% relative** | 165 | **~13 hours** |

So an overnight run is the minimum useful unit of work for low-duty
channels, and "an afternoon" — the phrase in `duty_cycles.md` §6 — is enough
only for the 10–25% conventional-LMR figures that document actually wants,
not for anything near 1%.

For the 22.5% TIA-603 figure the picture is much better: a 22.5% channel
with dispatch-length transmissions is caught in nearly every visit, and an
hour is enough to distinguish 22.5% from 10%.

---

## 4. Why the error bars are bigger than they look

Sweeps within a dwell are **not independent**. A transmission lasting longer
than the integration period produces a run of correlated present-sweeps, so
treating each sweep as a sample overstates precision by roughly
`sqrt(burst / integration)` — a factor of **7** for a 5 s transmission
sampled every 100 ms.

`effective_independent_samples()` counts distinct transmission opportunities
instead, and `occupancy_standard_error()` must be fed that count. A naive
binomial error bar on occupancy from this survey would be about seven times
too small. A dwell shorter than one transmission contributes **one** sample,
not many.

---

## 5. The load-bearing assumption

> **Visit times are uncorrelated with the traffic's own periodicity.**

Everything in §3 assumes visit phases are uniformly distributed relative to
whatever the transmitter is doing. Real traffic has real periodicity: shift
changes, hourly nets, a telemetry site that reports every 15 minutes, the
top-of-hour NOAA cycle.

A fixed revisit period commensurate with any of those does not return a
noisy answer — **it returns a confident wrong one**, sampling always during
the transmission or never. `jitter_fraction` defaults to 0.25 for this
reason, applied to the dwell rather than to an idle gap so no airtime is
wasted buying it. Setting it to zero produces a recorded warning in the
dataset, not a log line.

**If one number from this survey turns out to be wrong, this is where I
would look first.** What would settle it: run the same channel with two
different jitter seeds and two different nominal revisit periods. If the
occupancy estimates agree, aliasing is not biting; if they diverge beyond
the error bars in §4, it is.

---

## 6. What this design is not

It samples **one band at a time**. A transmission on VHF while the receiver
is tuned to UHF is not recorded as absent — the channel is marked *not
observed* for that sweep, and absence and non-observation are kept distinct
all the way into the data file. Occupancy is always a fraction of *decided*
samples.

The honest summary of the whole design: it measures **duty cycles in the
10–25% range well in an hour, 1% duty cycles poorly in a night, and sub-25 ms
bursts not at all.**
