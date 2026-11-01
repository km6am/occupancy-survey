# Installation

This package logs **how often a carrier is present** on each channel. It
needs Python 3.10+ and one of three receive backends. Pick the backend
first — it decides everything else.

| backend | hardware | install difficulty | use it when |
|---|---|---|---|
| `synthetic` | none | nothing to install | verifying an install, trying the tools |
| `rtl_sdr` | RTL-SDR dongle (~$30) | one binary + pip | reach matters more than sensitivity |
| `gnuradio` | SDRplay RSPdx, RSPduo, USRP… | conda environment | **preferred** — 14-bit, switchable notch filters |

> **The front end decides the result.** Near a city the spread between
> broadcast FM and land mobile is 70–90 dB, which an 8-bit RTL-SDR does not
> have. It is usable with an external bandpass filter and misleading
> without one. See [docs/front-end.md](docs/front-end.md).

---

## 1. Get the code

```bash
git clone <repository-url> occupancy-survey
cd occupancy-survey
```

## 2. Install the Python package

A virtual environment is strongly recommended, except when using the
`gnuradio` backend — see §4, where the conda environment *is* the
environment.

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install .
```

### For development, or to run the tests

```bash
pip install -e .
python -m unittest discover        # 172 tests, no hardware needed
```

### Optional extras

```bash
pip install '.[plots]'             # matplotlib, for the validation figure
```

Only the *analysis* machine needs that. See §7.

**Check it worked:**

```bash
occupancy-survey --help
python -m occupancy_survey --help  # equivalent, if the script is not on PATH
```

---

## 3. Backend: `rtl_sdr`

Install the RTL-SDR command-line tools. The package shells out to the
`rtl_sdr` binary and reads raw IQ from its stdout — there is no Python
binding to install.

| platform | command |
|---|---|
| Debian / Ubuntu / Raspberry Pi OS | `sudo apt install rtl-sdr` |
| Fedora | `sudo dnf install rtl-sdr` |
| macOS (Homebrew) | `brew install librtlsdr` |
| macOS (MacPorts) | `sudo port install rtl-sdr` |
| Windows | download an `rtl-sdr` release, add its folder to `PATH` |

Verify the dongle is seen:

```bash
rtl_test -t
```

On Linux you may need a udev rule to use the dongle without `sudo`, and to
blacklist the DVB-T kernel driver that otherwise claims it:

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo reboot
```

Then set `backend: "rtl_sdr"` in your config.

> **Do not set `gain: 0`.** For `rtl_sdr` that means *automatic* gain. An
> AGC moves the noise floor that every threshold in this tool is relative
> to, so a survey taken with one is worthless. The config loader refuses an
> `agc_enabled` key outright, and the `rtl_sdr` backend refuses a gain of
> zero.

---

## 4. Backend: `gnuradio` (SDRplay RSPdx and others)

**GNU Radio is not pip-installable.** It comes from a conda environment or
a distribution package, and this package must be installed *into* that same
environment.

### 4a. Install GNU Radio

**Prefer a direct install over radioconda where you have the choice.** In
this project's experience radioconda has been the less reliable of the two
on real machines — see the caveat below — while a distribution package
plus a locally built `gr-sdrplay3` has behaved better.

| platform | command |
|---|---|
| Debian / Ubuntu | `sudo apt install gnuradio gnuradio-dev cmake build-essential` |
| Fedora | `sudo dnf install gnuradio gnuradio-devel cmake` |
| macOS (Homebrew) | `brew install gnuradio cmake` |

**The tradeoff is real and worth stating.** Distribution packages do *not*
ship `gr-sdrplay3`, which SDRplay hardware needs, so you build it:

```bash
git clone https://github.com/fventuri/gr-sdrplay3
cd gr-sdrplay3 && mkdir build && cd build
cmake .. && make -j && sudo make install
```

That needs the SDRplay API installed first (§4b) and matching GNU Radio
headers. It is more steps than radioconda, and it is the route that has
worked better.

