# Data format

One file per site per logging interval:
`occ_<site_id>_<YYYYMMDDTHHMMSSZ>.json.gz`

Designed for merging from the start, because more receivers may appear and a
format that needs a migration to accept a second site is a format that only
ever gets one site.

Schema identifier: `occupancy-survey/1`. A file with any other `schema` value
is **refused**, not guessed at.

---

## 1. What makes it mergeable

Per-channel histograms are in units of **dB above that sweep's own band
baseline** — not absolute level. Each receiver has had its own noise floor
subtracted before anything is written, so two sites with different gain
settings, different antennas and different absolute noise floors still
produce histograms on a common axis.

Absolute levels are recorded as well, under `baseline_dbfs`, but they are
**uncalibrated dBFS, not dBm**. Nothing should compare them across
receivers. Occupancy does not need absolute calibration because it is
threshold-relative; absolute field strength would, and this tool does not
measure it (see §6).

### The one thing that is not common across sites

`bins_per_channel`. It sets how far the baseline sits above the mean noise
power — see `detector.baseline_elevation_db()` — so a 12-bin channel and a
180-bin channel have excess-dB axes whose zero points differ by about 3 dB.

`merge_channels()` pools across it anyway, because refusing would make a
multi-receiver dataset unusable over a small difference, but it sets
`comparable = False` and the flag travels with the result.

Mismatched **histogram specs** (different bin width or range) *are* refused,
because re-binning would need the raw samples and those are not stored.

---

## 2. The histogram, and why it is not a boolean

The detection threshold is the result, so the dataset stores enough to
**change** it afterwards: per channel per interval, a histogram of
`level − baseline` in 1 dB bins from −10 to +60 dB, plus underflow and
overflow counts.

Occupancy at any threshold is the histogram mass at or above it. So a
dataset taken at a 10 dB threshold can be re-read at 14 dB without
re-running the survey, and two sites that chose different thresholds can
still be compared at a third. The `occupancy` field is a **label on the
data, not a property of it** — it is the value at the file's own
`detection.threshold_db`.

Cost: about 70 numbers per channel per interval instead of two, a few
hundred kB per interval before compression for the default schedule.

**What the histogram cannot recover is time structure.** Two channels with
identical histograms can be one long transmission or many short ones, and
`duty_cycles.md` §1 turns on exactly that distinction — the same duty cycle
with a different episode length is a different problem for an operator. So
`runs` is accumulated alongside: counts and lengths of consecutive
present-sweeps. **Those are tied to the file's default threshold and cannot
be re-derived at another one.** `runs.at_threshold_db` records which.

---

## 3. Every file carries, without exception

The writer **refuses** to produce a file missing any of these:

| field | why it is mandatory |
|---|---|
| `detection.threshold_db` | the threshold is the result |
| `detection.estimator`, `baseline_percentile` | a threshold means nothing without the baseline that it is relative to |
| `receiver.gain`, `receiver.agc_enabled` | AGC on ⇒ the file is refused outright |
| `sampling.dwell_s`, `integration_s` | sets the blind spot |
| `histogram.bin_db` | needed to re-threshold |
| `site.site_id`, `site.antenna` | a quiet channel and a deaf receive path produce the same number |

AGC enabled is a **refusal, not a warning**: the noise floor moved during
acquisition, so every noise-floor-relative threshold in the file is
meaningless and nothing downstream can detect that after the fact.

Each band additionally records **`noise_floor_occupancy`** — the occupancy
an empty channel reads at that file's threshold and channel width — plus a
`noise_floor_warning` sentence when it is large enough to matter. It is
governed by bins per channel and reaches 5.6% at one bin per channel, which
would swamp the duty cycles being measured. See
[sampling-design.md](sampling-design.md) §4b.

`sampling` also carries the derived bounds from
[sampling-design.md](sampling-design.md) — cycle time, visits per interval,
minimum detectable burst — and any `warnings` the schedule raised, such as a
run that could not meet its own revisit target or one taken without jitter.
Those travel in the data because a log file will not be there in six months.

---

## 4. Absence versus non-observation

Kept distinct all the way to disk. A channel outside the current tuning, or
one sampled before the baseline window filled, is **not observed** — not
quiet. `occupancy` is always a fraction of `n_decided`, and a channel with
`n_decided = 0` produces no record at all rather than a row of zeros.

`occupancy: null` means not measured. `occupancy: 0.0` means measured and
quiet. Do not coalesce them.

---

## 5. Two service-level numbers, not one

`service_occupancy()` reports both, because they answer different questions
and are routinely confused:

- **`mean_channel_occupancy`** — averaged over all the band's channels,
  including the quiet ones. Low by construction on a uniform grid where most
  channels have no licensee. This is "how often is a *random* channel here
  busy", which is what a risk model wants.
- **`active_channel_occupancy`** — averaged over channels that were busy at
  all. This is the figure comparable with the **TIA-603 numbers in
  `duty_cycles.md`**, which describe a channel *in use*, not one picked off
  a grid.

Quoting the first against the second would make every measured duty cycle
look an order of magnitude lower than the standard it is replacing.

Control bands are reported separately with their verdicts. Folding a
100%-occupied broadcast-FM carrier into a land-mobile average would be
nonsense.

---

## 6. What this format deliberately cannot express

**There is no field-strength field, and there will not be one.** This tool
measures duty cycles per service at a fixed location. It does **not** yield
summit field strengths: the propagation path and the antenna are different,
and a QTH is not a summit. `sota-rf` computes field strength; this computes
occupancy; the two compose in the risk model and must not be conflated in a
data file, a plot title or a column name.

**There is no audio, no IQ and no demodulated content**, by construction
rather than by policy — the acquisition interface carries power spectra and
nothing else. That is what makes this safe to ask a stranger to run, and it
is stated in the volunteer instructions in those terms.
