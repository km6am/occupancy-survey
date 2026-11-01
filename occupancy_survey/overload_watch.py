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
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading

logger = logging.getLogger(__name__)

# The API's wording, as observed on gr-sdrplay3 with SDRplay API 3.15.1:
#     rspdx :warning: overload detected - please reduce gain
#     rspdx :warning: overload corrected
# Matched loosely on purpose: the vendor's exact phrasing is not a contract,
# and a missed count is worse than a slightly greedy one.
PATTERNS = {
    "overload_detected": re.compile(rb"overload\s+detected", re.I),
    "overload_corrected": re.compile(rb"overload\s+corrected", re.I),
}


class OverloadWatcher:
    """Tee file descriptor 2, counting the API's overload reports.

    Unsupported platforms degrade to counting nothing and saying so, rather
    than failing a survey over a diagnostic.
    """

    def __init__(self, patterns=None):
        self._patterns = patterns or PATTERNS
        self._counts = {k: 0 for k in self._patterns}
        self._lock = threading.Lock()
        self._saved_fd = None
        self._write_fd = None
        self._thread = None
        self._supported = hasattr(os, "dup2") and hasattr(os, "pipe")
        self._lines = 0

    # ------------------------------------------------------------------ #

    def start(self) -> "OverloadWatcher":
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

    def _count_line(self, line: bytes) -> None:
        with self._lock:
            self._lines += 1
            for name, pattern in self._patterns.items():
                if pattern.search(line):
                    self._counts[name] += 1

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
        out["supported"] = self._supported
        if not self._supported:                          # pragma: no cover
            out["note"] = ("this platform has no os.dup2; the API's overload "
                           "reports were not captured and the zero counts "
                           "below mean 'not observed', not 'none occurred'")
        return out

    def as_metadata(self) -> dict:
        """Dataset form, with the asymmetry spelled out for a later reader."""
        c = self.counts
        detected = c.get("overload_events", 0)
        out = {
            **c,
            "interpretation": (
                "Counts of the SDRplay API's own overload reports, captured "
                "from fd 2. A non-zero count means the analog front end was "
                "driven into its limit during this run; intermodulation "
                "products from it look like carriers and RAISE measured "
                "occupancy, so affected channels read busier than they are."
            ),
        }
        if detected and c.get("supported"):
            out["verdict"] = "overloaded"
        elif c.get("supported"):
            out["verdict"] = "no_overload_reported"
        else:                                            # pragma: no cover
            out["verdict"] = "not_observed"
        return out

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
