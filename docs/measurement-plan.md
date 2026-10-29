# Measurement plan

Two steps: a short test to confirm the receiver is hearing things, then an
extended run of several days.

Everything else in `docs/` is diagnostics — read it if something looks
wrong, not before.

---

## Step 1 — short test (15 minutes)

```bash
occupancy-survey --config config.yaml --check --device
occupancy-survey --config config.yaml --duration 900
```

**It states what it is about to do, then tells you as it goes.** First,
before any data exists:

```
Starting occupancy survey.
  site        qth (CM87)
  receiver    SDRplay RSPdx / discone at 6 m
  backend     gnuradio
  bands       vhf_lmr_low, nwr, vhf_lmr_high, uhf_lmr, bcast_fm
              2,494 channels, 14 tune step(s)
  threshold   10.0 dB above a 25th-percentile baseline
  schedule    20 s dwell, 287 s cycle, 25% jitter
  blind spot  bursts shorter than 25 ms
  output      ./data  (one file per 60 min)
  Ctrl-C stops and finalises the interval in progress.
```

Then, every minute, a block that **redraws in place** rather than
scrolling:

```
occupancy survey   qth   elapsed 00:04:00   0 file(s) written
  band          visits   chans  active   busiest
  vhf_lmr_low       12     417       5     151.4750 MHz  38.2%
  nwr                3       7       1     162.4750 MHz 100.0%   [reference]
  vhf_lmr_high      12     369       2     159.5250 MHz  14.1%
  uhf_lmr            4    1601       0            -- nothing detected
  bcast_fm           6     100       9     101.3000 MHz 100.0%   [pass]
  17 channel(s) with a carrier across 5 band(s) visited
  busiest channels
      162.4750 MHz  100.0%  n=24,180  nwr
      101.3000 MHz  100.0%  n=23,900  bcast_fm
      151.4750 MHz   38.2%  n=25,044  vhf_lmr_low
      ... and 14 more
```

That is the confirmation it is working: a row per band with how many of
its channels carry a carrier, and the busiest with its duty cycle.

It redraws in place on a terminal and falls back to plain appended blocks
when output is piped to a file or run under `systemd` or `nohup`, where
escape codes would be noise. `--status-interval 0` turns it off; the
default is every 60 seconds.

| what you see | what it means |
|---|---|
| some channels active, `bcast_fm` control passes | working — go to step 2 |
| nothing active anywhere | receive path. Antenna, gain, or threshold |
| everything active | front end overloaded, or threshold too low |

A strong local FM station should read close to 100%. If it does not, fix
that before collecting anything, because every other number in the run
rests on the same threshold.

---

## Step 2 — extended run (several days)

```bash
occupancy-survey --config config.yaml
```

Leave it. The same status block keeps printing once a minute, so a glance
at the terminal at any point tells you how many channels are active and
what their duty cycles are. One file per hour, about 57 kB each — a week is 10 MB, so
disk is not a consideration. `Ctrl-C` finalises the hour in progress.

Several days is the right length for two reasons: low-duty channels need
hours of samples before "quiet" means anything, and a few days covers
weekday and weekend, and day and night, so the data can be sliced
afterwards rather than decided in advance.

---

## Step 3 — results

```bash
# The headline number.
python tools/merge_datasets.py data/occ_*.json.gz --hours 08-18

# How it varies through the day.
python tools/merge_datasets.py data/occ_*.json.gz --by-hour vhf_lmr_low
```

`active_channel_occupancy` is the figure comparable with the TIA-603
numbers in `duty_cycles.md` — those describe a channel *in use*.
`mean_channel_occupancy` averages over every grid channel including the
empty ones, which is a different question.

**Use `--hours` for the headline.** Land mobile is dispatch and business
traffic, so a figure pooled over the whole clock is diluted by the quiet
hours and understates what a daylight operation meets.

Any of it can be re-derived at a different threshold without
re-acquiring: `--threshold-db 14`. That is what the stored histograms are
for.

---

## If something looks wrong

| symptom | where to look |
|---|---|
| channels active that should not be | [front-end.md](front-end.md) — intermod. Run `--intermod-check` |
| a predicted-strong channel reads 0% | terrain, or the antenna pattern. The prediction has neither |
| occupancy lower than expected | front-end compression, or the threshold. Check the FM control |
| want to know what the run can and cannot see | `--plan`, and [sampling-design.md](sampling-design.md) |
| NOAA Weather Radio specifically | [nwr-measurement.md](nwr-measurement.md) — it needs its own run |

---

## What the numbers mean, and what they do not

Every figure carries its acquisition conditions — threshold, gain, dwell,
antenna, filter state. A duty cycle without those is not a measurement,
and the tools refuse to drop them.

**A QTH is not a summit.** Duty cycle *transfers* between locations — an
18% dispatch channel heard here is 18% for an activator beside that
transmitter, because the path changes how *loud*, not how *often*. Field
strength does **not** transfer. This survey supplies the first; `sota-rf`
supplies the second.

These become the first entries in `duty_cycles.md`'s empty `measured`
column — carried with their conditions, and proposed to that repo rather
than written into it.

**The step most likely to be wrong** is schedule aliasing: the sampling
assumes visit times are uncorrelated with traffic periodicity. Running for
several days with jitter on makes that unlikely, but a repeat run with a
different jitter seed is what would settle it.

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
