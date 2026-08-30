"""Terminal presentation: colour discipline and glyph fallback.

A recording tool prints to pipes as often as to terminals, so escape codes
must not leak into anything being captured or diffed.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest

from sectape.ui import (Style, colour_ok, display_width, fit, style_for,
                        unicode_ok)
from tests.helpers import TempConfig

ESC = "\x1b"


class FakeStream:
    def __init__(self, tty: bool, encoding: str = "utf-8"):
        self._tty = tty
        self.encoding = encoding

    def isatty(self) -> bool:
        return self._tty


class TestColourDecision(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ("NO_COLOR", "SECTAPE_COLOR")}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_a_terminal_gets_colour(self):
        self.assertTrue(colour_ok(FakeStream(tty=True)))

    def test_a_pipe_does_not(self):
        self.assertFalse(colour_ok(FakeStream(tty=False)))

    def test_no_color_is_honoured_even_on_a_terminal(self):
        os.environ["NO_COLOR"] = "1"
        self.assertFalse(colour_ok(FakeStream(tty=True)))

    def test_no_color_is_honoured_when_empty(self):
        # The convention is that the variable's presence is what counts.
        os.environ["NO_COLOR"] = ""
        self.assertFalse(colour_ok(FakeStream(tty=True)))

    def test_sectape_color_off_switches(self):
        for value in ("0", "never", "off", "NEVER", "Off"):
            os.environ["SECTAPE_COLOR"] = value
            self.assertFalse(colour_ok(FakeStream(tty=True)), value)

    def test_sectape_color_on_forces_colour_through_a_pipe(self):
        # Piping into `less -R`, or a CI log that renders ANSI, is a terminal
        # as far as the reader is concerned; isatty cannot tell.
        for value in ("1", "always", "on", "yes", "true", "force", "ALWAYS", " On "):
            os.environ["SECTAPE_COLOR"] = value
            self.assertTrue(colour_ok(FakeStream(tty=False)), value)

    def test_sectape_color_on_beats_no_color(self):
        # The tool-specific setting is the more explicit of the two, so it is
        # the one that decides.
        os.environ["NO_COLOR"] = "1"
        os.environ["SECTAPE_COLOR"] = "always"
        self.assertTrue(colour_ok(FakeStream(tty=False)))

    def test_sectape_color_off_beats_a_terminal_and_no_color_alike(self):
        os.environ["NO_COLOR"] = "1"
        os.environ["SECTAPE_COLOR"] = "never"
        self.assertFalse(colour_ok(FakeStream(tty=True)))

    def test_an_unrecognised_sectape_color_leaves_the_guess_alone(self):
        # A typo must not silently mean "on"; the terminal check still decides.
        os.environ["SECTAPE_COLOR"] = "mauve"
        self.assertTrue(colour_ok(FakeStream(tty=True)))
        self.assertFalse(colour_ok(FakeStream(tty=False)))

    def test_style_for_carries_the_forced_choice(self):
        os.environ["SECTAPE_COLOR"] = "always"
        self.assertTrue(style_for(FakeStream(tty=False)).colour)

    def test_a_stream_that_cannot_answer_gets_no_colour(self):
        class Broken:
            def isatty(self):
                raise OSError("closed")
        self.assertFalse(colour_ok(Broken()))


class TestStyle(unittest.TestCase):
    def test_paint_adds_nothing_without_colour(self):
        plain = Style(colour=False)
        self.assertEqual(plain.red("x"), "x")
        self.assertEqual(plain.bold("x"), "x")

    def test_paint_wraps_and_resets_with_colour(self):
        painted = Style(colour=True).red("x")
        self.assertTrue(painted.startswith(ESC))
        self.assertTrue(painted.endswith("\033[0m"))
        self.assertIn("x", painted)

    def test_ascii_glyphs_when_unicode_is_unavailable(self):
        ascii_style = Style(colour=False, unicode_=False)
        for name in ("rec", "stop", "pause", "arrow", "tick", "cross"):
            glyph = ascii_style.g(name)
            self.assertTrue(glyph.isascii(), f"{name} -> {glyph!r}")

    def test_unicode_glyphs_when_available(self):
        self.assertEqual(Style(colour=False, unicode_=True).g("rec"), "⏺")

    def test_an_unknown_glyph_is_empty_not_an_error(self):
        self.assertEqual(Style(colour=False).g("nonesuch"), "")

    def test_the_deck_is_two_lines(self):
        lines = Style(colour=False).deck("rec", "a label")
        self.assertEqual(len(lines), 2)
        self.assertIn("a label", lines[0])


class TestUnicodeDetection(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None)
                      for k in ("LC_ALL", "LC_CTYPE", "LANG")}
        self.stdout = sys.stdout
        self.addCleanup(self._restore)

    def _restore(self):
        sys.stdout = self.stdout
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_a_utf8_stream_is_enough(self):
        sys.stdout = FakeStream(tty=True, encoding="UTF-8")
        self.assertTrue(unicode_ok())

    def test_a_utf8_locale_is_enough(self):
        sys.stdout = FakeStream(tty=True, encoding="ascii")
        os.environ["LANG"] = "en_GB.UTF-8"
        self.assertTrue(unicode_ok())

    def test_neither_falls_back_to_ascii(self):
        sys.stdout = FakeStream(tty=True, encoding="ascii")
        self.assertFalse(unicode_ok())


class TestFit(unittest.TestCase):
    def test_pads_and_truncates_by_screen_column(self):
        self.assertEqual(fit("abc", 5), "abc  ")
        self.assertEqual(display_width(fit("日本語です", 7)), 7)

    def test_never_splits_a_wide_character(self):
        self.assertEqual(fit("日本", 3), "日 ")


class TestNoEscapesReachAPipe(TempConfig):
    """The end-to-end guarantee: piped output is plain text."""

    def setUp(self):
        super().setUp()
        (self.sessions / "demo").mkdir(parents=True, exist_ok=True)
        (self.sessions / "demo" / "pane_01.raw").write_text("", encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "SECTAPE_STATE_DIR": str(self.root / "state"),
            "SECTAPE_OUTPUT_DIR": str(self.root / "out"),
            "SECTAPE_CONFIG": str(self.root / "none.toml"),
        })
        self.env.pop("NO_COLOR", None)
        self.env.pop("SECTAPE_COLOR", None)

    def test_no_command_writes_an_escape_when_piped(self):
        for argv in (["list"], ["status"], ["doctor"], ["config", "show"],
                     ["show", "demo"], ["list", "--json"], ["status", "--json"]):
            result = subprocess.run([sys.executable, "-m", "sectape", *argv],
                                    capture_output=True, text=True,
                                    env=self.env, timeout=90)
            self.assertNotIn(ESC, result.stdout, f"{argv} stdout")
            self.assertNotIn(ESC, result.stderr, f"{argv} stderr")


if __name__ == "__main__":
    unittest.main()
