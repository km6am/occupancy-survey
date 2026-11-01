# Moving this work to another machine

Written 2026-11-01, at the point where the RSPdx front end was the open
problem. Read this before running anything on new hardware.

## What a `git push` carries, and what it does not

| | travels | note |
|---|---|---|
| source, tests, `CLAUDE.md` | yes | `CLAUDE.md` holds both radio safety rules |
| `docs/`, this file | yes | |
| `.claude/memory-seed/` | yes | a seed; see its README |
| `config.yaml` | **no** | gitignored: it carries the site's grid square |
| `data/` | **no** | gitignored; only `data/demo/` exists anyway |
| live Claude memory | **no** | keyed to a working-directory path |
| radioconda, `gr-sdrplay3` | **no** | reinstall |
| `~/sota-rf` FCC cache (756 MB) | **no** | re-download via sota-rf |
| the RSPdx itself, its antennas and feedline | **no** | and this is the point below |

**The hardware does not travel, so neither do the measurements.** Everything
in "Where the measurement stands" was measured on one receiver, one feedline
and one antenna at one site. On different hardware, or the same hardware at a
different site, every number below is a *prior*, not a result. The antenna
port finding in particular is a property of how that box was cabled.

## Setting up the new machine

1. **Clone the three repositories.** Only the first is written to.

       git clone https://github.com/km6am/occupancy-survey.git ~/Code/occupancy-survey
       git clone https://github.com/km6am/sota-rf.git ~/sota-rf
       git clone https://github.com/km6am/hp8935_sinad_serial.git ~/Code/hp8935_sinad_serial

   `~/sota-rf` and `~/Code/hp8935_sinad_serial` are **read-only** to this
   work: propose changes to them, do not make them. That is a hard
   constraint in `CLAUDE.md`, not a preference.

2. **Install radioconda** to `~/radioconda` and use `conda`, not `pip`, for
   anything in it. `gnuradio` and `gr-sdrplay3` are not pip-installable.
   Install SDRplay's own API driver separately — `gr-sdrplay3` links against
   it and will import but fail to open a device without it.

3. **The volunteer path needs none of that**: `numpy`, `PyYAML` and an
   `rtl_sdr` binary. See `config.volunteer.yaml`.

4. **Bring `config.yaml` across by hand** (AirDrop, `scp`, USB). It is
   gitignored because it carries a grid square. Starting again from
   `config.example.yaml` works but loses two measured settings — see below.

5. **Check before touching the radio**:

       occupancy-survey --config config.yaml --check

   That one needs no hardware. The `--device` form opens the SDR and is a
   separate, explicit request every time it is run.

6. **Re-download the FCC cache** if the prediction tools are needed; run
   sota-rf once with downloads enabled. `tools/qth_predict.py` refuses to
   fetch anything itself.

## Two settings in `config.yaml` that were measured, not chosen

If `config.yaml` is recreated from the example, these are lost. Both are
specific to the receiver that stays behind.

- **`antenna_port: "Antenna A"`.** Measured 2026-11-01 with
  `tools/probe_antennas.py`: noise-floor spread across a 2.9× frequency span
  was 9.9 dB on A, 0.8 dB on B, 0.1 dB on C, reproducible within 0.6 dB. A
  connected antenna's noise floor falls steeply from VHF to UHF because the
  noise is external; an open port shows the receiver's own flat floor. B and
  C were open connectors. `config.example.yaml` still ships `"Antenna B"`
  deliberately, so that a new operator has to measure rather than inherit.
  Whether port C even covers the frequencies probed is **not** established —
  an open port's floor is generated downstream of the antenna switch, so
  that measurement cannot answer it.

- **`if_gain_reduction_db: 20` / `rf_gain_reduction_db: 20`.** A *test*
  setting, not a validated one. The original was IF 40 / RF 0. Total
  reduction is 40 dB either way; the change moved 20 dB of it ahead of the
  LNA and mixer, because the IF attenuator sits downstream of them and so
  cannot protect a compressing front end. Whether it helped is **unmeasured**
  (see below). SDRplay expresses gain as a *reduction*: larger means less
  gain. Measured ranges on this unit: IF `[-59..-20]`, RF `[-84..+0]`.

## Where the measurement stands

Confidence is flagged per claim. Nothing here is a published result.

- **No working overload sensor exists.** *Measured, four candidates.* Clip
  fraction reads zero always (IF attenuation downstream keeps the ADC
  happy); the fd-2 stderr watcher in `overload_watch.py` does not capture
  SDRplay's messages at all; gain readback shows no movement when the API
  says it "corrected". The only thing that works is a human reading the
  console. `overload_reports()` returning zero **must not** be read as a
  healthy front end, and the preflight check says so. The honest fix is a
  `gr-sdrplay3` patch exposing the API's own overload callback.

- **The front end is nonlinear at IF 20 / RF 20 on Antenna A.** *Measured
  once, 2026-11-01, thin.* `--intermod-check` on `vhf_lmr_low`: three of
  eight assessable channels (153.4625, 151.3750, 155.1250 MHz) at implied
  order 2.5–3.2, which is what a 3rd-order product predicts, at two
  independent tune steps. Two more channels moved further than any product
  order explains, which is the signature of traffic starting or stopping
  between the paired measurements — that band is intermittent land mobile,
  so the confound is maximally live there. **Eight channel-measurements is
  thin and one run cannot establish reproducibility.** A real product
  repeats on the same channel at the same order; intermittent traffic does
  not. That intersection is the next measurement and it has not been made.

- **Whether 20/20 beat 40/0 is unmeasured.** The diagnostic was never run at
  the old split, so "the redistribution helped" has no comparison behind it.
  Running `--intermod-check` at both splits is the clean paired experiment.

- **The notches were already on** during that run (`set_filters()` is called,
  and `vhf_lmr_low` carries `rf_notch=True, dab_notch=True`). The remedy the
  code recommends first — an external **150–174 MHz bandpass** — is **not
  fitted**. It is the only remaining fix that costs no sensitivity. More RF
  reduction costs noise figure directly.

- **`bcast_fm` saturates and this blocks a campaign.** *Measured: clip
  fraction 3.8e-02 on Antenna A.* It is the only **control** band, its notch
  must stay off by design, and it sits on the only connected port. A
  saturated control reads 100% occupied and therefore cannot fail, so it
  validates nothing. Three ways out: per-band gain, an external filter, or
  dropping it as the control. **Per-band gain permanently forecloses
  cross-band level comparison** — that is the one to think hardest about,
  because it is cheap now and expensive later. This is a design decision, not
  a measurement, and it is unresolved.

- **No campaign data exists.** `data/` holds only `data/demo/`. Nothing was
  surveyed before the Antenna A switch, so no dataset is invalidated by it.

## Starting the new session

Open Claude Code in `~/Code/occupancy-survey` — **not** in a
`morse_code_recorder` worktree, which is where the earlier sessions ran and
is why the memory files landed under the wrong project key. Point it at this
file and at `CLAUDE.md`.

The two safety rules it must be operating under from the first message, both
stated in full in `CLAUDE.md`:

- **One explicit request per prompt that triggers the radio.** Approval never
  carries to the next command, not even an identical re-run.
- **"My airplane" / "my controls" / "my radio" is an abort.** Release the
  radio, stop mid-stream, say where things stand, end the turn.
