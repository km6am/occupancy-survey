# Bringing up a survey host

A brief for starting a Claude Code session **on the machine that will run
the survey**. The framework was developed elsewhere; this is the part that
has never been done.

Clone the repo on that machine, start a session in it, and paste the brief
in §2. `CLAUDE.md` loads automatically and carries the settled decisions,
so the session should not need to re-derive any of them.

---

## 1. What is and is not already done

**Done and verified:** the package installs from a clean clone into a fresh
venv, 204 tests pass with no hardware, the synthetic backend produces real
datasets, and the analysis tools work end to end.

**Never done:** *anything involving a radio.* The `rtl_sdr` subprocess pipe
and the GNU Radio path have been written and reviewed but never run against
hardware. Treat every claim about them as untested.

**Prefer a direct GNU Radio install over radioconda.** Distribution
package plus a locally built `gr-sdrplay3` has been the more reliable
route; radioconda bundles everything but brought its own problems on
macOS 15 (see [rspdx-on-macos.md](rspdx-on-macos.md)). On Linux the
distribution package is the obvious first choice.

Specifically unknown on a new host:

- whether GNU Radio and `gr-sdrplay3` import in that environment
- whether the SDRplay API is on the loader path for it
- whether `rtl_sdr`'s stdout pipe behaves as assumed (buffering differs
  across platforms)
- what gain setting is right at that location
- whether the front end is linear at that gain, with that antenna

---

## 2. Brief to paste into the session

> I want to bring up an unattended channel-occupancy survey on this
> machine. The repo is cloned here and `CLAUDE.md` has the settled design
> decisions — please read it first and don't relitigate them.
>
> Nothing in this repo has ever touched a radio. The GNU Radio and
> `rtl_sdr` paths are written but untested against hardware, so expect to
> find problems there and treat those two backends as unproven.
>
> Work through this in order, and stop at the first thing that fails rather
> than working around it:
>
> 1. Install per `INSTALL.md` for whichever backend this machine has, and
>    confirm `python -m unittest discover` passes.
> 2. `occupancy-survey --config config.demo.yaml --duration 60` — no
>    hardware, confirms the install and shows what the display looks like.
> 3. Copy `config.example.yaml` to `config.yaml` and fill in the `site`
>    block. Ask me for the antenna description and grid square; don't guess
>    them, and don't put anything finer than a 4-character grid square in.
> 4. `occupancy-survey --config config.yaml --check`, then
>    `--check --device`. **Propose the `--device` command and let me run
>    it** — don't open the radio yourself.
> 5. If the clip fraction is above 1e-4, the front end is overloaded. Tell
>    me; the fix order is bandpass filter, then notch filters, then less
>    gain, because the first two cost no sensitivity.
> 6. `--plan`, and tell me what this configuration *cannot* see.
> 7. Propose a 15-minute test run. The thing to check is the live display:
>    are channels being detected, and does `bcast_fm` say `[pass]`?
>
> Then stop and report. Don't start a long run without me.
>
> Throughout: say which numbers are measured and which are assumed, and
> tell me which step you think is most likely to be wrong.

---

## 3. What "working" looks like

After step 7 the display should show something like:

```
occupancy survey   <site>   elapsed 00:14:00   0 file(s) written
  band          visits   chans  active   busiest
  vhf_lmr_low        6     417       4     151.4750 MHz  36.1%
  nwr                3       7       1     162.4000 MHz  98.0%   [reference]
  bcast_fm           8     100      11     101.3000 MHz 100.0%   [pass]
  16 channel(s) with a carrier across 3 band(s) visited
```

The two things that matter:

- **`bcast_fm` says `[pass]`.** A strong local FM station is a continuous
  carrier; if it does not read ~100%, the threshold or the receive path is
  wrong and every other number in the run inherits the fault.
- **Some land-mobile channels are active.** If none are after 15 minutes
  in a populated area, suspect the antenna, the gain, or an external
  filter that is attenuating the target band.

`nwr` reading below 100% is **not** a fault. Whether that carrier keys on
and off is an open question this survey is meant to settle; see
`docs/nwr-measurement.md`.

---

## 4. Most likely problems, in the order they will appear

| symptom | cause |
|---|---|
| `No module named 'gnuradio'` | package installed in a different environment from GNU Radio. Install it *into* the conda env |
| `libsdrplay_api` not loaded | API installed but not on that environment's loader path |
| `rtl_test` finds no device | DVB-T kernel driver claimed the dongle (Linux) — blacklist it |
| clip fraction high | front end overloaded. Filter first, gain last |
| everything reads ~0% | check the FM control before anything else |
| everything reads ~100% | threshold too low, or the front end is in compression |
| `bcast_fm` fails but FM is audible on a radio | threshold, gain, or an external filter rejecting the FM band |

`--check` diagnoses most of these and gives a remedy for each.

---

## 5. After bring-up

The measurement itself is in `docs/measurement-plan.md`: a short test, then
an extended run of several days, then one command for the results. The
headline figure uses `--hours 08-18`, because land mobile is diurnal and a
24-hour mean understates what a daytime operation meets.

Data volume is 1.4 MB/day, so a multi-day run needs no disk management.

One thing worth deciding before a long run: whether to use a targeted
channel list. It needs a prediction generated on a machine that has
`sota-rf`, and it buys exact frequencies and much smaller files, but it
**cannot find a busy channel with no FCC record**. Keep a grid band
alongside it if that matters.
