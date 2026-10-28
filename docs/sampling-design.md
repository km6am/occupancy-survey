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

### Cycle time depends on the receiver's bandwidth, not just the schedule

A band wider than the receiver's usable bandwidth is covered in several
tunings, so the same schedule costs very different airtime on different
hardware — and every catch probability in §3 scales with it:

| configuration | tune steps | cycle | visits/h | P(1% channel seen in 1 h) |
|---|---|---|---|---|
| RSPdx @ 6 MHz, 5 bands | 14 | 287 s | 12 | **0.62** |
| RTL-SDR @ 2.048 MHz, 5 bands | 34 | 697 s | 5 | **0.33** |
| RTL-SDR @ 2.048 MHz, volunteer's 4 bands | 21 | 430 s | 8 | **0.47** |

**The figures in the rest of this document are the first row**, 6 MHz, which
is what `SamplingDesign()`'s defaults assume. An RTL-SDR at 2.048 Msps
roughly halves every catch probability, because the 20 MHz UHF band alone
costs it 10 tune steps. That is the main reason the volunteer schedule drops
`uhf_lmr`: it is most of the airtime for the band least likely to be heard
on a whip.

`--plan` prints the figures for **your** configuration. Use those, not
these.

### Targeting helps less than you would expect

An FCC-derived channel list (`tools/make_channel_list.py`) looks like it
should collapse the cycle, since it drops 2494 grid channels to 188.
Measured at CM87 it does not:

| schedule | channels | tune steps | cycle | P(1% channel in 1 h) |
|---|---|---|---|---|
| grid, 6 MHz | 2494 | 14 | 287 s | 0.62 |
| **targeted, 6 MHz** | **188** | **10** | **205 s** | **0.74** |

A 30% saving, not the order of magnitude I first assumed. Licensed
channels are **spread across** their allocations rather than clustered, so
the tuning windows still cover nearly the same span — the targeted VHF set
spans 4.99 MHz of a 5.2 MHz band and needs the same two steps.

Targeting is still worth doing, for exact frequencies (no grid-offset
splitting) and far smaller files. But **if you need airtime, drop a band**
— removing `uhf_lmr` and `bcast_fm` saves more than targeting does.

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

**Those hours must be DAYTIME hours.** Land mobile is dispatch and business
traffic, so a duty cycle pooled over the whole clock is diluted by the
quiet hours and understates what a daylight operation meets. Taking
08:00–18:00 as ten usable hours a day, the 1% channel needs about **one
day** to be seen and **two days** to be estimated. Overnight data is worth
keeping — the diurnal shape is a result in itself — but it belongs in a
separate figure, not in the headline one. `merge_datasets.py --hours 08-18`
is how that figure is produced.

"An afternoon" — the phrase in `duty_cycles.md` §6 — is enough for the
10–25% conventional-LMR figures that document actually wants, and not for
anything near 1%.

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

## 4b. The other floor: what an empty channel reads

Integration length is not the only floor. A channel carrying **nothing**
still reads some occupancy, because noise occasionally clears the
threshold, and that rate is set almost entirely by **bins per channel**:

| bins/channel | occupancy an empty channel reads (10 dB threshold) |
|---|---|
| 1 | **5.6%** |
| 2 | 0.20% |
| 4 | 0.0019% |
| 12 | 0.0000003% |
| 22 *(default config)* | negligible |

A one-bin channel is catastrophic: a 5.6% floor swamps the 10–25% duty
cycles this survey exists to measure. This was found by ingesting an
`rtl_power` CSV whose 12.5 kHz bins made each 12.5 kHz channel exactly one
bin wide — ninety noise-only channels reported 1.7–15% occupancy.

`false_alarm_rate()` computes it, `--plan` prints it per band, and every
dataset records `noise_floor_occupancy` per band so a reader knows whether a
3% figure might be nothing at all.

**The fix is a finer FFT bin, not a higher threshold.** Raising the
threshold buries the false alarms and the weak real signals together; more
bins per channel improves sensitivity and the false-alarm rate at once.

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
