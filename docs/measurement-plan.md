# Measurement plan

How the survey is actually run and how the numbers come out. Five runs,
cheapest and most diagnostic first, so a fault is caught in minutes rather
than after an overnight.

**Two machines.** The *analysis host* has `sota-rf`, pandas and matplotlib
and does the prediction and the reporting. The *survey host* has numpy,
PyYAML and a backend, and does nothing but acquire. The only thing crossing
between them is a small JSON each way.

---

## Phase 0 — prediction and channel list (analysis host, once)

```bash
export SOTA_RF_DIR=/path/to/sota-rf

python tools/qth_predict.py --lat <LAT> --lon <LON> --alt-m <M> \
    --radius 90000 --out out/prediction.json

python tools/make_channel_list.py --prediction out/prediction.json \
    --top-per-band 60 --out out/channels.json
```

**Use ≥90 km.** NWR transmitters are 300 W–1 kW sited for area coverage and
broadcast FM is further still; a 30 km radius under-reports both. Copy
`out/channels.json` and `out/prediction.json` to the survey host.

Output of this phase: a targeted channel list with a `near_summit` tag per
channel, and the predicted-strongest channel per band.

---

## Phase 1 — host readiness (survey host, minutes)

```bash
python -m occupancy_survey --config config.yaml --check
python -m occupancy_survey --config config.yaml --check --device
python -m occupancy_survey --config config.yaml --plan
```

`--check` verifies imports, config, disk and clock without touching the
radio; `--device` opens it for one sweep and reports bin count, span and
clip fraction. `--plan` prints this configuration's blind spot, detection
edge and catch probabilities.

**Stop here if** `--check` fails, or clip fraction > 1e-4 (front end
overloaded at the configured gain).

---

## Phase 2 — gain and filter commissioning (survey host, minutes)

Fit the band's external bandpass, set a fixed gain, then:

```bash
python -m occupancy_survey --config config.yaml --intermod-check \
    --bands vhf_lmr_low
```

Reports each detected channel's implied nonlinearity order — 1 for a real
signal, 3 for a third-order product.

| result | action |
|---|---|
| suspect fraction ≥ 20% | **fix before proceeding.** Bandpass, then notches, then less gain — in that order, because the first two cost no sensitivity |
| a few suspect channels | note them; treat their occupancy as unmeasured |
| none | proceed |

**Then do not touch the gain again.** It is recorded per dataset and a
campaign whose gain changed midway cannot be pooled across the change.

---

## Phase 3 — the runs

### Run A — receive-path validation (1 hour)

```bash
python -m occupancy_survey --config config.yaml \
    --prediction prediction.json --duration 3600
```

Go/no-go. One hour suffices **because detection is fast**: a mostly-on
carrier is caught by a single 20 s visit with ~96% probability.

| check | pass | fail means |
|---|---|---|
| `bcast_fm.control_check` | `pass` | threshold, gain, antenna or feed — **stop** |
| `nwr.presence_check` | `present_as_predicted` | see below |

`nwr` outcomes: `present_wrong_channel` indicts the prediction or antenna
pattern, not the receive path — note it and continue. `inconclusive` is
expected at one hour if NWR turns out to be sparse; continue, it resolves
in Run C.

### Run B — NWR keying (2 hours, dedicated)

```bash
python -m occupancy_survey --config config.nwr.yaml --duration 7200
```

One band, one tuning, **no retunes** — the band-stepping schedule
structurally cannot measure a period, because a run longer than one dwell
is cut by the retune and discarded. Two hours covers 20–40 programme
cycles.

Settles the question an ear cannot: dead **audio** with the carrier up
(interference duty still 100%) versus dead **carrier** (genuinely less).

### Run C — the occupancy campaign (**daytime**, several sessions)

```bash
python -m occupancy_survey --config config.yaml \
    --channels channels.json --prediction prediction.json
```

The actual deliverable, and **the target is daytime, not duration.**

Land mobile is dispatch, business and public-safety traffic, so its duty
cycle is strongly diurnal — and the operations this feeds happen in
daylight. **A figure pooled over the whole clock is diluted by the quiet
hours and understates what a daytime operation meets**, which is an error
in the dangerous direction and the same mistake `duty_cycles.md` §1 warns
about: averaging across a condition that varies and quoting one number.

Running overnight is fine and costs nothing — the data is kept and the
diurnal shape is itself a result. But **the headline number is computed
from daytime intervals only**, and the two are reported separately.

Budget in *daytime* hours (taking 08:00–18:00, so ten per day):

| goal | daytime hours | sessions |
|---|---|---|
| 10–25% conventional LMR to ±30% | ~1 h | one morning |
| **1% channel: seen at all** | ~3 h | one day |
| **1% channel: estimated to ±30%** | ~13 h | **two days** |

So two full weekdays of daytime running is the realistic target for the
low-duty channels, and a single morning settles the TIA-603-range figures
that `duty_cycles.md` most wants.

Keep a grid band in the schedule alongside the targeted list, or a busy
channel with no FCC record cannot be found at all.

### Run D — aliasing control (repeat of C, different seed)

Re-run C with a different jitter seed and a different nominal
`revisit_target_s`. **This is the step most likely to expose an error.**
Every catch-probability bound assumes visit phases are independent of
traffic periodicity; if the two runs' occupancy estimates agree within the
error bars, aliasing is not biting. If they diverge, it is.

---

## Phase 4 — results (analysis host)

