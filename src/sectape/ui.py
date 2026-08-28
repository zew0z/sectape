"""Terminal presentation.

A tape deck: a red dot while it is running, a square when it stops, and a
counter in between. Colour is dropped when the output is not a terminal or
NO_COLOR is set, and every glyph degrades to ASCII when the terminal cannot
say it speaks UTF-8.
"""
from __future__ import annotations

import os
import shutil
import sys

from .terminal import char_width

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

RED = "\033[38;5;203m"
GREEN = "\033[38;5;114m"
YELLOW = "\033[38;5;179m"
BLUE = "\033[38;5;110m"
GREY = "\033[38;5;245m"

_UNICODE = {
    "rec": "⏺", "stop": "⏹", "pause": "⏸", "reel": "◉", "tape": "━",
    "rule": "─", "arrow": "→", "dot": "·", "warn": "⚠", "tick": "✓",
    "cross": "✗", "bar": "▍", "note": "❯",
}
_ASCII = {
    "rec": "*", "stop": "#", "pause": "=", "reel": "o", "tape": "-",
    "rule": "-", "arrow": "->", "dot": "-", "warn": "!", "tick": "+",
    "cross": "x", "bar": "|", "note": ">",
}


def display_width(text: str) -> int:
    """Screen columns a string occupies, counting wide characters as two."""
    return sum(char_width(ch) for ch in text)


def fit(text: str, width: int) -> str:
    """Truncate to `width` screen columns, then pad to exactly that.

    `str.ljust` counts characters, so a session named in Japanese - or with an
    emoji in it - pushed every column after it out of line.
    """
    kept, used = [], 0
    for ch in text:
        step = char_width(ch)
        if used + step > width:
            break
        kept.append(ch)
        used += step
    return "".join(kept) + " " * max(0, width - used)


def unicode_ok() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in encoding:
        return True
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        if "utf" in os.environ.get(name, "").lower():
            return True
    return False


def colour_ok(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("SECTAPE_COLOR", "").lower() in ("0", "never", "off"):
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


class Style:
    """Glyphs and colour, resolved once for the current output stream."""

    def __init__(self, colour: bool | None = None, unicode_: bool | None = None):
        self.colour = colour_ok() if colour is None else colour
        self.glyphs = _UNICODE if (unicode_ok() if unicode_ is None else unicode_) else _ASCII

    def g(self, name: str) -> str:
        return self.glyphs.get(name, "")

    def paint(self, text: str, *codes: str) -> str:
        if not self.colour or not codes:
            return text
        return "".join(codes) + text + RESET

    # -- shorthands --------------------------------------------------------
    def dim(self, text): return self.paint(text, DIM)
    def bold(self, text): return self.paint(text, BOLD)
    def red(self, text): return self.paint(text, RED)
    def green(self, text): return self.paint(text, GREEN)
    def yellow(self, text): return self.paint(text, YELLOW)
    def blue(self, text): return self.paint(text, BLUE)
    def grey(self, text): return self.paint(text, GREY)

    # -- blocks ------------------------------------------------------------
    def width(self, cap: int = 64) -> int:
        try:
            return min(cap, max(28, shutil.get_terminal_size((80, 24)).columns - 4))
        except Exception:
            return cap

    def rule(self, width: int | None = None) -> str:
        return self.dim(self.g("rule") * (width or self.width()))

    def deck(self, state: str, title: str) -> list[str]:
        """The header line: a transport symbol, a label, and a rule."""
        if state == "rec":
            badge = self.paint(f"{self.g('rec')} REC", RED, BOLD)
        elif state == "stop":
            badge = self.paint(f"{self.g('stop')} STOP", GREY, BOLD)
        else:
            badge = self.paint(f"{self.g('pause')} {state.upper()}", YELLOW, BOLD)
        return [f"  {badge}  {self.bold(title)}", "  " + self.rule()]

    def field(self, label: str, value: str) -> str:
        return f"    {self.grey(label.ljust(9))} {value}"

    def hint(self, *pairs: tuple[str, str]) -> str:
        joined = f"   {self.dim(self.g('dot'))}   ".join(
            f"{self.bold(key)} {self.grey(what)}" for key, what in pairs)
        return f"    {joined}"

    def counter(self, *parts: str) -> str:
        sep = f" {self.dim(self.g('dot'))} "
        return "    " + sep.join(p for p in parts if p)


def style_for(stream=None) -> Style:
    return Style(colour=colour_ok(stream))
