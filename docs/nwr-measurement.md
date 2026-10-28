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
