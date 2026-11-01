# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A long-term channel-occupancy monitor. It steps band to band and logs, per
channel, **what fraction of the time a carrier is present**. The output is
duty cycles per radio service.

It exists to fill one gap: `hp8935_sinad_serial/docs/reference/duty_cycles.md`
assigns a duty cycle to each radio service to predict SOTA summit RFI, and
every figure in it comes from a standard or from architecture. Its
`measured` column is empty. This produces the first entries.

## Commands

```bash
python -m unittest discover                      # 204 tests, no hardware
occupancy-survey --config config.yaml --check    # is this host ready?
occupancy-survey --config config.yaml --check --device   # opens the SDR
occupancy-survey --config config.yaml --plan     # what it can and cannot see
occupancy-survey --config config.demo.yaml --duration 120   # no hardware
python tools/merge_datasets.py data/occ_*.json.gz --hours 08-18
```

## Hard constraints

These are not preferences. Violating one invalidates the data.

- **Never demodulate, never record audio or IQ.** The backend interface
  (`backends/base.py`) yields power spectra only. This is what makes the
  tool safe to ask a stranger to run, and it is structural, not policy.
- **AGC is refused**, in the config schema, the source factory and the
  dataset writer. An AGC moves the noise floor that every threshold is
  relative to. This is deliberate; do not "fix" it by adding the option.
- **Fixed gain for a whole campaign.** Gain is recorded per dataset and a
  campaign whose gain changed midway cannot be pooled across the change.
- **`~/sota-rf` and `~/Code/hp8935_sinad_serial` are READ-ONLY.** Propose
  changes; do not make them.
- **Do not open an SDR or serial port unprompted.** Propose the command,
  and make a SEPARATE explicit request for *each* prompt that will trigger
  the radio. Approval for one radio command never carries to the next, not
  even an identical re-run after a config change. A session may be used to
  test the radio; a session or loop that then exercises it repeatedly off
  one approval may not.
- **"My airplane" / "my controls" / "my radio" is an abort.** On any of
  those phrases: release the radio, stop code tasks mid-stream, reach the
  closest clean state as fast as possible, say in a line or two where
  things stand, and end the turn. It is the aviation positive-exchange-of-
  control convention and it is used when the work is going the wrong way,
  so the value is entirely in the speed of the handover. Do not finish the
  current edit or test run because it is nearly done, and do not spend
  calls tidying -- leaving work uncommitted and saying so is correct.
- **Location is coarse and opt-in.** Maidenhead, 4 or 6 characters, and
  anything finer is *refused*, not truncated.
- **Datasets must carry their acquisition conditions.** The writer refuses
  a file missing threshold, gain, dwell or antenna. A duty cycle without
  those is not a measurement.

## Settled decisions — do not relitigate

Each of these was reached the hard way. The reasoning is in the commit
messages and the linked documents.

**`threshold_db` is not an SNR.** The baseline is a low percentile of the
*max-bin* statistic, which already sits above mean noise power, so a 10 dB
threshold needs ~12.8 dB SNR in a 12-bin channel. `required_snr_db()`
computes it; `--plan` prints it.

**An empty channel does not read 0%.** Noise clears the threshold at a rate
set almost entirely by bins per channel — negligible at 22, but **5.6% at
one bin**. The fix is a finer FFT bin, never a higher threshold, which
would bury weak real signals too. See `detector.false_alarm_rate()`.

**The baseline is pooled over channels × time, and measured from a uniform
grid**, not from the channels being recorded. A spectral-only baseline is
pulled up by adjacent leakage; a temporal-only one reads 0% on a
permanently-keyed channel. A *targeted* channel list would poison its own
baseline, since every channel in it was chosen for likely traffic.

**A keying period needs a continuous dwell.** Band-stepping cannot measure
one — a run longer than the dwell is cut by the retune and discarded — and
the tool correctly reports `cycles 0, period null` rather than the dwell
length. See `docs/nwr-measurement.md` and `config.nwr.yaml`.

**NOAA Weather Radio is NOT a duty-cycle control.** That was retracted on
2026-10-28: "the service runs 24/7" is a claim about *availability*, not
about carrier *keying*, and an operator's listening experience contradicted
it. It *is* a **presence** reference — over a long enough observation it
must appear, because the NWS publishes the transmitter list. A regression
test fails if it is restored to `CONTROL`; restoring it needs a
*measurement*, not a re-reading of the same sources.

**Broadcast FM is the only control**, because its claim is about the
modulation scheme rather than a schedule: an FM transmitter radiates an
unmodulated carrier through programme silence.

**Measure in the daytime.** Land mobile is dispatch and business traffic,
strongly diurnal. A figure pooled over the whole clock is diluted by the
quiet hours and *understates* what a daylight operation meets. Run
overnight too — the diurnal shape is a result — but compute the headline
with `--hours 08-18`.

**Duty cycle transfers between locations; field strength does not.** An 18%
dispatch channel heard at a QTH is 18% for an activator beside that
transmitter, because the path changes how *loud*, not how *often*. This is
why a QTH survey is useful at all, and why there is no field-strength field
in the schema. **A QTH is not a summit.**

**Intermod is the dominant error and it flatters.** Third-order products
land on empty channels *exactly when the strong carriers key up*, so they
correlate with predicted-strong emitters and make the validation plot look
*better* while being wrong. Unlike thermal false alarms it does not average
out. `--intermod-check` discriminates by stepping gain. See
`docs/front-end.md`.

**Summit emitters are tagged, not filtered.** Filtering to summit-co-sited
emitters shrinks the sample for no modelling benefit — `duty_cycles.md` is
keyed by *service code*, not callsign.

## Where estimates have been wrong

A caution, because the pattern repeated: **magnitude estimates in this
project have been unreliable; compute them.** Each of these was asserted
and then measured:

| claim | measured |
|---|---|
| targeting cuts cycle time ~10× | **30%** (14 → 10 tune steps) |
| disk will need managing | **1.4 MB/day** — never a constraint |
| overnight is the right target | daytime; overnight *dilutes* the figure |
| NWR is a continuous carrier | not established; retracted |

Derivations have held up; estimates of "how much better" have not.

## If the toolchain breaks on macOS

`docs/rspdx-on-macos.md` is a confirmed repair runbook, with
`tools/fix_macos_rpaths.py`. The short version: macOS 15's dyld refuses to
load any Mach-O with duplicate `LC_RPATH` entries, several conda-forge
builds have them, and **every symptom points somewhere other than the
cause** — numpy reports a source-directory error it has no business
reporting, and dyld reports a search failure for a file that is present.
Get the chained traceback before changing anything.

## Architecture

```
backend (power spectra only)
  -> ChannelLevelExtractor   max-bin per channel
  -> BandBaseline            low percentile, pooled, from a uniform grid
  -> decide()                excess >= threshold
  -> BandAccumulator         re-thresholdable histograms + runs/gaps/keying
  -> IntervalDataset         one file per site per interval
```

Key invariants in `survey.py`: one threshold policy object builds the
histograms *and* supplies the recorded metadata; baselines are keyed by
(band, tune step) and reset on retune or filter change; the design's
`usable_bandwidth_hz` must match the backend's real bandwidth or tune steps
leave gaps that look like quiet channels.

## Testing

`python -m unittest discover` — 204 tests, no hardware. Synthetic spectra
use exponential bin power, because the max-bin statistic depends on that
tail. Tests assert *measured* figures the docs quote, so a doc cannot drift
from the code.
