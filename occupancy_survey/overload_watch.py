"""Capture the SDRplay API's overload reports, which it writes to fd 2.

WHY THIS IS NOT OPTIONAL. Front-end overload is this project's dominant
error, and it is the error that FLATTERS the result: a compressed front end
generates intermodulation products, those products look like carriers, and
spurious carriers raise the measured occupancy. A survey that overloads
reports a busier band than exists and looks more successful for it.

The SDRplay API detects overload and announces it -- `overload detected -
please reduce gain` -- but it announces it from C++ onto file descriptor 2.
gr-sdrplay3 exposes no callback, no counter and no flag, so for three runs
of this tool the authoritative signal scrolled past the operator's eyes and
reached neither the preflight verdict nor the dataset. The clip counter in
the GNU Radio backend missed all of it: the IF attenuator sits DOWNSTREAM of
the stage that compresses, so the samples arriving are well below full scale
while the analog path is being driven hard.

So this captures fd 2 at the OS level, which is the only level the message
exists at.

IT TEES, IT DOES NOT SWALLOW. Everything written to fd 2 is passed straight
through to the real stderr, because fd 2 is shared with Python's logging and
with every other library in the process. A diagnostic tool that eats
diagnostics would be a poor trade.

WHAT THIS FORECLOSES, stated plainly. Redirecting fd 2 is process-global and
not reentrant: one watcher at a time, and a second would capture the first's
tee. It also cannot work on a platform without os.dup2, and it will not see
messages a library writes by opening /dev/stderr itself. If any of that ever
becomes a problem, the right answer is a patch to gr-sdrplay3 exposing the
API's own callback, not a cleverer version of this.

AND IT DOES NOT ACTUALLY CATCH THE RSPdx MESSAGES. Measured, 2026-11-01: a
run printed `rspdx :warning: overload corrected` to the console with this
watcher running from before the device was opened, and the count came back
zero. It counts correctly when something writes to fd 2 in this process --
there are tests -- so the SDRplay path is not going through our fd 2. The
likely routes are the API's separate service process, or a descriptor the
library duplicated when it was loaded, which is before any Python code here
can intervene.

So this module currently buys very little, and `overload_reports()` reading
zero MUST NOT be treated as evidence of a healthy front end. The working
instrument turned out to be GnuRadioBackend.gain_in_effect(): the gain
readback shows the API's correction directly, in dB, from the device itself.
Keep this watcher for the cases where it does fire, and do not build on it.
The honest fix remains a gr-sdrplay3 patch exposing the API's callback.

ON A WINDOWS CONSOLE IT IS NOT STARTED AT ALL. There, Python writes stderr
through the console handle behind fd 2, and once fd 2 is a pipe every write
fails with `WinError 1: Incorrect function`. Measured 2026-10-31: each log
line during the watched window printed `--- Logging error ---` instead, and
the watcher runs for a backend's whole lifetime, so an overnight survey
would have logged nothing at all. Losing a watcher that has never caught an
RSPdx report is the cheaper side of that trade. With stderr redirected to a
file or pipe it is an ordinary descriptor and the watcher runs as elsewhere.

WHERE THE REPORTS ACTUALLY GO -- found 2026-11-02, and it supersedes the
guesses above. The messages are GNU Radio log lines: gr-sdrplay3 handles the
API's PowerOverloadChange event and calls its block logger, whose pattern is
`%n :%l: %v`. GNU Radio's own config decides where that logger writes, and
radioconda ships `[LOG] log_file = stdout` -- not stderr, which is why an
fd 2 capture never saw one. On Windows the sink is spdlog's wincolor sink,
which takes the Win32 std handle once, when GNU Radio's logging starts, so
redirecting a CRT descriptor afterwards cannot reach it either.

So GnuRadioLogCapture below goes upstream of all that: BEFORE gnuradio is
imported it sets GR_CONF_LOG_LOG_FILE=stderr and, on Windows, points the
Win32 STD_ERROR_HANDLE (not fd 2, which Python's console stream still uses)
at a pipe. spdlog sees a non-console handle and writes each line with
WriteFile, unbuffered; measured, a warning reached the pipe in the same
millisecond it was logged. A file sink would have been simpler and is wrong:
spdlog buffers it, and nothing reached the file until the process exited.
The lines then go to a timestamped log file instead of scrolling over the
live display, and each overload is recorded with when it happened and what
the receiver was doing -- the question a bare count cannot answer is
whether an overload is a retune transient or a steady condition.
"""

from __future__ import annotations

import collections
import datetime as _dt
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GR_LOG_ENV = "GR_CONF_LOG_LOG_FILE"

# An overload within this long of a retune or gain change is filed as
# "settling": the RSPdx re-runs its LNA/PLL setup on both, and the lab logs
# put most reports at exactly those moments (port switches, retunes).
SETTLING_S = 2.0

