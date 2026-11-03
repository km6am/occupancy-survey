# Installation

occupancy-survey logs **how often a carrier is present** on each channel.
It needs Python 3.10+ and one receive backend. Pick the backend first,
because it decides everything else.

| backend | hardware | what you install | use it when |
|---|---|---|---|
| `synthetic` | none | this package only | checking an install, trying the tools |
| `gnuradio` | SDRplay RSPdx, RSPdx-R2, RSP1A, RSP1B | SDRplay API + GNU Radio + gr-sdrplay3 | **preferred**: 14-bit, switchable FM/DAB notches |
| `rtl_sdr` | RTL-SDR dongle | the `rtl_sdr` binary | reach matters more than sensitivity; needs an external filter |

> **The front end decides the result.** Near a city the spread between
> broadcast FM and land mobile is 70–90 dB. An 8-bit RTL-SDR does not
> have that range, so it is usable with an external bandpass filter and
> misleading without one. See [docs/front-end.md](docs/front-end.md).

The SDRplay path has three pieces from three sources, installed in this
order:

1. **SDRplay API** (from sdrplay.com). This is a driver plus a background
   service, and it is the only thing that talks to the radio.
2. **GNU Radio** (from conda or your OS). It is not pip-installable.
3. **gr-sdrplay3** (from its author, F. Venturi). It is the GNU Radio
   block for SDRplay receivers, and it is **not** part of radioconda or
   conda-forge.

Then install this package into the *same* Python environment as GNU Radio.

**Verified combination**, Windows 10, 2026-11-02:

| piece | version |
|---|---|
| SDRplay API | 3.15 |
| radioconda | 2025.03.14 (Python 3.12.9, GNU Radio 3.10.12.0) |
| gr-sdrplay3 | 3.11.0.8, conda package built for that radioconda |

The macOS route was confirmed earlier (see §3b). The Linux and
Miniforge/Miniconda routes are written from upstream instructions and
have **not** been run on hardware here.

---

## 1. Get the code

```bash
git clone https://github.com/km6am/occupancy-survey.git
cd occupancy-survey
```

---

## 2. SDRplay API (all platforms)

Download **API 3.15 or later** from <https://www.sdrplay.com/api>.
gr-sdrplay3 3.11 will not work with an older API.

| platform | install | then check |
|---|---|---|
| Windows | run the installer and keep the default path, `C:\Program Files\SDRplay\API` | `sc query SDRplayAPIService` shows `RUNNING` |
| macOS | run the `.pkg` | `launchctl list \| grep -i sdrplay` lists the service |
| Linux | `chmod +x SDRplay_RSP_API-Linux-3.*.run && sudo ./SDRplay_RSP_API-Linux-3.*.run` | `systemctl status sdrplay` (the service name varies by API version) |

Plug the receiver in **after** the API is installed. Close SDRuno, SDR++
and any other SDR program before a survey: only one program can open an
SDRplay device at a time.

---

## 3. GNU Radio and gr-sdrplay3

### 3a. Windows: radioconda (verified)

1. **Install radioconda.** Download `radioconda-Windows-x86_64.exe` from
   <https://github.com/ryanvolz/radioconda/releases> and run it. Choose
   "Just me" and keep the default path, `%USERPROFILE%\radioconda`.
2. **Open the radioconda Prompt** (Start menu → radioconda). Update conda
   itself, **and nothing else**:

   ```bat
   conda update conda -y
   ```

   Do not run `conda update --all`. It replaces GNU Radio's dependencies
   with versions the gr-sdrplay3 package was not built against, and
   imports then fail with `DLL load failed`.

3. **Install gr-sdrplay3.** Download the Windows `.conda` package from
   <https://github.com/fventuri/gr-sdrplay3/releases>. The package must
   match your radioconda: 3.11.0.8 is built for radioconda 2025.03.14 /
   Python 3.12. From the folder you downloaded it to:

   ```bat
   conda install gnuradio-sdrplay3-3.11.0.8-py312h702a0ab_0.conda
   ```

4. **Make sure the API library is visible to conda.** The package should
   copy `sdrplay_api.dll` into the environment. If it did not, copy it
   yourself:

   ```bat
   copy /B /Y "%ProgramFiles%\SDRplay\API\x64\sdrplay_api.dll" "%CONDA_PREFIX%\Library\bin"
   ```

### 3b. macOS

