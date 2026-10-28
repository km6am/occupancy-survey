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
pip install -e .
cp config.example.yaml config.yaml   # edit the site block
python -m occupancy_survey --plan    # touches no hardware
python -m occupancy_survey --backend synthetic --cycles 3
```

`--plan` is the right first command: it prints the schedule, the detection
edge, and what the configuration **cannot** see.

Volunteers running an RTL-SDR should read
[docs/volunteer-quickstart.md](docs/volunteer-quickstart.md) instead — one
page, no radio knowledge assumed.

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

The control bands do double duty: they calibrate the detection threshold,
and the weakest control channel actually heard sets an **empirical
audibility floor** that makes "predicted but never active" mean something.
Without it that list is thousands of distant low-power licences — a real
smoke test returned 11,980 predicted channels for one location.

## Merging

```bash
python tools/merge_datasets.py data/occ_*.json.gz --band vhf_lmr_low
python tools/merge_datasets.py data/*.json.gz --threshold-db 14   # no re-acquisition
```

Histograms are in dB above each receiver's *own* baseline, so sites with
different gains, antennas and noise floors pool onto a common axis. See
[docs/data-format.md](docs/data-format.md) — including the two service-level
averages, and why quoting the wrong one makes every measured duty cycle look
an order of magnitude below the standard it replaces.

## Hardware

| backend | needs | notes |
|---|---|---|
| `synthetic` | nothing | verify an install, exercise the pipeline |
| `rtl_sdr` | the `rtl_sdr` binary + numpy | the volunteer path |
| `gnuradio` | radioconda + gr-sdrplay3 | RSPdx; 12-bit, the better receiver |
| `rtl_power` | the `rtl_power` binary | fallback; **averages before thresholding** |

On this Mac the RSPdx path needs two repairs first, both diagnosed and
neither needing `sudo` — see
[docs/rspdx-on-macos.md](docs/rspdx-on-macos.md).

`rtl_power` is supported but discouraged, and its datasets carry
`averaging_bias: pre-threshold`. It averages each bin over its whole
interval, which turns a 20% duty cycle into a continuous carrier 7 dB
weaker — the §1 mistake of `duty_cycles.md` moved out of the model and into
the measurement.

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
