# occupancy-survey

[![tests](https://github.com/km6am/occupancy-survey/actions/workflows/tests.yml/badge.svg)](https://github.com/km6am/occupancy-survey/actions/workflows/tests.yml)

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

It opens by stating what it is about to do — site, receiver, bands,
threshold, blind spot, where the output goes — and then shows a per-band
summary that **redraws in place** once a minute, so you can confirm it is
working without waiting for the first hourly file:

```
occupancy survey   qth   elapsed 00:04:00   0 file(s) written
  band          visits   chans  active   busiest
  vhf_lmr_low       12     417       5     151.4750 MHz  38.2%
  nwr                3       7       1     162.4750 MHz 100.0%   [reference]
  bcast_fm           6     100       9     101.3000 MHz 100.0%   [pass]
  17 channel(s) with a carrier across 5 band(s) visited
```

In-place on a terminal, plain appended blocks when piped to a log.

To hand this to a stranger, build the kit rather than sending the repo:

```bash
python tools/make_volunteer_bundle.py --out volunteer-kit.zip
```

98 KiB, 25 files, runs with only `numpy` and `PyYAML` — verified by
extracting it and running `--check` from the extracted copy. It leaves out
the analysis tools, the GNU Radio backends, the tests and the FCC
pipeline: none are needed to contribute, and a smaller kit is one a
volunteer can actually read before running it, which is the right amount
of trust to ask of someone who was sent a zip by a stranger.

Every file going in is scanned by `occupancy_survey/disclosure.py` and a
high-severity finding **refuses** the build. That scanner exists because
`tools/make_channel_list.py` used to copy the prediction's `query` block
into the channel list verbatim — so the one file designed to be handed to
volunteers carried this site's exact latitude and longitude, plus absolute
paths holding the account name. Fixed; coordinates are now reduced to a
4-character grid square and paths to basenames.

The kit also ships the scanner itself, so a volunteer can audit a file
before sending it without taking anyone's word for what is in it:

```bash
python -m occupancy_survey.disclosure data/occ_*.json.gz
```

**What the scanner cannot fix, and the kit says so in writing.** Local hour
of day is load-bearing — land-mobile traffic follows the local working day
— and combined with the UTC timestamp it reveals the UTC offset. And the
occupancy data *is* a location fingerprint: which public-safety channels a
receiver hears identifies a region more sharply than a 4-character grid
square does. Neither is removable without destroying the measurement, so
both are stated for informed consent instead. A volunteer who is promised
privacy and later works out that their channel list identifies their county
will not send a second file.

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

### Three prediction engines

| | `--engine report` | `--engine qth-cli` | `--engine pipeline` (default) |
|---|---|---|---|
| runs | nothing — ingests a file | `web/map/qth_cli.mjs` | `sota_rf_sources.py` |
| terrain | SRTM diffraction | SRTM diffraction | none — every path is line of sight |
| channels per site | all | all | lowest 40 |
| ordering | proxy with terrain and the FM vertical pattern | same | ERP/d², frequency-blind |
| level | `p_dbm_ref` with an `error_db` band | same | none |
| writes | `qth-prediction/2` | `qth-prediction/2` | `qth-prediction/1` |
| needs | **nothing but a browser** | Node ≥ 18, the map's exported tiles | Python, the FCC cache |

All three emit `field_proxy_w_per_m2` with the same meaning and units, so
one validator reads any of them; `/2` adds `p_dbm_ref` and `path`, which is
what makes the audibility floor frequency-aware and the path residual a
discriminator rather than a restatement of the ranking.

### `--engine report`: the volunteer path

The map UI computes the location report in the browser and offers it as a
download. **That file is the whole input** — no `sota-rf` checkout, no Node,
no FCC cache — so this is the path for anyone running a survey on a machine
that does not have the model.

1. Open <https://km6am.com/summitRFI/map/>, scroll the left panel to
   **Report for a location**.
2. **Pick on map** (or type the coordinates), then fill in **Antenna above
   ground (m)**. Leave gain, span and floor alone.
3. **Build report**, check the coordinates it echoes, then **Download
   JSON** — a file named like `qth-rfi-2026-11-01.json`.

`sota-rf`'s `docs/volunteer/location-report-howto.html` is the same six
steps with screenshots, written to go in a survey kit.

```bash
python tools/qth_predict.py --engine report \
    --report ~/Downloads/qth-rfi-2026-11-01.json
```

- **The header comes from the file, not the command line.** The point, the
  antenna height, the reference gain, the span and the channel floor are
  all read out of the report's own `query` block. Retyping them is how you
  get a prediction whose header describes a different station than its rows
  were traced at.
- `--lat` / `--lon` / `--ant-agl` are accepted here only as a *check*. A
  disagreement is refused, not resolved — either value could be the right
  one, and guessing silently mislabels the prediction.
- **The download holds your exact coordinates** and the distance and
  bearing to every transmitter, which is why the how-to tells volunteers to
  keep it to themselves. The ingested `/2` file inherits that; the submit
  path's allowlist is the only safe exit, and
  `tests/test_qth_report_ingest.py` runs a real UI-shaped report all the
  way to a feedback body to check that none of it leaves.
- **The deployed page is current with the model; its data is not.**
  Measured 2026-11-01: `engine.js` is byte-identical to `sota-rf` branch
  `qth-rfi-report`, `qth.js` differs only by its cache-busting stamp, and
  the page has the −130 dBm channel-floor control. But
  `map/data/manifest.json` is the **2026-10-28 CA/OR/WA export**, whose
  names sidecar carries only `owner`, `freqs` and `svc` — **no `ref`, no
  `call`, no `freqs_all`** — and the longest frequency list is exactly 40
  entries, so `load_uls`'s truncation is still in effect. A report built
  from it has every row but no emitter reference, which breaks
  `make_channel_list.py`'s summit-tag join. The tool counts blank refs and
  says so on stderr rather than letting it pass unnoticed.

```bash
python tools/qth_predict.py --engine qth-cli --lat <LAT> --lon <LON> \
    --ant-agl 10 --dem-2s ~/srtm2s --data ~/sota-rf/out/export
```

- **`--ant-agl` is required and never defaulted.** It moves every
  diffraction loss, so a guessed height changes which channels are listed
  at all. `--config CONFIG.YAML` reads `site.antenna_height_m` instead.
- **Without `--dem-2s` the tool refuses** unless you pass
  `--allow-remote-terrain`: the CLI would otherwise fetch terrain tiles for
  a 120 km box around your coordinates from AWS, which tells a third party
  roughly where the receiver is.
- **Without `--data` it reads the live map's tiles**, which are older — no
  callsigns and no full per-site channel lists — and that silently shortens
  the prediction rather than failing.
- **A `/2` file locates the station.** Rows carry `distance_m` and emitter
  coordinates, and two of those trilaterate the receiver. It stays local;
  the submit path's field allowlist is the only safe exit. `bearing_deg`
  and `fspl_db` are deliberately not carried.
- `pipeline` is still the default only because it is the engine that works
  against `sota-rf`'s mainline. `qth_cli.mjs` is on the unmerged
  `qth-rfi-report` branch and is not served (404, measured 2026-11-01), so
  `--engine report` is the one a volunteer can use today.

> The `/2` channel list is **much** longer: 69,751 channels in these five
> bands at one Oakland point against 188 in the current targeted list
> (**measured**, `sota-rf`, 2026-10-31). The 120 km cap, not the −130 dBm
> floor, is what bounds it — only 237 of 93,324 channels fell below the
> floor.
>
> Airtime is **unchanged**: at the 4.8 MHz usable window both lists already
> need the full-span cover, 14 tune steps across the five bands
> (**measured**, with uniformly spread synthetic lists of each size). That
> assumption is the weak link — `cover_channels()` is a greedy interval
> cover, so a *clustered* short list would need fewer steps, and a
> proxy-selected 188-channel list may well be clustered. Measure it on the
> real list before relying on it.
>
> What the longer list does cost is interval-file size and, more
> importantly, **false alarms**, which `detector.false_alarm_rate()`
> charges per channel: 370× the channels is 370× the spurious actives, and
> they all land in the *busy with no FCC record* quadrant and swamp it.
> Select with `make_channel_list.py --top-per-band` (default 60) before
> surveying rather than feeding the whole list in.

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

### What a `/2` prediction changes in the validator

| | with `/1` | with `/2` |
|---|---|---|
| audibility floor | `ERP/d²`, frequency-blind | **dBm**, from `p_dbm_ref` |
| predicted-but-silent | two-way: above or below the floor | **three-way**: anomaly / undetermined / expected |
| path model | no test | **FM residual by path class** |

The three-way split exists because the two-way one puts every channel
*near* the floor into the anomaly list, where the model's own error band
says it does not belong. A channel is an **anomaly** only when it sits
above the floor by more than its own `error_db`; within that band it is
**undetermined**, which is a statement about the measurement and not about
the transmitter. A row with no level, or no error band, is undetermined
rather than an anomaly — calling the margin significant without the band
would manufacture a finding.

The dBm floor is *less* frequency-blind than the proxy — at equal field a
matched antenna's capture area grows as λ², about 9.5 dB between 150 and
450 MHz, which `ERP/d²` cannot see. It is **not** frequency-correct:
`p_dbm_ref` is computed at a reference gain, not this antenna's, which also
varies with frequency. Transferring an FM-derived floor to 450 MHz is still
an extrapolation, just a smaller one.

**The FM residual by path class is the only new test that discriminates.**
For a control carrier that is on continuously, measured excess-dB median
minus `p_dbm_ref` is the receive chain's unknown constant plus whatever the
model got wrong. The constant is the same for every channel, so its
absolute value says nothing — but it cannot hide a *difference* between
`los` and `diffracted` carriers. If the diffraction model is right, both
classes share the offset; a systematic gap is path-model error, and a
positive gap means diffraction loss was overestimated. The gap is only
readable against the within-class scatter, so a gap smaller than the IQR it
sits inside is reported as no evidence either way.

It often cannot run: broadcast-FM sites are high and mostly line of sight,
so the diffracted class is frequently empty. The tool says `CANNOT RUN`
with the class counts rather than printing a number.

### Intermod is the error that flatters the comparison

Products land at frequencies set by the **strong** emitters, so they
correlate with exactly the channels the prediction calls strong, and they
read as carriers. An overloaded front end therefore *raises* measured
occupancy on predicted-strong channels — the direction that makes the model
look right. Both the figure and the report say so whenever no check was
supplied:

```bash
python -m occupancy_survey --config config.yaml --intermod-check --intermod-json out/intermod.json
```

```bash
python tools/validate_occupancy.py --prediction out/qth_prediction/prediction.json --intermod-report out/intermod.json data/occ_*.json.gz
```

Bands present in the datasets but absent from the report are named as
unchecked, and a band with suspect or too-steep channels is explicitly
**not** called clean.

## Sending a survey back

```bash
python tools/submit_feedback.py --prediction out/qth_prediction/prediction.json --intermod-report out/intermod.json data/occ_*.json.gz
```

**Dry run by default.** The body is written to `--out` and nothing leaves
the machine without `--send`. Reading the file first is the point.

**An allowlist, not a denylist.** A `/2` prediction row carries
`distance_m` and each emitter's coordinates, and two such rows trilaterate
the receiver as surely as its latitude does — so a denylist would be one
new field away from publishing someone's address. Only these are copied,
by name:

| sent per predicted channel | sent per measured channel |
|---|---|
| `freq_mhz`, `ref`, `db`, `service` | `freq_mhz`, `duty` |
| `p_dbm_ref`, `error_db`, `path`, `rank` | `decided_n`, `excess_db_p50`, `excess_db_p90` |

The body is then scanned a second time for the forbidden names at any
depth, and a third time by `occupancy_survey.disclosure` — belt and braces,
because an allowlist is only as good as the code applying it. This tool's
forbidden set is a strict superset of the server's `GEOMETRY_KEYS`: it also
refuses `rf_lat` / `rf_lon`, which is what a `/2` prediction actually calls
an emitter's coordinates, and `operator_note`.

**The body's shape is the server's**, taken from `rf_feedback.clean_survey()`
rather than from prose: `location.grid`, `prediction.{engine, data,
ant_agl_m, …, channels[]}`, `measured.{acquisition, channels[]}`. `engine`,
`data` and `ant_agl_m` are *required*, and all three exist only in a `/2`
file — so a `/1` prediction cannot be submitted, and the tool says so
instead of sending a body that would 400.

When a local `sota-rf` checkout is present, the test suite runs a built body
through that very validator. It is skipped when the checkout is absent, so a
green run on its own does **not** prove the shape is right.

`rank` is **1..N over the rows actually sent**, by field proxy. It is
deliberately not the report's own rank, which is computed over the whole
queried span before the band filter and so is neither 1..N nor comparable
between submissions with different band sets.

**Nulls that matter.** `duty` is `null`, never 0, when nothing was
decided — an undecided channel is unmeasured, and a zero duty is a
measurement. The excess percentiles are `null` when they land in the
histogram's underflow or overflow, because those returns are *bounds*, not
values, and the server does arithmetic on them.

**Location:** `site.grid_square` at 4 or 6 characters, else withheld. An
8-character locator is withheld rather than truncated — someone who wrote
8 may not have meant to share even the first 6. Never coordinates.

**It refuses** to submit when a checked band had suspect or too-steep
channels (pass `--allow-suspect-intermod` to override), when a
callsign-shaped `site_id` would go out (a callsign resolves to a street
address in the public FCC database), when AGC was enabled, when the
threshold, gain, dwell or antenna is missing, or when either list exceeds
100,000 rows.

**The token comes from `SUMMITRFI_TOKEN` and nowhere else** — not an
argument, which lands in shell history; not a query string, which the
server logs.

> **Measured 2026-11-01.** The server caps a body at 2 MiB on the wire and
> 16 MiB after gunzip, with 100,000 rows per list.
>
> | predicted | measured | uncompressed | gzipped |
> |---|---|---|---|
> | 69,751 | 10,000 | 11.10 MB | 1.17 MB |
> | 100,000 | 10,000 | 15.46 MB | 1.62 MB |
>
> So at full row counts the **uncompressed** limit is the tighter one —
> 15.46 MB is 92% of 16 MiB, while the gzipped body uses only 1.62 of
> 2 MiB. Oakland fits comfortably either way.
>
> These rows use random references and floats, which compress *worse* than
> real data (real licences cluster by holder and sit on a channel grid), so
> they are upper bounds — the safe direction.

### What this cannot hide

The set of channels you heard. The audible public-safety channel set at one
point is close to unique, so it locates you more sharply than a
4-character grid square does. Submitting occupancy at all discloses roughly
where you are, and no version of this tool could change that. It is stated
rather than engineered away.

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
| RSPdx-R2 | 14-bit | ~75–85 dB | same controls as the RSPdx; `device_type: rspdxr2` |
| RSP1A / RSP1B | 14-bit | ~70–80 dB | FM/DAB notches, one antenna input, no HDR; `rsp1a` / `rsp1b` |
| RSPduo | 14-bit | ~75–85 dB | **not yet driven** — two tuners would allow a control band and a target band at once |
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
| `gnuradio` | radioconda + gr-sdrplay3 | RSPdx, RSPdx-R2, RSP1A, RSP1B, and other high-performance SDRs; actuates the notch filters |
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
| **analysis host** | + pandas, matplotlib | merges, validates, plots |
| **ingesting a prediction** (`--engine report`) | nothing extra — a browser, once, elsewhere | turns a downloaded location report into `/2` |
| **generating a fresh prediction** (`--engine pipeline` / `qth-cli`) | + a `sota-rf` checkout and its FCC cache | that one step, and nothing else |

**No sibling checkout is needed to clone and use this repository.** What it
reads from its two upstream projects — the duty-cycle reference table and
sota-rf's data contracts — is vendored in `occupancy_survey/reference/`,
with the source branch, commit, blob and sha256 it was taken from. Fixtures
in `tests/fixtures/` stand in for a real sota-rf run, so merging,
validating and plotting all work standalone.

Only *generating a new* prediction locally still shells out to `sota-rf`,
which is an optional import step: point it at a checkout with
`SOTA_RF_DIR=/path/to/sota-rf`. `--engine report` needs no checkout at all,
which is the point of it. Deliberately not vendored — it is ~756 MB
of third-party data with an acquisition date on it, and copying it would
freeze a dataset that should be refreshed and duplicate a maintained tool.

`occupancy-survey --check` prints which pinned revision the vendored table
came from and, if the source repository happens to be present, whether it
has drifted since. When the source is absent the answer is "not
re-verified", never "fine".

Starting a Claude Code session on the survey machine? `CLAUDE.md` loads
automatically and carries the settled design decisions, and
[docs/survey-host-bringup.md](docs/survey-host-bringup.md) has a brief to
paste in, what "working" looks like, and the problems to expect first.

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
python -m pytest tests/ -q
```

```bash
python -m unittest discover
```

No hardware needed. Synthetic spectra use exponential bin power, because the
max-bin statistic depends on that tail.

CI runs the suite on **3.10 through 3.13** — 3.10 because that is the floor
`pyproject.toml` declares, so the claim is tested rather than asserted.
GNU Radio and `gr-sdrplay3` are deliberately *not* installed there: they come
from radioconda and are not pip-installable, so if a change makes the test
suite import them, CI fails. That is the point of running it without them.

CI also imports the installed package from **outside** the source tree. In
the repo root the source directory shadows the installed copy, so a wheel
that dropped `occupancy_survey/reference/*.json` would pass every test and
fail only for someone who had `pip install`ed it.

## Provenance and licence

GPL v3. [`backends/gr_sdr_sources.py`](occupancy_survey/backends/gr_sdr_sources.py)
is vendored from [morse-code-recorder][mcr] with its origin and the cost of
the copy named in its header.

[dc]: https://github.com/
[mcr]: https://github.com/km6am/morse-code-recorder
