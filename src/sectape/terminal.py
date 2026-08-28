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
import unicodedata


CSI_RE = re.compile(r"[0-?]*[ -/]*[@-~]")

# A run of printable characters that are all exactly one column wide: no
# controls, no DEL, no C1, nothing combining or double-width. These can be
# appended to the current row wholesale instead of one at a time.
PLAIN_RUN_RE = re.compile(r"[\x20-\x7e\xa0-\u02ff]+")


def char_width(ch: str) -> int:
    """Columns a character occupies on screen: 0, 1 or 2.

    CJK and emoji are two columns wide, and combining marks are none. Counting
    every character as one put the cursor in the wrong place for the rest of
    the line whenever a prompt contained an emoji, because the shell computes
    its cursor moves in real columns.
    """
    if ch < "\u0300":                    # ASCII and Latin-1: always one column
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


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

    # A real terminal can be very narrow - a split tmux pane easily is - and
    # replaying one at a wider column than it had puts every wrap and every
    # carriage return in the wrong place. Only nonsense is refused.
    MIN_WIDTH = 1

    def __init__(self, width: int = DEFAULT_WIDTH) -> None:
        self.width = max(self.MIN_WIDTH, int(width or self.DEFAULT_WIDTH))
        # One entry per screen column. A double-width character owns two: the
        # character itself and an empty continuation cell.
        self.lines: list[list[str]] = [[]]
        self.wrap: list[bool] = [False]
        self.row = 0
        self.col = 0
        self.top = 0                      # origin row for absolute addressing
        self._saved = (0, 0)
        self.alt = False
        self._stash = None

    def resize(self, width: int) -> None:
        if width and int(width) >= self.MIN_WIDTH:
            self.width = int(width)

    # -- internals ---------------------------------------------------------
    def _ensure_row(self) -> None:
        while len(self.lines) <= self.row:
            self.lines.append([])
            self.wrap.append(False)

    @staticmethod
    def _break_wide(row: list[str], col: int) -> None:
        """Blank a double-width character that `col` lands in the middle of.

        Overwriting half of one leaves the other half behind; a real terminal
        erases both cells.
        """
        if 0 <= col < len(row):
            if row[col] == "" and col > 0:
                row[col - 1] = " "
                row[col] = " "

    def _put(self, ch: str) -> None:
        # Almost every character is a plain one landing at the end of the
        # current row, and the general path below spends most of its time
        # proving that. Recognising it directly is what keeps a multi-megabyte
        # capture to seconds rather than minutes.
        if ch < "\u0300" and self.col < self.width and self.row < len(self.lines):
            row = self.lines[self.row]
            if self.col == len(row):
                row.append(ch)
                self.col += 1
                return

        width = char_width(ch)
        if width == 0:                             # combining mark
            self._ensure_row()
            row = self.lines[self.row]
            if 0 < self.col <= len(row) and row[self.col - 1]:
                row[self.col - 1] += ch
            return
        if self.col + width > self.width:          # deferred autowrap
            self._ensure_row()
            self.wrap[self.row] = True
            self.row += 1
            self.col = 0
        self._ensure_row()
        row = self.lines[self.row]
        while len(row) < self.col + width:
            row.append(" ")
        self._break_wide(row, self.col)
        self._break_wide(row, self.col + width)
        row[self.col] = ch
        for offset in range(1, width):
            row[self.col + offset] = ""
        self.col += width

    def _erase_line(self, mode: int) -> None:
        self._ensure_row()
        row = self.lines[self.row]
        if mode == 0:
            self._break_wide(row, self.col)
            del row[self.col:]
            self.wrap[self.row] = False
        elif mode == 1:
            self._break_wide(row, self.col + 1)
            count = min(self.col + 1, len(row))
            row[:count] = [" "] * count
        elif mode == 2:
            row.clear()
            self.wrap[self.row] = False

    def _new_region(self) -> None:
        """`clear`/reset: keep the transcript, start addressing fresh below."""
        if self.lines and self.lines[-1]:
            self.lines.append([])
            self.wrap.append(False)
        self.top = len(self.lines) - 1
        self.row = self.top
        self.col = 0
        self.wrap[self.row] = False

    def _erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            if self.alt:
                self.lines, self.wrap = [[]], [False]
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
        self.lines, self.wrap = [[]], [False]
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
            row = self.lines[self.row]
            count = max(1, n)
            if len(row) > self.col:
                self._break_wide(row, self.col)
                self._break_wide(row, self.col + count)
                row[self.col:self.col + count] = [" "] * count
        elif final == "P":                          # delete n chars
            self._ensure_row()
            row = self.lines[self.row]
            count = max(1, n)
            self._break_wide(row, self.col)
            self._break_wide(row, self.col + count)
            del row[self.col:self.col + count]
        elif final == "@":                          # insert n blanks
            self._ensure_row()
            row = self.lines[self.row]
            self._break_wide(row, self.col)
            row[self.col:self.col] = [" "] * max(1, n)
        elif final == "L":                          # insert lines
            self._ensure_row()
            for _ in range(max(1, n)):
                self.lines.insert(self.row, [])
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

            if " " <= ch < "\u0300" and ch != "\x7f" and self.row < len(self.lines):
                # Bulk-append the whole run of plain characters when it lands
                # at the end of the row, which is the overwhelmingly common
                # case. Falls through to _put for anything else.
                row = self.lines[self.row]
                if self.col == len(row) and self.col < self.width:
                    # Bounded by the room left on the row: matching the whole
                    # remaining run and then slicing it made one very long
                    # line quadratic.
                    run = PLAIN_RUN_RE.match(data, i, i + self.width - self.col)
                    if run:
                        chunk = run.group(0)
                        row.extend(chunk)
                        self.col += len(chunk)
                        i += len(chunk)
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
            elif ch < " " or "\x80" <= ch <= "\x9f":
                # C1 controls are controls, not text. Printing them put raw
                # control bytes into the export.
                pass
            else:
                self._put(ch)
            i += 1
        return self

    def to_text(self) -> str:
        lines, wrap = self.lines, self.wrap
        if self.alt and self._stash is not None:
            # The capture ended with the alternate screen still up: a
            # full-screen program was killed, or the recording was stopped
            # from another tab while `less` was open. The real transcript is
            # the stashed one - keeping the redrawn screen instead threw the
            # whole session away and left a picture of vim.
            lines, wrap = self._stash[0], self._stash[1]
        out, buf = [], ""
        for i, cells in enumerate(lines):
            line = "".join(cells)
            if i < len(wrap) and wrap[i]:
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


# Everything a misbehaving full-screen program might have left switched on.
#
# DECSC/DECRC (ESC 7 / ESC 8) wrap the whole thing on purpose: resetting the
# scroll region with CSI r homes the cursor to row 1 column 1, so without the
# save/restore the next thing printed lands at the top of the screen, on top of
# whatever was already there.
TERMINAL_RESET = (
    "\x1b7"                                          # save cursor
    "\x1b[?1049l"                                    # leave alternate screen
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l"              # mouse reporting off
    "\x1b[?1005l\x1b[?1006l\x1b[?1015l"              # extended mouse off
    "\x1b[?2004l"                                    # bracketed paste off
    "\x1b[?7h"                                       # autowrap on
    "\x1b[?25h"                                      # cursor visible
    "\x1b[?1l\x1b>"                                  # normal cursor keys + keypad
    "\x1b[r"                                         # full scroll region (homes!)
    "\x1b[m"                                         # reset colours/attributes
    "\x1b8"                                          # put the cursor back
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
