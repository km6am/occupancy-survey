# The front end: dynamic range, filters, and the error that flatters you

The receiver's front end is part of this measurement, not a detail of it.
A wideband survey near a city is a **large-signal** problem long before it
is a sensitivity problem.

---

## 1. The error that flatters you

Two failure modes, neither of which announces itself in the data:

**Compression.** A strong carrier drives the front end toward saturation
and the noise floor across the *whole* passband rises. Every threshold in
this tool is noise-floor-relative, so weak channels fall below it and
**occupancy reads low**. The dataset looks like a quiet band.

**Intermodulation.** Third-order products of two strong carriers land on
channels where nothing is transmitting. Occupancy reads **high** on empty
channels.

Intermod is the worse of the two, and it is worse in a specific way:

> The products appear **exactly when the real transmitters key up**. So
> they correlate with the strong predicted emitters, and they make the
> prediction-versus-measurement plot look **better** while being wrong.

A thermal false alarm (see [sampling-design.md](sampling-design.md) §4b) is
random, so it averages out and shows up as a floor. An intermod false alarm
is structured like real traffic: it survives a long run, it survives
averaging, and it lands on exactly the channels that make the model look
right. Nothing downstream can distinguish it from a transmission.

**Test for it rather than hoping:**

```bash
python -m occupancy_survey --config config.yaml --intermod-check
```

Reduce the **RF** (LNA) gain by *G* dB and watch each channel's
**absolute** level:

| what it is | level falls by | order = drop / *G* |
|---|---|---|
| a real signal | *G* | **1** |
| 2nd-order product | 2*G* | 2 |
| 3rd-order product | 3*G* | **3** |
| a receiver spur | ~0 | **< 0.4**, "internal" |

Anything materially above 1 is being generated inside the receiver. This is
a *discriminating* test — the hypotheses predict different numbers and the
measurement can land on either side.

Three choices in it were each forced by a measurement (RSPdx, CM87,
2026-11-01):

- **RF, not IF.** A 6 dB IF step moved the floor, real signals and spurs
  all by 6 dB: the IF attenuator is after the stages that compress, so it
  scales a product exactly like a signal. An earlier version stepped IF
  and could never have found front-end intermod.
- **Absolute levels, not excess over the floor.** The floor here is the
  receiver's own: it moved 0–0.7 dB for an 8.3 dB RF step. Measured as
  excess, every real signal would have read order ~1.9.
- ***G* is measured,** as the median drop of strong, stable channels. The
  readback said 12 dB at UHF where signals fell 8.3. The readback is a
  fallback when fewer than two strong channels exist, and the output says
  when it was used.

Its confound, stated: intermittent traffic. So the check reads base gain,
reduced gain, then base gain again, and a channel whose two base readings
disagree is set aside as **unstable** rather than classified. A weak
product sinks into the floor at reduced gain; when a real signal of the
same level would have stayed clearly visible, the vanishing bounds the
order from below and is reported as product-like. The output is a
suspicion to resolve with a filter or a repeat that reproduces it, not a
verdict.

---

## 2. Why dynamic range decides what you can measure

The strongest signal in the passband sets the gain. Everything weaker has
to clear the noise floor *at that gain*. So what matters is **instantaneous
dynamic range within one tuning**, not sensitivity.

A concrete case, this survey's own QTH (CM87, SF peninsula): broadcast FM
transmitters ~25 km away, versus the land mobile this survey is about. The
spread between them at the antenna is on the order of **70–90 dB**
(*inferred* from the ERP/d² ratios in the prediction — not measured, and
the antenna's own frequency response is not in it).

| receiver | ADC | usable dynamic range | notes |
|---|---|---|---|
| RTL-SDR | 8-bit | **~45–50 dB** | below the spread above; cannot hold FM and LMR in one tuning |
| Airspy R2 / Mini | 12-bit | ~70–80 dB | good front end, no built-in notches |
| **SDRplay RSPdx** | 14-bit | **~75–85 dB** | tracking preselect + switchable MW/FM/DAB notches |
| RSPduo | 14-bit | ~75–85 dB | two tuners; can hold a control band and a target band at once |
| USRP B2xx / Ettus | 12–14-bit | ~70–85 dB | no built-in preselection; needs external filters |

Status of that column: **bounded / inferred**, from ADC bit depth and
published specifications, not measured on this bench. The binding
constraint in practice is usually the **tuner's IP3, not the ADC**, so
treat these as an ordering rather than as numbers to design against. The
`--intermod-check` is what actually settles it for a given site.

The consequence: **an RTL-SDR cannot measure VHF land mobile at this QTH
without an external filter.** Not "measures it less well" — the FM carriers
alone exceed its dynamic range, so without filtering its LMR occupancy
numbers are whatever its nonlinearity produces. With a 150–174 MHz bandpass
in front of it, it becomes usable, because the large signals never reach it.

---

## 3. Three filter stages

**Stage 1 — external, ahead of the receiver.** The only stage that helps a
receiver whose front end is already being overloaded, because it attenuates
before any active device. A band-appropriate bandpass is the single highest
-value accessory for this survey. `BAND_PLANS[...].front_end.external` names
what each band wants; it is **recorded, never actuated** — the tool cannot
see what is in the coax, and a dataset that assumed a filter that was not
fitted would be worse than one admitting it does not know. Put it in
`site.front_end_filters`.

**Stage 2 — the receiver's own.** The RSPdx has tracking preselection plus
switchable MW, FM and DAB notches. These are per-band and this tool
actuates them, recording what it applied:

| band | FM notch | DAB notch |
|---|---|---|
| `vhf_lmr_low` / `vhf_lmr_high` / `uhf_lmr` | **on** | on |
| `nwr` | **on** | on |
| `bcast_fm` | **off** | off |

> **The interaction that bites.** The FM notch must be **off** for the
> broadcast-FM control band and **on** for everything else. Leave it on for
> the control and the control reads ~0%, which declares the whole run
> untrustworthy — for a fault that is in the filter plan, not in the
> receive path the failure message points at.

Because a notch reshapes the passband, the noise floor moves, so every
baseline is discarded on a filter change exactly as it is on a retune.

**Stage 3 — digital, in software.** The per-channel integration in
[detector.py](../occupancy_survey/detector.py). It cannot help with
stages 1 and 2: by the time a product exists in the ADC output, no amount
of digital filtering removes it.

---

## 4. Setting gain, once, for a campaign

1. Fit the band's external filter.
2. Set a fixed gain. Start conservative — for the RSPdx, IF gain reduction
   around 40 dB.
3. Run `--intermod-check` on the busiest target band.
4. If suspect channels appear: **filter first, gain last.** A bandpass or a
   notch costs no sensitivity; reducing gain costs it directly.
5. Check `clip_fraction` in the first dataset. Above ~1e-4 the front end is
   being driven into the rails and the noise floor — hence every threshold
   — is suspect.
6. **Then do not touch it again.** Gain is recorded per dataset, and a
   campaign whose gain changed midway cannot be pooled across the change.

AGC is refused throughout, in the config schema, the source factory and the
dataset writer. An AGC moves the noise floor and takes the occupancy number
with it, and nothing downstream could detect that afterwards.
