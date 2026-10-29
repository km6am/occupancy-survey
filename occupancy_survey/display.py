"""A non-scrolling status block that degrades safely.

Redrawing in place is easy to get subtly wrong, so the failure modes are
handled explicitly rather than hoped away:

  NOT A TERMINAL -- piped to a file, run under nohup, systemd or cron. ANSI
  escapes would be garbage in a log, so the display falls back to plain
  appended blocks and nothing is lost.

  WINDOWS -- ANSI works in Windows 10+ consoles only once virtual terminal
  processing is enabled, which is a one-line ctypes call. If it cannot be
  enabled (older console, odd host) the display falls back to appending
  rather than printing escape codes as literal text.

  LINES WIDER THAN THE TERMINAL -- a wrapped line occupies two rows, so
  moving the cursor up by the number of lines written would land in the
  wrong place and corrupt the display from then on. Every line is truncated
  to the terminal width before being drawn.

  TERMINAL RESIZED MID-RUN -- the width is re-read on every frame.

  LOG OUTPUT INTERLEAVING -- anything else writing to the stream moves the
  cursor without the display knowing, so the next redraw eats the wrong
  rows. In-place mode therefore quiets console logging below WARNING, and a
  warning that does get through forces the next frame to redraw from
  scratch instead of moving up.
"""

from __future__ import annotations

import os
import shutil
import sys

CSI = "\033["


def _enable_windows_vt() -> bool:
    """Turn on ANSI processing for a Windows console. True if usable."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


class StatusDisplay:
    """Draws a block of text, replacing the previous one where possible."""

    def __init__(self, stream=None, force_plain: bool = False):
        self._stream = stream if stream is not None else sys.stdout
        self._rows = 0
        self._dirty = False
        try:
            tty = self._stream.isatty()
        except Exception:
            tty = False
        self._in_place = bool(tty) and not force_plain and _enable_windows_vt()

    @property
    def in_place(self) -> bool:
        return self._in_place

    def _width(self) -> int:
        try:
            return max(20, shutil.get_terminal_size().columns)
        except Exception:
            return 80

    def invalidate(self) -> None:
        """Something else wrote to the stream; redraw fresh, do not move up."""
        self._dirty = True

    def render(self, text: str) -> None:
        width = self._width()
        # Truncation is not cosmetic: a wrapped line occupies two terminal
        # rows, and the cursor arithmetic below counts lines, not rows.
        lines = [ln[:width] for ln in text.splitlines()]

        if self._in_place and self._rows and not self._dirty:
            self._stream.write(f"{CSI}{self._rows}F")   # up N, column 0
        elif self._rows:
            self._stream.write("\n")

        out = []
        for ln in lines:
            out.append(ln)
            if self._in_place:
                out.append(f"{CSI}K")                   # clear to end of line
            out.append("\n")
        # A shrinking block would leave stale rows below it.
        if self._in_place and self._rows > len(lines):
            for _ in range(self._rows - len(lines)):
                out.append(f"{CSI}K\n")
            out.append(f"{CSI}{self._rows - len(lines)}F")

        self._stream.write("".join(out))
        self._stream.flush()
        self._rows = len(lines)
        self._dirty = False

    def finish(self) -> None:
        """Leave the final frame on screen and put the cursor after it."""
        if self._rows:
            self._stream.write("\n")
            self._stream.flush()
        self._rows = 0


def attach_logging_guard(display: StatusDisplay) -> None:
    """Quiet console INFO while drawing in place, and notice warnings.

    Interleaved log lines move the cursor without the display knowing,
    which corrupts every later frame. Rather than fight that, INFO is
    quieted on console handlers while in-place drawing is active -- the
    display already shows what those lines said -- and anything at WARNING
    or above marks the next frame for a fresh draw, so an important message
    is never swallowed to keep the display tidy.
    """
    import logging

    if not display.in_place:
        return

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) \
                and handler.level < logging.WARNING:
            handler.setLevel(logging.WARNING)

    class _Guard(logging.Handler):
        def emit(self, record):
            display.invalidate()

    root.addHandler(_Guard(level=logging.WARNING))
