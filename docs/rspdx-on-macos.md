# GNU Radio + SDRplay on macOS — repair runbook

**Status: CONFIRMED WORKING** on macOS 15.7.5 / Apple M3 Max / radioconda,
2026-10-31.

Verified by, in this order:

```
204/204 package tests pass under ~/radioconda/bin/python
numpy 2.2.3, scipy 1.15.2, matplotlib 3.10.1 import
gnuradio 3.10.12.0, gnuradio.{blocks,fft,filter} import
gnuradio.sdrplay3 imports; sdrplay3.rspdx is callable
occupancy_survey's GR backend imports
--check reports: gnuradio importable, gr-sdrplay3 importable, Ready
```

Device access remains untested — no receiver was attached. Imports working
is necessary, not sufficient.

This document exists so the repair does not have to be re-derived. It took
a long time to find because **every symptom pointed somewhere other than
the cause.**

---

## 1. The cause

**macOS 15's dyld refuses to load any Mach-O binary carrying duplicate
`LC_RPATH` entries.** Not "skips the duplicate" — it fails the load
outright, *even when handed the file's absolute path*. Older dyld
tolerated it, which is why an installation that worked before an OS
upgrade stops working after one.

Several conda-forge builds carry `@loader_path` twice, or a path alongside
the same path with a trailing slash — textually distinct, semantically
identical. In this installation **368 Mach-O files** were affected.

`libgfortran.5.dylib` had three rpath entries and could not be loaded at
all. That broke openblas → numpy → scipy → matplotlib → gnuradio →
gr-sdrplay3. **One root cause presenting as a pile of unrelated failures.**

---

## 2. Why the symptoms mislead

This is the part worth reading. Three separate "diagnoses" were wrong, and
each was wrong because a message said something other than what was
happening.

**dyld reports a *search* failure.** It reads as "library not found" when
the file is sitting right there:

```
Library not loaded: @rpath/libgfortran.5.dylib
  Reason: tried: '.../lib/libgfortran.5.dylib' (duplicate LC_RPATH '@loader_path')
```

`(duplicate LC_RPATH …)` is the actual reason and is easy to read past as
noise about search paths.

**numpy wraps it in a misleading message.** You get *"you should not try to
import numpy from its source directory"* when you are nowhere near a source
tree. That sent the first investigation toward reinstalling numpy, which
would not have helped — numpy was never the problem.

**A missing symlink was blamed, and the symlink already existed.**
`gnuradio.sdrplay3` failed with what looked like an inability to find
`libsdrplay_api.so.3`. The symlink into `~/radioconda/lib` was already
present. dyld was refusing the load for the same duplicate-rpath reason.

### Get the real error before theorising

```bash
cd /tmp && python -c "import traceback
try: import numpy
except Exception: traceback.print_exc()"
```

The chained exception names the actual library and the actual reason. Read
it before changing anything.

---

## 3. The fix

```bash
python3 tools/fix_macos_rpaths.py ~/radioconda                    # dry run
python3 tools/fix_macos_rpaths.py ~/radioconda --apply -o log.json
```

Removes duplicate rpath entries, keeping one of each distinct value, and
re-signs each edited file ad-hoc — editing invalidates the existing
signature and an unsigned arm64 dylib will not load.

**Reversible.** `log.json` records every deletion;
`install_name_tool -add_rpath "<path>" <file>` puts one back.

**Patching beats reinstalling here.** The duplicates are baked into the
packages as built, so a fresh radioconda would most likely reproduce them.

Verify:

```bash
cd /tmp && ~/radioconda/bin/python -c "import numpy, scipy; from gnuradio import gr, sdrplay3; print('OK', gr.version())"
```

### Two traps in doing this by hand

- `install_name_tool -delete_rpath` prints warnings to stderr while
  succeeding, and the exit status of a *pipeline* is the last command's.
  Check the file afterwards with `otool -l`, not the exit code. The script
  re-reads each file to confirm.
- Conda's package cache under `~/radioconda/pkgs/` contains extracted
  tarballs that are never on a load path. Fixing them is harmless but
  pointless; the script skips them unless `--include-pkgs` is given.

---

## 4. What was NOT the problem

Recorded because each cost time:

| suspected | actual |
|---|---|
| numpy install corrupt | numpy was fine; openblas could not load libgfortran |
| `gr-sdrplay3` needs a symlink | it already existed; dyld refused the load |
| architecture mismatch | none — all arm64, API is a universal binary |
| SDRplay API version mismatch | `gr-sdrplay3` wants the generic `libsdrplay_api.so.3` soname, satisfied by 3.15.1 |
| `DYLD_LIBRARY_PATH` pollution | present (a JavaVM path) and cosmetic — the failure reproduced under `env -i` |

The SoapySDR SDRplay module **is** genuinely version-mismatched — built
against API `3.07` against an installed `3.15`, so `SoapySDRUtil` reports
"No factories found". It is not needed for the `gnuradio` backend and can
be ignored.

---

## 5. Still outstanding, and why it does not matter

About **145 Qt plugin and QML files** still carry duplicate rpaths. They
are not on the survey's load path, which is why everything above passes.
They would matter for GNU Radio Companion or any Qt GUI. To clear them:

```bash
python3 tools/fix_macos_rpaths.py ~/radioconda --apply -o log.json
```

The script is idempotent and makes several passes, so re-running is safe.

---

## 6. Diagnostics worth running before blaming the toolchain

```bash
# Is the API service running? macOS needs it.
ps aux | grep -i sdrplay_apiService

# Architectures must agree.
file ~/radioconda/bin/python3.12
file /Library/SDRplayAPI/*/lib/libsdrplay_api.so.*

# What soname does gr-sdrplay3 actually want?
otool -L ~/radioconda/lib/python3.12/site-packages/gnuradio/sdrplay3/*.so | grep sdrplay_api

# Does a specific library load on its own?
python -c "import ctypes; ctypes.CDLL('/full/path/to/lib.dylib')"
```

That last one is the most useful single test: it isolates one library from
the import chain and gives dyld's unfiltered reason.

---

## 7. Consider not using radioconda

This failure is a **conda packaging** problem, not a GNU Radio one. A
direct install — distribution package, or Homebrew, plus `gr-sdrplay3`
built from source — avoids the entire class, and has been the more
reliable route on other machines. It costs a source build of
`gr-sdrplay3`, which radioconda bundles. See INSTALL.md §4a.

Worth knowing for context: this radioconda was installed on 2026-03-28,
onto a macOS that had shipped the stricter dyld in 2024-09. **It was
broken from the moment it was installed and had never worked on this
machine** — there are no recordings or logs from the CW recorder here, and
that project's documented working setup is a Windows one.

---

## 8. Next step

`--check` passes. The untested part is the radio itself:

```bash
occupancy-survey --config config.yaml --check --device
```

That opens the receiver and takes one sweep. See
[survey-host-bringup.md](survey-host-bringup.md).