# Kept in the dataset; the log file has every one.
RECENT_EVENTS = 100

# The API's wording, as observed on gr-sdrplay3 with SDRplay API 3.15.1:
#     rspdx :warning: overload detected - please reduce gain
#     rspdx :warning: overload corrected
# Matched loosely on purpose: the vendor's exact phrasing is not a contract,
# and a missed count is worse than a slightly greedy one.
PATTERNS = {
    "overload_detected": re.compile(rb"overload\s+detected", re.I),
    "overload_corrected": re.compile(rb"overload\s+corrected", re.I),
}


def _unsupported_reason(stream) -> str | None:
    """Why fd 2 cannot be redirected here, or None if it can."""
    if not (hasattr(os, "dup2") and hasattr(os, "pipe")):
        return "this platform has no os.dup2"
    raw = getattr(getattr(stream, "buffer", None), "raw", None)
    if type(raw).__name__ == "_WindowsConsoleIO":
        return ("stderr is a Windows console, which redirecting fd 2 would "
                "break for every log line")
    return None


def _utc(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat(
        timespec="milliseconds")


def _set_win32_stderr(handle) -> bool:                    # pragma: no cover
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    k32.SetStdHandle.restype = ctypes.c_int
    return bool(k32.SetStdHandle(0xFFFFFFF4, handle))    # STD_ERROR_HANDLE


class GnuRadioLogCapture:
    """GNU Radio's log, moved off the console and onto a pipe we read.

    Process-wide, because the std handle is. install() must run before
    gnuradio is first imported: GNU Radio reads its log config, and spdlog
    takes the handle, exactly once.
    """

    _active: "GnuRadioLogCapture | None" = None

    def __init__(self, log_path, read_fd: int):
        self.log_path = str(log_path) if log_path else None
        self._subscribers = []
        self._lock = threading.Lock()
        self._file = None
        if self.log_path:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.log_path, "a", encoding="utf-8",
                              errors="replace")
        self._thread = threading.Thread(
            target=self._pump, args=(read_fd,), name="gr_log_capture",
            daemon=True)
        self._thread.start()

    @classmethod
    def active(cls) -> "GnuRadioLogCapture | None":
        return cls._active

    @classmethod
    def install(cls, log_path, *, platform=None,
                set_std_handle=None) -> "GnuRadioLogCapture | None":
        """Route GNU Radio's log here. None when it cannot or need not.

        Off Windows this only sets the log to stderr, where the fd 2 tee
        already works; it returns None and OverloadWatcher tees as before.
        """
        if cls._active is not None:
            return cls._active
        if "gnuradio.gr" in sys.modules:
            logger.warning(
                "gnuradio was imported before the log capture was installed; "
                "the SDRplay overload reports will go wherever GNU Radio's "
                "config sends them, uncounted")
            return None
        os.environ[GR_LOG_ENV] = "stderr"
        if (platform or sys.platform) != "win32":
            return None
        if set_std_handle is None:                         # pragma: no cover
            import msvcrt
            read_fd, write_fd = os.pipe()
            if not _set_win32_stderr(msvcrt.get_osfhandle(write_fd)):
                os.close(read_fd)
                os.close(write_fd)
                logger.warning("could not redirect the Win32 stderr handle; "
                               "overload reports will not be counted")
                return None
        else:
            read_fd, write_fd = set_std_handle()
        # write_fd stays open for the life of the process: the std handle IS
        # it, and closing it would hand spdlog a dead handle.
        cls._active = cls(log_path, read_fd)
        return cls._active

    @classmethod
    def _reset_for_tests(cls) -> None:
        active, cls._active = cls._active, None
        if active is not None:
            active._thread.join(timeout=2.0)
            if active._file is not None:
                active._file.close()

    def subscribe(self, fn) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn) -> None:
        with self._lock:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

    def _pump(self, read_fd: int) -> None:
        buf = b""
        while True:
            try:
                chunk = os.read(read_fd, 65536)
            except OSError:                              # pragma: no cover
                break
            if not chunk:
                break
            t = time.time()
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                self._dispatch(line.rstrip(b"\r"), t)
        if buf:
            self._dispatch(buf.rstrip(b"\r"), time.time())

    def _dispatch(self, line: bytes, t: float) -> None:
        with self._lock:
            subs = list(self._subscribers)
        notes = []
        for fn in subs:
            try:
                note = fn(line, t)
            except Exception:                            # pragma: no cover
                logger.exception("overload subscriber failed")
                note = None
            if note:
                notes.append(note)
        if self._file is not None:
            text = line.decode("utf-8", "replace")
            suffix = ("  [" + "; ".join(notes) + "]") if notes else ""
            try:
                self._file.write(f"{_utc(t)} {text}{suffix}\n")
                self._file.flush()
            except (OSError, ValueError):                # pragma: no cover
                pass