```bash
# THE HEADLINE NUMBER: daytime only.
python tools/merge_datasets.py data/occ_*.json.gz --hours 08-18 \
    --band vhf_lmr_low --json-out out/service_daytime.json

# The diurnal shape, and the 24-hour figure for comparison.
python tools/merge_datasets.py data/occ_*.json.gz --by-hour vhf_lmr_low
python tools/merge_datasets.py data/*.json.gz --json-out out/service_all.json

python tools/validate_occupancy.py --prediction out/prediction.json \
    data/occ_*.json.gz --plot out/qth_validation.png
```

Produces:

1. **Service-level occupancy, daytime** — the deliverable. Reported two
   ways, and the distinction matters: `active_channel_occupancy` is
   comparable with the TIA-603 figures in `duty_cycles.md`, which describe
   a channel *in use*; `mean_channel_occupancy` averages over all grid
   channels including the empty ones.
   Without `--hours`, `merge_datasets.py` warns when the intervals span
   more than eight hours of the clock, because the pooled figure is then
   diluted by the quiet hours.
1b. **The diurnal profile** (`--by-hour`) — a band busy 09:00–17:00 and
   quiet at 03:00 tells an activator something a 24-hour mean does not.
2. **The NWR keying answer** — period, duty, and whether the carrier stops.
3. **The validation figure and report** — predicted vs measured, by
   continuity class, with the predicted-but-silent and busy-but-unpredicted
   lists.

Re-threshold any of it without re-acquiring:
`--threshold-db 14`. That is what the stored histograms are for.

---

## What the numbers mean, and what they do not

**Every figure carries its acquisition conditions** — threshold, baseline
estimator, gain, dwell, integration, antenna, filter state. A duty cycle
without those is not a measurement, and the merge tools refuse to drop them.

**A QTH is not a summit.** Duty cycle *transfers* between locations —
an 18% dispatch channel heard here is 18% for an activator beside that
transmitter, because the path changes how *loud*, not how *often*. Field
strength does **not** transfer. This survey supplies the first; `sota-rf`
supplies the second; they compose in the risk model and must not be
conflated in a column name or a plot title.

**These become the first entries in `duty_cycles.md`'s empty `measured`
column** — carried with their conditions, and proposed to that repo rather
than written into it.

### The three steps most likely to be wrong

1. **Schedule aliasing.** Run D settles it.
2. **Intermod.** Phase 2 settles it; unlike thermal false alarms it does
   not average out, and it makes the validation plot look *better* while
   being wrong.
3. **The uncalibrated ERP/d² proxy.** No terrain, no antenna pattern — a
   predicted-strong channel reading 0% may be a shadow, not a bad record.

---

## Appendix — prediction for this QTH (CM87, 90 km, 2026-10-29)

Exact coordinates, not the grid-square centre. The prediction file holds
them and lives in `out/`, which is gitignored; only the grid square ever
reaches a dataset.

**47,490 predicted channels.** Continuity split **40,465 intermittent /
6,944 continuous / 81 unknown**.

| service | channels | continuity (architectural) |
|---|---|---|
| IG — industrial/business, conventional | 35,169 | intermittent |
| **YG — industrial/business, trunked** | **6,445** | **continuous** |
| PW — public safety, conventional | 5,234 | intermittent |
| YW — public safety, trunked | 291 | continuous |
| YK / IK — 470–512 MHz | 206 | as YG / IG |

The 6,445 `YG` channels are the reason this QTH is a good test bed: the
trunked-means-continuous claim in `duty_cycles.md` §2 is what reclassifies
emitters in the risk model, and there is a large sample here to test it
against a 5,234-channel conventional comparison group.

### Reference bands

| band | predicted | strongest |
|---|---|---|
| **bcast_fm** (control) | 140 | KIOI 101.3 MHz, 1.19e-04 |
| **nwr** (presence) | 5 | **KHB49 162.400 MHz, 5.28e-06** |

KHB49 leads the next NWR station by **35× (15.4 dB)**, so the
predicted-strongest check is unambiguous: if anything in 162.400–162.550 is
audible and it is *not* 162.400, the prediction or the antenna pattern is
wrong and the receive path is fine.

### Strongest VHF targets

| frequency | proxy | service | licensee |
|---|---|---|---|
| **151.4750** | 2.15e-05 | PW | Telecommunications Engineering Assoc. |
| 159.5250 | 5.15e-05 | IG | Business Radio Licensing |
| 153.7850 | 2.15e-05 | PW | Telecommunications Engineering Assoc. |

### The targeted list

252 channels, **56 tagged as near a W6 summit** — mostly broadcast FM on
W6/CC-072 and W6/NC-407, with 28 across the two VHF target bands. Tagged,
not filtered: duty cycle transfers between locations and field strength
does not, so the tag exists to test whether summit-co-sited emitters are
systematically busier rather than to assume it.

### What the exact coordinates changed

Worth recording, because it quantifies a caveat stated earlier:

| | grid-square centre | exact |
|---|---|---|
| strongest VHF channel | **151.4750 MHz** | **151.4750 MHz** |
| its field proxy | 2.60e-04 | 2.15e-05 |

**The frequency ranking held; the level did not** — a 12× (10.8 dB) change
for an emitter at 2,638 m. That is almost exactly the ~10 dB error
predicted for a 2 km emitter under 4.35 km of grid-square uncertainty. So a
grid square is adequate for deciding *what to measure* and inadequate for
*how strong it should be* — which is the half the validation plot's x-axis
depends on.
