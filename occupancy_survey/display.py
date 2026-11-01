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

  FOREIGN OUTPUT -- anything else writing to the terminal moves the cursor
  without the display knowing. Python logging can be quieted, but the
  SDRplay library prints `overload detected` from C++ straight to the
  console, and on 2026-11-01 those lines broke a move-up-N-rows redraw into
  a scrolling log of repeated headers over a twelve-hour run. So nothing
  here counts rows: the block is drawn on the alternate screen, from the
  top-left every frame, and everything below it is cleared. A stray line
  shows below the block until the next frame removes it. On exit the normal
  screen comes back and the last frame is printed once into the scrollback.
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
        self._alt = False
        self._last = ""
        try:
            tty = self._stream.isatty()
        except Exception:
            tty = False
        self._in_place = bool(tty) and not force_plain and _enable_windows_vt()

    @property
    def in_place(self) -> bool:
        return self._in_place

    def _size(self) -> tuple[int, int]:
        try:
            size = shutil.get_terminal_size()
            return max(20, size.columns), max(5, size.lines)
        except Exception:
            return 80, 24

    def _width(self) -> int:
        return self._size()[0]

    def invalidate(self) -> None:
        """Something else wrote to the stream.

        Kept for callers; every in-place frame already redraws from the
        top-left, so there is no cursor state left to correct.
        """

    def render(self, text: str) -> None:
        width, height = self._size()
        # Truncation is not cosmetic: a wrapped line, or a block taller
        # than the screen, scrolls the alternate screen and pushes the top
        # of the block out of view.
        lines = [ln[:width] for ln in text.splitlines()]
        self._last = "\n".join(lines)

        if not self._in_place:
            if self._rows:
                self._stream.write("\n")
            self._stream.write(self._last + "\n")
            self._stream.flush()
            self._rows = len(lines)
            return

        lines = lines[:height - 1]
        out = []
        if not self._alt:
            out.append(f"{CSI}?1049h")                 # alternate screen
            self._alt = True
        out.append(f"{CSI}H")                          # top-left
        for ln in lines:
            out.append(f"{ln}{CSI}K\n")                # clear to end of line
        out.append(f"{CSI}J")                          # clear everything below
        self._stream.write("".join(out))
        self._stream.flush()
        self._rows = len(lines)

    def finish(self) -> None:
        """Return to the normal screen and leave the final frame there."""
        if self._alt:
            self._stream.write(f"{CSI}?1049l{self._last}\n")
            self._alt = False
        elif self._rows:
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