class OverloadWatcher:
    """Count the API's overload reports, and record when each happened.

    Reads GnuRadioLogCapture when one is installed, and otherwise tees file
    descriptor 2. Unsupported platforms degrade to counting nothing and
    saying so, rather than failing a survey over a diagnostic.

    `context`, when given, is called on each report and returns what the
    receiver was doing: band, center_hz, and the time.time() of the last
    retune and gain change. That is what splits a settling transient from a
    steady overload during a dwell -- only the second biases a duty cycle.
    """

    def __init__(self, patterns=None, stderr=None, context=None,
                 capture=None):
        self._patterns = patterns or PATTERNS
        self._counts = {k: 0 for k in self._patterns}
        self._lock = threading.Lock()
        self._saved_fd = None
        self._write_fd = None
        self._thread = None
        self._context = context
        self._capture = (capture if capture is not None
                         else GnuRadioLogCapture.active())
        self._subscribed = False
        if self._capture is not None:
            self._unsupported_note = None
        else:
            self._unsupported_note = _unsupported_reason(
                sys.stderr if stderr is None else stderr)
        self._supported = self._unsupported_note is None
        self._lines = 0
        self._by_band = collections.Counter()
        self._by_phase = collections.Counter()
        self._recent = collections.deque(maxlen=RECENT_EVENTS)

    # ------------------------------------------------------------------ #

    def start(self) -> "OverloadWatcher":
        if self._capture is not None:
            if not self._subscribed:
                self._capture.subscribe(self._on_line)
                self._subscribed = True
            return self
        if not self._supported or self._saved_fd is not None:
            return self
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            self._saved_fd = os.dup(2)
            read_fd, self._write_fd = os.pipe()
            os.dup2(self._write_fd, 2)
        except OSError as e:                             # pragma: no cover
            logger.warning("cannot watch fd 2 for overload reports: %s", e)
            self._cleanup_failed_start()
            return self

        self._thread = threading.Thread(
            target=self._pump, args=(read_fd, self._saved_fd),
            name="overload_watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        """Restore fd 2 and return the counts. Safe to call twice."""
        if self._subscribed:
            self._capture.unsubscribe(self._on_line)
            self._subscribed = False
            return self.counts
        if self._saved_fd is None:
            return self.counts
        try:
            sys.stderr.flush()
        except Exception:
            pass
        # Restore BEFORE closing the pipe, so anything written in between
        # lands on the real stderr rather than on a closed descriptor.
        os.dup2(self._saved_fd, 2)
        try:
            os.close(self._write_fd)
        except OSError:                                  # pragma: no cover
            pass
        if self._thread is not None:
            # Bounded: a stuck reader must not hang a survey's shutdown.
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():                  # pragma: no cover
                logger.warning("overload watcher thread did not finish; "
                               "counts may be short")
        try:
            os.close(self._saved_fd)
        except OSError:                                  # pragma: no cover
            pass
        self._saved_fd = self._write_fd = self._thread = None
        return self.counts

    def _cleanup_failed_start(self) -> None:             # pragma: no cover
        for fd in (self._write_fd, self._saved_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._saved_fd = self._write_fd = None

    def _pump(self, read_fd: int, out_fd: int) -> None:
        buf = b""
        try:
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                # Tee first: a dropped diagnostic is worse than a late count.
                try:
                    os.write(out_fd, chunk)
                except OSError:                          # pragma: no cover
                    pass
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    self._count_line(line)
                if len(buf) > 1 << 20:                   # pragma: no cover
                    # A single unterminated megabyte is not a log line.
                    self._count_line(buf)
                    buf = b""
        finally:
            if buf:
                self._count_line(buf)
            try:
                os.close(read_fd)
            except OSError:                              # pragma: no cover
                pass

    def _on_line(self, line: bytes, t: float):
        return self._count_line(line, t)

    def _count_line(self, line: bytes, t: float | None = None):
        """Count one line; return a short note for the log if it matched."""
        t = time.time() if t is None else t
        with self._lock:
            self._lines += 1
            kinds = [name for name, pattern in self._patterns.items()
                     if pattern.search(line)]
            for name in kinds:
                self._counts[name] += 1
        if not kinds:
            return None
        event = {"t_utc": _utc(t), "kind": kinds[0].replace("overload_", "")}
        event.update(self._where(t))
        with self._lock:
            self._by_band[event.get("band") or "unknown"] += 1
            self._by_phase[event["phase"]] += 1
            self._recent.append(event)
        bits = [event["phase"]]
        if event.get("band"):
            bits.append(event["band"])
        if event.get("center_mhz") is not None:
            bits.append(f"{event['center_mhz']:.4f} MHz")
        if event.get("s_since_change") is not None:
            bits.append(f"{event['s_since_change']:.1f} s after "
                        f"{event['last_change']}")
        return ", ".join(bits)

    def _where(self, t: float) -> dict:
        """What the receiver was doing at t, and the phase that implies."""
        ctx = {}
        if self._context is not None:
            try:
                ctx = dict(self._context() or {})
            except Exception:                            # pragma: no cover
                ctx = {}
        out = {}
        if ctx.get("band"):
            out["band"] = ctx["band"]
        if ctx.get("center_hz"):
            out["center_mhz"] = round(float(ctx["center_hz"]) / 1e6, 6)
        changes = {k: ctx.get(k) for k in ("retune", "gain")
                   if ctx.get(k) is not None}
        if not changes:
            out["phase"] = "startup"
            return out
        last, at = max(changes.items(), key=lambda kv: kv[1])
        since = max(0.0, t - at)
        out["last_change"] = last
        out["s_since_change"] = round(since, 3)
        out["phase"] = "settling" if since < SETTLING_S else "dwell"
        return out

    # ------------------------------------------------------------------ #

    @property
    def counts(self) -> dict:
        with self._lock:
            out = dict(self._counts)

        # A `corrected` implies a `detected` that we may not have seen -- the
        # API reports the onset from inside its own startup, before a watcher
        # can be in place. So the episode count is the larger of the two, not
        # the `detected` tally.
        #
        # This is not hypothetical: a run printed one `overload corrected`
        # with no `detected`, and keying the verdict on `detected` alone put
        # "none reported in this sweep" three lines under the warning.
        detected = out.get("overload_detected", 0)
        corrected = out.get("overload_corrected", 0)
        out["overload_events"] = max(detected, corrected)
        if corrected > detected:
            out["onset_before_watching"] = corrected - detected
        with self._lock:
            out["by_phase"] = dict(self._by_phase)
            out["by_band"] = dict(self._by_band)
        out["route"] = ("gnuradio_log" if self._capture is not None
                        else "fd2")
        out["supported"] = self._supported
        if not self._supported:
            out["note"] = (f"{self._unsupported_note}; the API's overload "
                           "reports were not captured and the zero counts "
                           "below mean 'not observed', not 'none occurred'")
        return out

    def as_metadata(self) -> dict:
        """Dataset form, with the asymmetry spelled out for a later reader."""
        c = self.counts
        detected = c.get("overload_events", 0)
        route = ("GNU Radio's log, which is where gr-sdrplay3 sends them"
                 if c["route"] == "gnuradio_log" else "fd 2")
        out = {
            **c,
            "interpretation": (
                f"Counts of the SDRplay API's own overload reports, captured "
                f"from {route}, for this run so far. A non-zero count means "
                f"the analog front end was driven into its limit; "
                f"intermodulation products from it look like carriers and "
                f"RAISE measured occupancy, so affected channels read busier "
                f"than they are. by_phase splits them: 'settling' is within "
                f"{SETTLING_S:g} s of a retune or gain change (a transient "
                f"the retune settle mostly discards), 'dwell' is during "
                f"measurement and is the kind that biases duty cycles."
            ),
        }
        with self._lock:
            out["recent_events"] = list(self._recent)
        if self._capture is not None and self._capture.log_path:
            out["log_file"] = Path(self._capture.log_path).name
        # ASYMMETRIC, on purpose. A count is evidence of overload; a zero is
        # not evidence of its absence. Measured 2026-11-01: a 15-minute run
        # with stderr redirected to a file, so the watcher was running,
        # captured 0 while the same file received 3,321 `overload detected`
        # and 5,309 `overload corrected` lines from the SDRplay library --
        # which writes through a stderr this capture never sees. The
        # dataset said "no_overload_reported". A verdict that reads as a
        # clean bill of health beside thousands of reports is the false
        # reassurance this module exists to prevent, so a zero is
        # "none_captured" and the interpretation says what that is worth.
        if detected and c.get("supported"):
            out["verdict"] = "overloaded"
        elif c.get("supported") and c["route"] == "gnuradio_log":
            out["verdict"] = "none_captured"
            out["interpretation"] += (
                " None were captured. This route reads the logger that "
                "prints the reports, so a zero is far better evidence than "
                "the fd 2 zero was -- but it was verified with a logged test "
                "line, not yet against a real overload, so read it with the "
                "gain lock and --intermod-check until a run has shown both.")
        elif c.get("supported"):
            out["verdict"] = "none_captured"
            out["interpretation"] += (
                " NONE WERE CAPTURED, WHICH IS NOT EVIDENCE OF A HEALTHY "
                "FRONT END: on an RSPdx the library's reports were measured "
                "to bypass this capture entirely (0 captured while 3,321 "
                "reached the same log file). Use clip_fraction, the gain "
                "lock and --intermod-check instead.")
        else:
            out["verdict"] = "not_observed"
        return out

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