**[radioconda](https://github.com/ryanvolz/radioconda/releases)** bundles
`gr-sdrplay3`, SoapySDR and UHD in one installer, which is genuinely
convenient when it works.

> **radioconda caveat, measured not guessed.** On macOS 15 a radioconda
> installation was entirely unusable: 368 of its Mach-O files carry
> duplicate `LC_RPATH` entries, which that OS's dyld refuses to load.
> numpy, scipy, matplotlib, GNU Radio and `gr-sdrplay3` all failed from
> that one cause, with symptoms that pointed everywhere else. It is
> repairable — `tools/fix_macos_rpaths.py`, and
> [docs/rspdx-on-macos.md](docs/rspdx-on-macos.md) is the runbook — but a
> direct install avoids the whole class of problem, because the duplicates
> come from conda packaging rather than from GNU Radio.

### 4b. Install the SDRplay API (SDRplay hardware only)

Download **API v3.x** from
[sdrplay.com/downloads](https://www.sdrplay.com/downloads/) and install it.
Reboot afterwards on Windows. On Linux the installer is a `.run` script and
installs a service that must be running.

### 4c. Install this package into that environment

```bash
# with radioconda's python, not a venv
~/radioconda/bin/pip install .        # adjust the path to your install
```

### 4d. Verify

```bash
~/radioconda/bin/python -c "from gnuradio import gr; print(gr.version())"
~/radioconda/bin/python -c "from gnuradio import sdrplay3; print('sdrplay3 ok')"
```

If the second fails with a message mentioning `libsdrplay_api`, the API
library is installed but not on the loader path for that environment. On
some installs a symlink into `<env>/lib` resolves it. A worked example on
one machine is in
[docs/rspdx-on-macos.md](docs/rspdx-on-macos.md) — that document is a
troubleshooting note for **one host**, not a prerequisite.

Only one application can use an SDRplay device at a time; close SDRuno or
any other SDR software first.

---

## 5. Configure

```bash
cp config.example.yaml config.yaml
```

Edit the `site:` block. Three fields are required and one matters more than
people expect:

```yaml
site:
  site_id: "home-1"                  # how files are kept apart when merged
  receiver_model: "SDRplay RSPdx"
  antenna: "discone, roof mount"     # REQUIRED, in plain words
  front_end_filters: "none"          # what is actually in the coax
  grid_square: "CM87"                # OPTIONAL - delete to share nothing
```

**`antenna` is required because a quiet channel and a deaf receive path
produce the same number, and this field is the only thing that tells them
apart.** "rubber duck on a windowsill" is a useful answer; blank is not.

`grid_square` is **optional and deliberately coarse** — Maidenhead, 4 or 6
characters only. Anything finer is *refused*, not silently truncated.
Delete the line entirely to share no location; the dataset is still valid
and still mergeable.

RTL-SDR users can start from `config.volunteer.yaml`, which has exactly
three lines to edit.

---

## 6. Verify and run

```bash
occupancy-survey --config config.yaml --check            # no hardware touched
occupancy-survey --config config.yaml --check --device   # opens the SDR, one sweep
occupancy-survey --config config.yaml --plan             # what this config can and cannot see
```

`--check` reports Python, imports, config, disk headroom and clock, and
every failure comes with a remedy. `--plan` prints the blind spot, the
detection edge and the catch probabilities — **run it before committing to
a long run**, because it tells you what the configuration cannot see.

A first real run:

```bash
occupancy-survey --config config.yaml --duration 3600
```

One file per hour appears in `data/`. About **57 kB gzipped per hour**
(1.4 MB/day), so disk is not a constraint.

Then read [docs/measurement-plan.md](docs/measurement-plan.md), which sets
out the full sequence: commissioning, receive-path validation, and the
campaign itself.

---

## 7. The analysis machine

The machine that *surveys* and the machine that *analyses* need not be the
same, and deliberately have different dependencies:

| role | needs |
|---|---|
| **survey host** | numpy, PyYAML, a backend |
| **analysis host** | the above, plus matplotlib and pandas |
| **generating a fresh prediction** | plus a [`sota-rf`](https://github.com/km6am/sota-rf) checkout and its FCC cache |

The survey host never needs a 400 MB FCC download: the channel list is a
small JSON generated on the analysis host and copied across.

Neither host needs a sibling checkout to *install or run* this repository.
The duty-cycle reference table and sota-rf's data contracts are vendored in
`occupancy_survey/reference/` with full provenance, and
`tests/fixtures/prediction_sample.json` stands in for a real run, so the
analysis tools are exercisable standalone. Only `tools/qth_predict.py`
needs `sota-rf`, and only to produce a new prediction.

```bash
pip install '.[plots]'
export SOTA_RF_DIR=/path/to/sota-rf     # if not at ~/sota-rf
python tools/qth_predict.py --lat <LAT> --lon <LON> --radius 90000 \
    --out out/prediction.json
```

`sota-rf` is a separate project and is **read, never modified**, by these
tools.

---

## 8. Troubleshooting

| symptom | cause and fix |
|---|---|
| `No module named 'gnuradio'` | this package is installed in a different environment from GNU Radio. Install it into the conda env (§4c) |
| `libsdrplay_api` not loaded | SDRplay API installed but not on that environment's loader path (§4d) |
| `rtl_test`: *no supported devices* | dongle not plugged in, or the DVB-T kernel driver claimed it (§3) |
| `gain_db must be positive` | `gain: 0` means automatic on `rtl_sdr`. Pick a fixed value, e.g. 40 |
| `agc_enabled is not a setting` | remove the key. There is no valid occupancy survey with AGC on |
| `refusing to write a dataset missing…` | a required acquisition field is absent. The message names it |
| config error naming an unknown key | a misspelled setting is refused rather than ignored, because the default would otherwise be recorded as though chosen |
| run produced no files | shorter than one `interval_s`, or no band was observed |
| everything reads ~0% occupied | check the `bcast_fm` control band's verdict before anything else |

`--check` diagnoses most of these directly.

---

## 9. Bringing up a new survey host with Claude Code

`CLAUDE.md` in the repository root loads automatically into a Claude Code
session and carries the settled design decisions, the hard constraints, and
the places where earlier estimates turned out wrong — so a new session does
not re-derive or relitigate them.

[docs/survey-host-bringup.md](docs/survey-host-bringup.md) has a brief to
paste in, a checklist that stops at the first failure rather than working
around it, what a working bring-up looks like, and the problems to expect
in the order they appear.

---

## 10. Uninstall

```bash
pip uninstall occupancy-survey
```

Datasets in `data/` and `out/` are plain files and are not removed.
