"""Terminal handling: window size, reset sequences, and a VT replayer.

The replayer turns a raw PTY capture back into the text that was on screen.
It is deliberately not a faithful emulator - there is no scroll region and
wrapped rows are rejoined - because the goal is logical lines for a document,
not an 80-column screenshot.
"""
from __future__ import annotations

import fcntl
import re
import struct
import sys
import termios


CSI_RE = re.compile(r"[0-?]*[ -/]*[@-~]")


class VTScreen:
    """An unbounded-height terminal transcript.

    Width matters: shells and readline redraw prompts with absolute cursor
    moves that assume the terminal wraps at a known column, so a widthless
    replay produces enormous space-padded lines. Rows that were filled by
    autowrap are flagged and rejoined on output, so the note gets logical
    lines rather than an 80-column screenshot.
    """

    TABSTOP = 8
    DEFAULT_WIDTH = 80

    def __init__(self, width: int = DEFAULT_WIDTH) -> None:
        self.width = max(20, int(width or self.DEFAULT_WIDTH))
        self.lines: list[str] = [""]
        self.wrap: list[bool] = [False]
        self.row = 0
        self.col = 0
        self.top = 0                      # origin row for absolute addressing
        self._saved = (0, 0)
        self.alt = False
        self._stash = None

    def resize(self, width: int) -> None:
        if width and int(width) >= 20:
            self.width = int(width)

    # -- internals ---------------------------------------------------------
    def _ensure_row(self) -> None:
        while len(self.lines) <= self.row:
            self.lines.append("")
            self.wrap.append(False)

    def _put(self, ch: str) -> None:
        if self.col >= self.width:                 # deferred autowrap
            self._ensure_row()
            self.wrap[self.row] = True
            self.row += 1
            self.col = 0
        self._ensure_row()
        line = self.lines[self.row]
        if len(line) < self.col:
            line += " " * (self.col - len(line))
        self.lines[self.row] = line[: self.col] + ch + line[self.col + 1 :]
        self.col += 1

    def _erase_line(self, mode: int) -> None:
        self._ensure_row()
        line = self.lines[self.row]
        if mode == 0:
            self.lines[self.row] = line[: self.col]
            self.wrap[self.row] = False
        elif mode == 1:
            keep = line[self.col + 1 :] if len(line) > self.col else ""
            self.lines[self.row] = " " * min(self.col + 1, len(line)) + keep
        elif mode == 2:
            self.lines[self.row] = ""
            self.wrap[self.row] = False

    def _new_region(self) -> None:
        """`clear`/reset: keep the transcript, start addressing fresh below."""
        if self.lines and self.lines[-1] != "":
            self.lines.append("")
            self.wrap.append(False)
        self.top = len(self.lines) - 1
        self.row = self.top
        self.col = 0
        self.wrap[self.row] = False

    def _erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            if self.alt:
                self.lines, self.wrap = [""], [False]
                self.row = self.col = self.top = 0
            else:
                self._new_region()
        elif mode == 0:
            del self.lines[self.row + 1 :]
            del self.wrap[self.row + 1 :]
            self._erase_line(0)
        elif mode == 1:
            self._erase_line(1)

    def _enter_alt(self) -> None:
        if self.alt:
            return
        self._stash = (self.lines, self.wrap, self.row, self.col, self.top)
        self.lines, self.wrap = [""], [False]
        self.row = self.col = self.top = 0
        self.alt = True

    def _leave_alt(self) -> None:
        if not self.alt:
            return
        # Full-screen program output (vim, less, htop) is discarded on purpose:
        # a redrawn screen is noise in a lesson note.
        self.lines, self.wrap, self.row, self.col, self.top = self._stash
        self._stash = None
        self.alt = False

    # -- CSI ---------------------------------------------------------------
    def _csi(self, body: str) -> None:
        final = body[-1]
        params_raw = body[:-1]
        private = params_raw[:1] in ("?", ">", "<", "=")
        if private:
            params_raw = params_raw[1:]
        params_raw = re.sub(r"[ -/]", "", params_raw)
        nums = []
        for part in params_raw.split(";"):
            try:
                nums.append(int(part))
            except ValueError:
                nums.append(0)
        n = nums[0] if nums else 0
        last_col = self.width - 1

        if private:
            if final in ("h", "l"):
                for code in nums:
                    if code in (1049, 47, 1047):
                        self._enter_alt() if final == "h" else self._leave_alt()
            return

        if final == "A":
            self.row = max(self.top, self.row - max(1, n))
        elif final == "B":
            self.row += max(1, n)
            self._ensure_row()
        elif final == "C":
            self.col = min(last_col, self.col + max(1, n))
        elif final == "D":
            self.col = max(0, self.col - max(1, n))
        elif final == "E":
            self.row += max(1, n)
            self.col = 0
            self._ensure_row()
        elif final == "F":
            self.row = max(self.top, self.row - max(1, n))
            self.col = 0
        elif final in ("G", "`"):
            self.col = min(last_col, max(0, max(1, n) - 1))
        elif final in ("H", "f"):
            r = max(1, nums[0] if nums else 1)
            c = max(1, nums[1] if len(nums) > 1 else 1)
            self.row = self.top + r - 1
            self.col = min(last_col, c - 1)
            self._ensure_row()
        elif final == "d":
            self.row = self.top + max(1, n) - 1
            self._ensure_row()
        elif final == "J":
            self._erase_display(n)
        elif final == "K":
            self._erase_line(n)
        elif final == "X":                          # erase n chars
            self._ensure_row()
            line = self.lines[self.row]
            cnt = max(1, n)
            if len(line) > self.col:
                self.lines[self.row] = line[: self.col] + " " * cnt + line[self.col + cnt :]
        elif final == "P":                          # delete n chars
            self._ensure_row()
            line = self.lines[self.row]
            self.lines[self.row] = line[: self.col] + line[self.col + max(1, n) :]
        elif final == "@":                          # insert n blanks
            self._ensure_row()
            line = self.lines[self.row]
            self.lines[self.row] = line[: self.col] + " " * max(1, n) + line[self.col :]
        elif final == "L":                          # insert lines
            self._ensure_row()
            for _ in range(max(1, n)):
                self.lines.insert(self.row, "")
                self.wrap.insert(self.row, False)
        elif final == "M":                          # delete lines
            self._ensure_row()
            for _ in range(max(1, n)):
                if self.row < len(self.lines):
                    del self.lines[self.row]
                    del self.wrap[self.row]
            self._ensure_row()
        # m, r, s, u, n, t, c ... : presentation/report only, ignored.

    # -- main loop ---------------------------------------------------------
    def feed(self, data: str) -> "VTScreen":
        i, size = 0, len(data)
        while i < size:
            ch = data[i]
            if ch == "\x1b":
                nxt = data[i + 1] if i + 1 < size else ""
                if nxt == "[":
                    m = CSI_RE.match(data, i + 2)
                    if not m:
                        break
                    self._csi(m.group(0))
                    i = m.end()
                    continue
                if nxt in ("]", "P", "^", "_"):
                    bel = data.find("\x07", i + 2)
                    st = data.find("\x1b\\", i + 2)
                    if bel == -1 and st == -1:
                        break
                    if bel == -1 or (st != -1 and st < bel):
                        i = st + 2
                    else:
                        i = bel + 1
                    continue
                if nxt in ("(", ")", "*", "+", "%", "#", " "):
                    i += 3
                    continue
                if nxt == "7":
                    self._saved = (self.row, self.col)
                elif nxt == "8":
                    self.row, self.col = self._saved
                    self._ensure_row()
                elif nxt == "M":
                    self.row = max(self.top, self.row - 1)
                elif nxt in ("D", "E"):
                    self.row += 1
                    if nxt == "E":
                        self.col = 0
                    self._ensure_row()
                elif nxt == "c":
                    self._new_region()
                i += 2
                continue

            if ch == "\r":
                self.col = 0
            elif ch in ("\n", "\x0b", "\x0c"):
                self.row += 1
                self._ensure_row()
            elif ch in ("\b", "\x7f"):
                self.col = max(0, self.col - 1)
            elif ch == "\t":
                self.col = min(self.width - 1,
                               ((self.col // self.TABSTOP) + 1) * self.TABSTOP)
            elif ch < " ":
                pass
            else:
                self._put(ch)
            i += 1
        return self

    def to_text(self) -> str:
        out, buf = [], ""
        for i, line in enumerate(self.lines):
            if i < len(self.wrap) and self.wrap[i]:
                buf += line
            else:
                out.append((buf + line).rstrip())
                buf = ""
        if buf:
            out.append(buf.rstrip())
        return "\n".join(out)


def render(data: str, width: int = VTScreen.DEFAULT_WIDTH) -> str:
    """Replay a raw capture and return what was on screen, as text."""
    return VTScreen(width).feed(data).to_text()


# --------------------------------------------------------------------------
# Shell integration markers
# --------------------------------------------------------------------------
# Private OSC 7337. Terminals ignore OSC codes they don't know, so these are
# invisible to the user while the session is live and are stripped by the VT
# emulator on replay.
#   begin: ESC ] 7337 ; THM ; b | <b64 command> | <epoch>            BEL
#   end:   ESC ] 7337 ; THM ; e | <exit code> | <epoch> | <b64 cwd>  BEL
#   size:  ESC ] 7337 ; THM ; w | <cols> | <rows>                    BEL
# The size marker is written straight into the log file, never to the tty.


# Everything a misbehaving full-screen program might have left switched on.
TERMINAL_RESET = (
    "\x1b[?1049l"                                   # leave alternate screen
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l"             # mouse reporting off
    "\x1b[?1005l\x1b[?1006l\x1b[?1015l"             # extended mouse off
    "\x1b[?2004l"                                   # bracketed paste off
    "\x1b[?7h"                                      # autowrap on
    "\x1b[?25h"                                     # cursor visible
    "\x1b[?1l\x1b>"                                 # normal cursor keys + keypad
    "\x1b[r"                                        # full scroll region
    "\x1b[m"                                        # reset colours/attributes
)


def get_winsize(fd: int) -> tuple[int, int, int, int]:
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
        if rows and cols:
            return rows, cols, xpix, ypix
    except Exception:
        pass
    return 24, 80, 0, 0


def current_size() -> tuple[int, int, int, int]:
    """Window size of the controlling terminal, safe to call anywhere."""
    try:
        return get_winsize(sys.stdin.fileno())
    except Exception:
        return 24, 80, 0, 0


def set_winsize(fd: int, rows: int, cols: int, xpix: int = 0, ypix: int = 0) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xpix, ypix))
    except Exception:
        pass
