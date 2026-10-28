# Measuring NOAA Weather Radio keying

**This is a measurement target, not a reference.** NWR was this tool's
detection-threshold calibration until **2026-10-28**, when it was retracted.
Do not anchor a threshold to it.

---

## 1. Why it was retracted

The band was marked `CONTINUOUS`, duty 100%, on the reasoning that NWR is a
continuous automated broadcast — which made it look like an ideal threshold
reference, since a channel busy 100% of the time tells you whether your
detector is set right.

An operator reported from listening experience that transmissions are
finite, with dead time between them. **That is an observation against an
inference, so the inference loses.**

Re-reading the sources afterwards, the inference was weaker than it looked:
NWS documents a 24/7 service and a 3–6 minute programme cycle, but nothing
found addresses **transmitter keying**. *"The service runs 24/7"* is a claim
about **availability**; *"the carrier is continuous"* is a claim about
**keying**. Only the first is supported.

> **The general lesson, since it cost a working calibration:** an assumption
> used as a calibration is the most dangerous kind, because everything
> measured against it inherits its error silently. Prefer a control whose
> basis is a *mechanism* to one whose basis is a *schedule*. Broadcast FM
> survives that test — an FM transmitter radiates an unmodulated carrier
> through programme silence, which follows from the modulation scheme — so
> it is now the schedule's only control.

---

## 1b. What NWR *is* still good for: presence

The duty-cycle claim was retracted. A **weaker and more robust** claim
survives, and it needs no assumption about carrier keying at all:

> Over a long enough observation, **NWR must appear.**

If an extended look at 162.400–162.550 shows nothing, the fault is on the
**measuring end** — threshold too high, gain wrong, antenna or feed broken,
predicted channel wrong — because the NWS publishes the transmitter list and
its coverage. **Absence contradicts published data, not an inference.** That
is precisely why it holds where the duty-cycle version did not.

### The precondition, which constrains the schedule

Absence is only evidence if the observation could plausibly have caught a
keyed carrier. A survey dwelling 1 s every 10 minutes could miss one
legitimately, and "we did not see NWR" would then be ambiguous between a
dead receive path and an aliased schedule — the worse failure, because it
sends an operator to check hardware that is fine.

So rather than a flat rule, `presence.min_detectable_duty()` computes what
the observation actually bought, from the same catch-probability model as
[sampling-design.md](sampling-design.md). **The default schedule gives NWR
only 4.2 minutes of coverage per hour** (1.7 on an RTL-SDR), so:

| run length | visits | detectable duty cycle (95%) | absence means |
|---|---|---|---|
| 1 h | 12 | ≥ 16.5% | **nothing** — inconclusive |
| 2 h | 25 | ≥ 5.7% | weak evidence |
| 4 h | 50 | ≥ 0.3% | **the receive path** |
| 8 h+ | 100 | any duty | damning |

`--plan` prints this table for *your* configuration. The verdict is gated on
it: below the `conclusive_duty` threshold (default 10%) the check reports
**`inconclusive`**, not `absent`, and says explicitly that it is not grounds
to go looking for a hardware fault.

Why 10%: a service broadcasting on a documented 3–6 minute cycle around the
clock has a duty cycle of at least tens of percent *if it transmits at all*,
and that much follows from the published cycle rather than from any claim
about carrier continuity. The bound itself is always recorded, so a reader
can apply a different cutoff without re-running anything.

### The stronger version: which channel should be loudest

NWR is seven fixed channels and the NWS publishes each transmitter's
location and power, so the prediction can name the one that *should*
dominate. That turns one outcome into three:

| outcome | what it indicts |
|---|---|
| nothing at all | the **receive path** |
| wrong channel strongest | the **prediction**, or the antenna pattern |
| right channel, wrong level | the **path loss** |

```bash
python tools/qth_predict.py --lat <LAT> --lon <LON> --radius 90000 --bands nwr
python -m occupancy_survey --config config.nwr.yaml --prediction out/qth_prediction/prediction.json
```

**Use a wide radius.** NWR transmitters run 300 W–1 kW and carry well past
30 km. For CM87, a 30 km radius predicted **one** channel and a 90 km
radius predicts **five** — so a thin NWR list is usually a radius artifact
rather than a gap in the data:

