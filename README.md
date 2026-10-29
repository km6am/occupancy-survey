# occupancy-survey

Long-term channel-occupancy monitor. Steps band to band on a schedule and
logs, per channel, **what fraction of the time a carrier is present** — plus
a count of active channels per band per interval.

The output is service-level statistics: *"VHF business-pool channels around
here are busy X% of the time."*

It exists to fill one gap. [`duty_cycles.md`][dc] in `hp8935_sinad_serial`
turns `sota-rf`'s per-emitter list into an interference prediction by
assigning each radio service a duty cycle — and every figure in it comes
from a published standard or from system architecture. Its `measured` column
is **empty**. This tool produces the first entries in it.

**It logs energy, not content.** No audio, no IQ, no demodulation. That is
structural: the only thing the radio layer hands upward is a power spectrum.
See [`backends/base.py`](occupancy_survey/backends/base.py).

## Quick start

```bash
pip install .
cp config.example.yaml config.yaml          # edit the site block
occupancy-survey --config config.yaml --check   # is this host ready?
occupancy-survey --config config.yaml --plan    # what can it and can it not see?
occupancy-survey --config config.yaml --backend synthetic --cycles 3
```

Needs Python 3.10+, numpy and PyYAML. **Full instructions, per platform
and per backend, are in [INSTALL.md](INSTALL.md)** — including GNU Radio
and the SDRplay API, which are not pip-installable.

`--check` and `--plan` touch no hardware. Run both before committing to
anything long: `--plan` tells you what the configuration *cannot* see.

While a survey runs it prints, once a minute, how many channels have a
carrier and what their duty cycles are — so you can confirm it is working
without waiting for the first hourly file:

```
[00:04:00] 3 band(s) visited, 6 channel(s) with a carrier
      151.4750 MHz   38.2%  n=25,044  vhf_lmr_low
      154.0550 MHz   21.4%  n=25,044  vhf_lmr_low
    control bcast_fm: 100.0% (pass)
```

Volunteers running an RTL-SDR should read
[docs/volunteer-quickstart.md](docs/volunteer-quickstart.md) instead — one
page, no radio knowledge assumed.

> **Measure in the daytime.** Land mobile is dispatch and business
> traffic, so its duty cycle is strongly diurnal. A figure pooled over the
> whole clock is diluted by the quiet hours and understates what a daylight
> operation meets. Run overnight if you like — the diurnal shape is a
> result in itself — but compute the headline number with
> `merge_datasets.py --hours 08-18`.

## The five choices that are the measurement

**1. The threshold is the result.** It is noise-floor-relative — *N* dB
above a low percentile pooled across each band's channels and recent sweeps
— and it is recorded in every file alongside gain, dwell and revisit.
Better still, each channel stores a **histogram** of level-above-baseline,
so occupancy is recoverable at *any* threshold offline, and two sites that
chose differently can still be compared.

Pooling across both channels and time is not fussiness. A purely spectral
baseline is pulled up by leakage from a strong neighbour; a purely temporal
one reads **0%** on a channel occupied 100% of the time, because the carrier
becomes its own 25th percentile. Its one assumption is named in the code:
fewer than `baseline_percentile`% of a band's channels are busy at once.

> **`threshold_db` is not an SNR.** The baseline is a percentile of the
> max-bin statistic and already sits above the mean noise power, so a 10 dB
> threshold needs about **12.8 dB** of SNR in a 12-bin channel.
> `required_snr_db()` computes it; `--plan` prints it per band.

> **An empty channel is not at 0%.** Noise clears the threshold sometimes,
> at a rate set almost entirely by bins per channel: negligible at the
> default 22, but **5.6% at one bin per channel**. `--plan` prints it per
> band and every dataset records it. The fix is a finer FFT bin, not a
> higher threshold.

**2. Fixed gain, AGC off.** Not a default — AGC is absent from the config
schema, the source factory raises on it, and the dataset writer refuses a
file that claims it. An AGC moves the noise floor and takes the occupancy
number with it, and nothing downstream could detect that afterwards.