The route confirmed on macOS 15 / Apple silicon is radioconda plus a
separately installed gr-sdrplay3. That radioconda needed a repair first:
368 of its binaries carry duplicate `LC_RPATH` entries, which macOS 15
refuses to load. Every symptom of that points somewhere else; for
example, numpy reports a source-directory error.
[docs/rspdx-on-macos.md](docs/rspdx-on-macos.md) is the runbook, and
`tools/fix_macos_rpaths.py` does the repair.

The alternative is Homebrew (`brew install gnuradio cmake`) plus
gr-sdrplay3 built from source as in §3c. It avoids the rpath problem
entirely, but has not been run here.

### 3c. Linux (not run here)

Install GNU Radio from the distribution, then build gr-sdrplay3 against
it. The SDRplay API (§2) must be installed first.

```bash
sudo apt install gnuradio gnuradio-dev cmake build-essential git pybind11-dev   # Debian/Ubuntu
# sudo dnf install gnuradio gnuradio-devel cmake gcc-c++ git pybind11-devel     # Fedora

git clone https://github.com/fventuri/gr-sdrplay3.git
cd gr-sdrplay3 && mkdir build && cd build
cmake .. && make -j"$(nproc)"
sudo make install && sudo ldconfig
```

Use the system `python3` for everything after this, not a virtualenv.
That is where GNU Radio lives.

### 3d. Miniforge or Miniconda instead of radioconda (not run here)