| channel | station | ERP/d² (uncalibrated) |
|---|---|---|
| **162.400** | **KHB49, San Francisco** | **1.6 × 10⁻⁵** |
| 162.550 | KEC49, Monterey | 1.4 × 10⁻⁷ |
| 162.450 | WWF64, Monterey Marine | 4.6 × 10⁻⁸ |
| 162.425 | WNG655, Contra Costa County | 2.8 × 10⁻⁸ |
| 162.500 | KDX54, Big Rock Ridge | 1.4 × 10⁻⁸ |

KHB49 dominates by roughly **20 dB**, which makes this an unusually clean
prediction to test: if anything on 162.400–162.550 is audible at CM87 and
it is *not* 162.400, the prediction or the antenna pattern is wrong, and the
receive path is demonstrably fine.

---

## 2. The question, and why your ear cannot answer it

| what is happening | interference duty |
|---|---|
| dead **audio** between cycles, **carrier up** | **100%** — a carrier desenses a front end whether or not it is modulated |
| dead **carrier** between cycles | **genuinely below 100%** |

Both are silence from a speaker. A receiver logging carrier-present
separates them trivially — which is exactly what this tool does.

It matters because 162.400–162.550 is the closest **continuous-service**
allocation to the 2 m band in the whole `sota-rf` catalogue, about 14 MHz
out, and the risk model leans on it: KWO37 on Mt Lukens drives that summit's
2 m QRM rating to High on the strength of this assumption.

---

## 3. The normal schedule cannot answer it

The default schedule is a **20 s dwell on a ~287 s cycle**. The documented
NWR programme cycle is **180–360 s**. Two separate problems:

- **The longest run you can observe is one dwell.** A 20 s dwell cannot
  measure a carrier-on period of minutes — the run is cut by the retune, and
  this tool deliberately *discards* interrupted runs rather than recording
  them short, so the period simply never appears.
- **The revisit period overlaps the signal period.** This is precisely the
  commensurate-period aliasing [sampling-design.md](sampling-design.md) §5
  warns about. Jitter keeps the *duty cycle* estimate honest, but it does
  nothing for the *period*.

So this measurement needs **one band, one tuning, one uninterrupted dwell.**

---

## 4. The run

```bash
python -m occupancy_survey --config config.nwr.yaml --duration 7200
```

That is `config.nwr.yaml`: the `nwr` band alone, one tune step, `dwell_s`
longer than the run, so the receiver never retunes and the carrier is logged
continuously at the integration period.

Two hours covers 20–40 programme cycles, which is enough to see whether the
period is stable. An hour would do; overnight would also show whether the
pattern changes outside working hours.

Then:

```bash
python tools/merge_datasets.py data/occ_*.json.gz --band nwr
```

---

## 5. Reading the answer

Per channel, the dataset carries `occupancy`, `runs`, `gaps` and `keying`.
All durations are in **sweeps**; multiply by `integration_s` for seconds
(the band record states it).

| what you see | what it means |
|---|---|
| `keying.cycles_observed: 0`, `gaps.n: 0`, occupancy ~1.0 | **Carrier is continuous.** The dead time is audio only, the retracted assumption was right after all, and the interference duty really is 100%. |
| `keying.period_sweeps` ≈ 180–360 s worth, `regularity` < 0.15 | **Carrier keys on a clock.** Duty cycle is `duty_from_cycles`; the row in `duty_cycles.md` becomes that number, measured. |
| gaps present but irregular | Something else is going on — check for fading, a marginal threshold, or an adjacent-channel transmitter. Re-run with a higher threshold and compare. |
| occupancy ~0 | No NWR transmitter is receivable here, or the receive path is wrong. Check the FM control band in the same run before concluding anything. |

**The period is as interesting as the duty cycle.** A 3–6 minute cycle is
slow enough that an activator experiences it as the band going quiet and
loud again, rather than as a steady impairment — the "quiet-but-spiky versus
loud-but-steady" distinction that `duty_cycles.md` §1 is about. A 20% duty
cycle in 3-minute blocks and a 20% duty cycle in half-second blocks are the
same number and completely different problems.

---

## 6. Reporting it

Whatever comes out, it carries its acquisition conditions: threshold,
baseline estimator, gain, integration period, antenna, and the front-end
filter state. A duty cycle without those is not a measurement.

If the result does establish a continuous carrier, NWR may become a control
again — but only on the strength of **that measurement**, not on a
re-reading of the same sources. `tests/test_front_end.py` has a regression
guard that fails if the band is restored to `CONTROL` without one.