**3. Energy, not content.** See above.

**4. Dwell versus revisit, with the blind spot stated.**
[docs/sampling-design.md](docs/sampling-design.md) computes the bounds
rather than describing them. The headline, which is easy to miss:

> The default design **catches a 1% duty-cycle channel only 62% of the time
> in an hour**. 95% confidence of merely *seeing* it takes ~3 hours;
> *estimating* its duty cycle to ±30% takes ~13.

Also: the revisit interval is **jittered** by default, because a fixed
period commensurate with real traffic periodicity returns a confidently
wrong answer rather than a noisy one. That is the step most likely to be
wrong, and the doc says what would settle it.

**5. A QTH is not a summit.** This yields duty cycles per service, which is
what the risk model lacks. It does **not** yield summit field strengths —
different propagation, different antenna. There is no field-strength field
in the schema and no plot title that implies one.

## Validating against `sota-rf`

```bash
python tools/qth_predict.py --lat <LAT> --lon <LON> --radius 30000
python tools/validate_occupancy.py --prediction out/qth_prediction/prediction.json data/occ_*.json.gz
```

`qth_predict.py` points `sota-rf` at a fixed location through a one-row
pseudo-summit file — **read-only**: existing FCC cache, `--no-download`,
scratch output directory, nothing written into that repo.

The figure has two panels and only the second one earns anything. Occupancy
against predicted field strength is merely *consistent* with the model —
a detector that fired on noise would also put the strong emitters on top.
Occupancy **by continuity class** is the discriminating comparison:
`duty_cycles.md` §2 claims from architecture alone that trunked sites
(`Y`-prefix service codes) are continuous while conventional land mobile is
intermittent at 10–25%. Those predict different distributions, so the
measurement can tell them apart — and can fail.

The control band does double duty: it calibrates the detection threshold,
and the weakest control channel actually heard sets an **empirical
audibility floor** that makes "predicted but never active" mean something.

> **Only broadcast FM is a control**, and the list is short on purpose.
> NOAA Weather Radio was one until 2026-10-28, on the reasoning that a 24/7
> automated service implies a continuous carrier. It does not — *"the
> service runs 24/7"* is a claim about **availability**, and transmitter
> **keying** is a separate question no source addressed. An operator's
> listening experience contradicted it, so the row was retracted and NWR
> is now a measurement target. FM survives the same scrutiny because the
> claim is about the modulation scheme rather than a schedule: an FM
> transmitter radiates an unmodulated carrier through programme silence.
>
> The lesson, since it cost a working calibration: **an assumption used as
> a calibration is the most dangerous kind**, because everything measured
> against it inherits its error silently. Prefer a control whose basis is a
> *mechanism* over one whose basis is a *schedule*.
> [docs/nwr-measurement.md](docs/nwr-measurement.md) covers the
> measurement that would settle it.
Without it that list is thousands of distant low-power licences — a real
smoke test returned 11,980 predicted channels for one location.

## Merging

```bash
python tools/merge_datasets.py data/occ_*.json.gz --hours 08-18 --band vhf_lmr_low
python tools/merge_datasets.py data/occ_*.json.gz --by-hour vhf_lmr_low
python tools/merge_datasets.py data/*.json.gz --threshold-db 14   # no re-acquisition
```

Histograms are in dB above each receiver's *own* baseline, so sites with
different gains, antennas and noise floors pool onto a common axis. See
[docs/data-format.md](docs/data-format.md) — including the two service-level
averages, and why quoting the wrong one makes every measured duty cycle look
an order of magnitude below the standard it replaces.

## Hardware, and why the front end decides the result

This is a **large-signal** problem before it is a sensitivity problem. The
strongest carrier in the passband sets the gain; everything weaker must
clear the noise floor at that gain. Near a city the spread between
broadcast FM and land mobile is 70–90 dB, which is more instantaneous
dynamic range than a cheap receiver has.

