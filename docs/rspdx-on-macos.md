# Getting the RSPdx working on one particular Mac

> **This is a troubleshooting note for a single host, not a prerequisite
> for the project.** This package is developed on this Mac and intended to
> *run elsewhere*. If you are deploying to a different machine, you do not
> need anything on this page — run
> `python -m occupancy_survey --check` on that machine instead, which
> reports what is missing there and what to do about it.
>
> Keep reading only if you want the RSPdx working on *this* development
> Mac, for local smoke-testing.

**Measured on 2026-10-28** (Darwin 24.6.0, Apple silicon). Each failure
below was reproduced from a neutral working directory. None of the fixes
needs `sudo`.

**Nothing here has been run.** These are proposed commands for the operator
to run, because repairing a shared conda environment and touching a device
are both the operator's call.

---

## The three breakages

### 1. radioconda's numpy is broken

```
$ ~/radioconda/bin/python -c "import numpy"
ImportError: Error importing numpy: you should not try to import numpy from
        its source directory; ...
```

The message is misleading — it is raised from radioconda's own
`site-packages`, not from a source tree, and it reproduces from any working
directory. scipy and matplotlib fail with the same error, since they import
numpy. **Consequence:** nothing in `morse_code_recorder` or in this repo's
GNU Radio backend can import in radioconda as it stands.

Proposed fix — reinstall numpy into that environment:

```bash
~/radioconda/bin/conda install -y --force-reinstall -c conda-forge numpy scipy
```

Verify from a directory that is not a source tree:

```bash
cd /tmp && ~/radioconda/bin/python -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"
```

### 2. gnuradio.sdrplay3 cannot find the SDRplay API

```
ImportError: dlopen(.../gnuradio/sdrplay3/sdrplay3_python...so):
  Library not loaded: @rpath/libsdrplay_api.so.3
  tried: '/Users/<you>/radioconda/lib/libsdrplay_api.so.3' (no such file)
```

The module's rpath is `~/radioconda/lib`. The API is installed at
`/usr/local/lib/libsdrplay_api.so.3`, which resolves through two symlinks to
`/Library/SDRplayAPI/3.15.1/lib/libsdrplay_api.so.3.15`. The two just never
meet. `~/radioconda/lib` is user-writable, so:

```bash
ln -s /usr/local/lib/libsdrplay_api.so.3 ~/radioconda/lib/libsdrplay_api.so.3
```

Verify:

```bash
cd /tmp && ~/radioconda/bin/python -c "from gnuradio import sdrplay3; print('sdrplay3 OK')"
```

### 3. SoapySDR's SDRplay module is built against the wrong API version

```
$ SoapySDRUtil --info
Module found: /usr/local/lib/SoapySDR/modules0.8/libsdrPlaySupport.so
  dlopen() failed: Library not loaded: libsdrplay_api.so.3.07
Available factories... No factories found!
```

That module wants API **3.07**; the system has **3.15**. This is a version
mismatch, not a path problem, so **a symlink is the wrong fix here** — it
would load a 3.15 library into a module compiled against a 3.07 ABI. It
needs `SoapySDRPlay3` rebuilt against the installed API.

Not worth fixing unless SoapySDR is wanted for its own sake: the
`gnuradio` route above and the `rtl_sdr` route both bypass it. The
`SoapySDR` **Python** module is not installed either.

---

## What works today, with no repair at all

| path | state |
|---|---|
| `--backend synthetic` | works now, no hardware |
| `--backend rtl_sdr` | works now; `/opt/local/bin/rtl_sdr` is present |
| `--backend rtl_power` | works now; `/opt/local/bin/rtl_power` is present |
| `--backend gnuradio` (RSPdx) | needs fixes 1 and 2 |

So the survey can start on an RTL-SDR while the RSPdx path is repaired, and
the datasets merge afterwards — that is what the format is for. The RSPdx is
the better receiver for this job (12-bit rather than 8-bit, so a strong local
carrier does not push the rest of a tuning toward the noise floor), which is
why it is worth repairing rather than working around.

---

## First acquisition commands, for the operator to run

A dry run that touches no hardware and writes a real dataset:

```bash
cd ~/Code/occupancy-survey && python -m occupancy_survey --config config.yaml --backend synthetic --cycles 3
```

Then, after the two repairs above, a short RSPdx run on the control band
only — broadcast FM, where the right answer is known in advance:

```bash
cd ~/Code/occupancy-survey && python -m occupancy_survey --config config.yaml --backend gnuradio --bands bcast_fm --duration 300
```

If that run's `control_check` verdict is not `pass`, stop and find out why
before collecting any target data: either no FM station is receivable at
this QTH, or the gain, the antenna or the threshold is wrong. Every target
number from a run whose control failed inherits the same fault.

(NOAA Weather Radio was the control until 2026-10-28 and is now a
measurement target — its carrier continuity is not established. See
[nwr-measurement.md](nwr-measurement.md).)