radioconda is a curated conda-forge environment. A plain conda gives the
same GNU Radio if it is pinned to the versions gr-sdrplay3's package was
built against. Prefer **Miniforge** (<https://conda-forge.org/download/>),
whose default channel is conda-forge. With **Miniconda**, add
`-c conda-forge --override-channels` to every command, so nothing is
mixed in from Anaconda's own channel.

```bash
conda create -n occ -c conda-forge python=3.12 gnuradio-core=3.10.12 \
    "spdlog=1.15" "fmt=11.0" numpy pyyaml matplotlib
conda activate occ
conda install ./gnuradio-sdrplay3-3.11.0.8-py312h702a0ab_0.conda   # Windows package, §3a step 3
```

On Linux or macOS, build gr-sdrplay3 into this environment instead: run
§3c's `cmake` with `-DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX`.

An analysis-only machine needs **no** GNU Radio. A Miniforge or Miniconda
environment with `python numpy pyyaml matplotlib`, or a plain venv, is
enough (§8).

---

## 4. Install this package into that environment

Always run pip through the environment's own Python, so the package lands
beside GNU Radio:

```bat
:: Windows, radioconda Prompt (or the full path to radioconda's python.exe)
python -m pip install -e .
```

```bash
# macOS/Linux, conda (radioconda or Miniforge, activated)
python -m pip install -e .
# Linux, system GNU Radio
python3 -m pip install --user -e .
```

**No radio, or `rtl_sdr` only?** A virtualenv is fine:

```bash
python3 -m venv .venv && . .venv/bin/activate     # Windows: py -3 -m venv .venv ; .venv\Scripts\Activate.ps1
python -m pip install -e ".[plots]"
```

---

## 5. Verify: four checks, in order, each must pass

```bash
python -c "from gnuradio import gr; print(gr.version())"     # 3.10.12.0
python -c "from gnuradio import sdrplay3; print('sdrplay3 ok')"
python -m unittest discover                                  # ~630 tests, no hardware
python -m occupancy_survey --config config.example.yaml --check
```

The test suite has one known failure on Windows,
`test_an_unwritable_output_dir_fails`. The test makes a folder unwritable
with a Unix permission change, which has no effect on Windows.

`--check` reports Python, imports, config, disk headroom and clock, and
gives a remedy for each failure. It does not touch the radio. Opening the
receiver for one sweep is a separate, deliberate step:

```bash
python -m occupancy_survey --config config.yaml --check --device
```

---

## 6. Configure

```bash
cp config.example.yaml config.yaml          # Windows: copy config.example.yaml config.yaml
```

`config.yaml` is git-ignored, because it holds your site. The fields that
matter:

```yaml
site:
  site_id: "home-1"              # NOT your callsign: that resolves to a street address
  receiver_model: "SDRplay RSPdx"
  antenna: "discone, roof mount" # required: a deaf antenna and a quiet band read the same
  grid_square: "CM87"            # optional; 4 or 6 characters, finer is refused

sdr:
  backend: "gnuradio"
  device_type: "rspdx"           # rspdx | rspdxr2 | rsp1a | rsp1b
  antenna_port: "Antenna A"      # RSPdx/-R2 only; leave out on an RSP1A/1B
  if_gain_reduction_db: 20       # fixed for the whole campaign; there is no AGC
  rf_gain_reduction_db: 20
  band_rf_gain_reduction_db:     # per-band override; FM needed more here
    bcast_fm: 50
```

- **Which port the antenna is on:** find it with
  `python tools/probe_antennas.py --freq-mhz 162.475 --freq-mhz 465`.
  Don't guess: an empty port reads as a quiet band.
- **Values that belong to one receiver:** the FM band's 50 dB RF
  reduction and the `spurs_mhz` list were measured on one RSPdx. They do
  not transfer to another receiver, even another RSPdx. Re-measure on
  yours:
  - `python -m occupancy_survey --config config.yaml --intermod-check`
    for the gain step and intermod;
  - an open-port run for spurs.

  [docs/measurement-plan.md](docs/measurement-plan.md) gives the full
  commissioning sequence.

---

## 7. Run

```bash
python -m occupancy_survey --config config.yaml --plan        # what it can and cannot see; no radio
python -m occupancy_survey --config config.yaml               # runs until Ctrl-C
python -m occupancy_survey --config config.yaml --channels out/channels.json   # a targeted list
```

- **Output:** one gzipped JSON file per hour in `data/`, about 1.4 MB a
  day. **Ctrl-C** finishes and saves the interval in progress.
- **Live display:** a summary redraws in place; it does not scroll.
- **GNU Radio and SDRplay messages:** these are written to
  `data/logs/gnuradio_<UTC>.log`, not to the screen. Each overload report
  there is stamped and tagged with its band and phase (`settling`,
  `dwell` or `startup`).
- **Daytime:** land-mobile traffic is diurnal. Pool the headline figure
  over daylight hours:
  `python tools/merge_datasets.py data/occ_*.json.gz --hours 08-18`.

---

## 8. Analysis tools

```bash
python -m pip install -e ".[plots]"     # matplotlib
python tools/plot_channels.py "data/occ_*.json.gz" --channels out/channels.json --out out/plots
python tools/summit_report.py "data/occ_*.json.gz" --channels out/channels.json --hours 08-18
```

A channel list comes from a location report downloaded from the sota-rf
map ("Download JSON"):

```bash
python tools/qth_predict.py --engine report --report qth-rfi-YYYY-MM-DD.json --out out/prediction.json
python tools/make_channel_list.py --prediction out/prediction.json --out out/channels.json
```

The report holds your exact coordinates; the channel list it produces
does not. Neither host needs the FCC database or a sota-rf checkout for
this route.

---

## 9. Troubleshooting

| symptom | cause and fix |
|---|---|
| `No module named 'gnuradio'` | this package is in a different environment from GNU Radio. Reinstall with that environment's `python -m pip` (§4) |
| `No module named 'gnuradio.sdrplay3'` | gr-sdrplay3 is not installed in this environment (§3) |
| `DLL load failed while importing sdrplay3_python` | `sdrplay_api.dll` is not in `%CONDA_PREFIX%\Library\bin` (§3a step 4), or `conda update --all` was run. Reinstall radioconda and do not update all |
| device not found / `sdrplay_api_Open` fails | the API service is stopped, or another program has the receiver (§2) |
| `antenna_port … is not a … port` | a misspelled port, or a port on an RSP1A/1B, which have a single input |
| `agc_enabled is not a setting` | remove the key: there is no valid occupancy survey with AGC on |
| `refusing to write a dataset missing…` | a required acquisition field is absent; the message names it |
| unknown config key | misspelled settings are refused rather than ignored |
| everything reads ~0% | check the `bcast_fm` control verdict first, then the antenna port |
| macOS: numpy/dyld errors that make no sense | duplicate `LC_RPATH`, see [docs/rspdx-on-macos.md](docs/rspdx-on-macos.md) |
| Linux `rtl_test`: no supported devices | the DVB-T driver claimed the dongle: blacklist `dvb_usb_rtl28xxu` and reboot |

---

## 10. RTL-SDR backend

```bash
sudo apt install rtl-sdr        # Fedora: dnf install rtl-sdr · macOS: brew install librtlsdr
rtl_test -t                     # the dongle is seen
```

On Windows, download an rtl-sdr release and put its folder on `PATH`.
Then set `backend: "rtl_sdr"` and a fixed `gain` (never `0`, which means
automatic).

---

## 11. Uninstall

```bash
python -m pip uninstall occupancy-survey
```

Datasets in `data/` and `out/` are plain files and are not removed. To
remove radioconda on Windows, use *Add or remove programs*. The SDRplay
API has its own uninstaller.

Bringing up a new survey host with Claude Code:
[docs/survey-host-bringup.md](docs/survey-host-bringup.md).