| receiver | ADC | usable range | for this survey |
|---|---|---|---|
| **SDRplay RSPdx** | 14-bit | ~75–85 dB | **preferred** — tracking preselect + switchable FM/DAB notches |
| RSPduo | 14-bit | ~75–85 dB | two tuners: a control band and a target band at once |
| Airspy R2 / Mini | 12-bit | ~70–80 dB | good front end, no built-in notches |
| USRP B2xx | 12–14-bit | ~70–85 dB | no preselection; needs external filters |
| RTL-SDR | 8-bit | ~45–50 dB | **needs an external bandpass** to be usable here at all |

Those figures are *bounded/inferred* from bit depth and published specs,
not measured — and the binding constraint is usually the tuner's IP3 rather
than the ADC. `--intermod-check` is what settles it for a given site.

> **The error that flatters you.** Intermod products land on empty channels
> *exactly when the strong carriers key up*, so they correlate with the
> predicted-strong emitters and make the validation plot look **better**
> while being wrong. Unlike a thermal false alarm, it does not average out.
> [docs/front-end.md](docs/front-end.md) covers the three filter stages,
> the per-band notch plan, and the gain-setting procedure.

```bash
python -m occupancy_survey --config config.yaml --intermod-check
```

Changes gain by 6 dB and reports each channel's implied nonlinearity order
— 1 for a real signal, 3 for a third-order product. Fix with a filter
before reaching for gain: a bandpass costs no sensitivity, less gain does.

| backend | needs | notes |
|---|---|---|
| `gnuradio` | radioconda + gr-sdrplay3 | RSPdx and other high-performance SDRs; actuates the notch filters |
| `rtl_sdr` | the `rtl_sdr` binary + numpy | reach over sensitivity; external filter effectively required |
| `rtl_power` | the `rtl_power` binary | fallback; **averages before thresholding** |
| `synthetic` | nothing | verify an install, exercise the pipeline |

On this Mac the RSPdx path needs two repairs first, both diagnosed and
neither needing `sudo` — see
[docs/rspdx-on-macos.md](docs/rspdx-on-macos.md).

`rtl_power` is supported but discouraged, and its datasets carry
`averaging_bias: pre-threshold`. It averages each bin over its whole
interval, which turns a 20% duty cycle into a continuous carrier 7 dB
weaker — the §1 mistake of `duty_cycles.md` moved out of the model and into
the measurement.

## Deploying to a survey host

This package is developed on one machine and **runs on another**, so the two
roles have deliberately different dependencies:

| role | needs | does |
|---|---|---|
| **survey host** | numpy, PyYAML, a backend | runs unattended, writes interval files |
| **analysis host** | + `sota-rf`, pandas, matplotlib | predicts, merges, validates, plots |

The survey host never needs `sota-rf` or a 400 MB FCC download: a targeted
channel list is a small JSON generated on the analysis host and copied
across. Point the analysis tools at a different checkout with
`SOTA_RF_DIR=/path/to/sota-rf`.

Before any long run, on the machine that will do the surveying:

```bash
python -m occupancy_survey --check            # imports, config, disk, clock
python -m occupancy_survey --check --device   # also opens the SDR, one sweep
```

`--check` does **not** open the radio; `--device` opts into that and reports
bin count, span and clip fraction. It flags an overloaded front end straight
away, which is the failure most worth catching before committing to an
overnight run.

**Data volume is not a constraint.** Measured at **57 kB gzipped per hourly
interval** for the full five-band schedule — 1.4 MB/day, 513 MB/year. The
histograms compress about 66× because most channels are empty. `--check`
reports headroom in days against the configured interval.

## Tests

```bash
python -m unittest discover
```

No hardware needed. Synthetic spectra use exponential bin power, because the
max-bin statistic depends on that tail.

## Provenance and licence

GPL v3. [`backends/gr_sdr_sources.py`](occupancy_survey/backends/gr_sdr_sources.py)
is vendored from [morse-code-recorder][mcr] with its origin and the cost of
the copy named in its header.

[dc]: https://github.com/
[mcr]: https://github.com/km6am/morse-code-recorder
