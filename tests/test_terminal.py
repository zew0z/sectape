import os
import unittest
from pathlib import Path

from sectape.terminal import get_winsize, render, set_winsize


class TestRendering(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(render("hello\r\nworld\r\n"), "hello\nworld\n")

    def test_backspace_erases(self):
        self.assertEqual(render("thm\b\b\bcat"), "cat")

    def test_backspace_space_backspace(self):
        self.assertEqual(render("abc\b \b"), "ab")

    def test_backspace_does_not_eat_previous_line(self):
        self.assertEqual(render("ab\r\n\bX"), "ab\nX")

    def test_carriage_return_overwrites(self):
        self.assertEqual(render("12345\rab"), "ab345")

    def test_progress_bar_collapses_to_final_state(self):
        raw = "".join(f"\r[{'#' * i}{'.' * (4 - i)}] {i * 25}%" for i in range(5))
        self.assertEqual(render(raw), "[####] 100%")

    def test_erase_to_end_of_line(self):
        self.assertEqual(render("abcdef\r\x1b[3Cxx\x1b[K"), "abcxx")

    def test_erase_whole_line(self):
        self.assertEqual(render("garbage\x1b[2Kdone"), "       done")

    def test_cursor_forward_and_back(self):
        self.assertEqual(render("abcdef\x1b[3DXYZ"), "abcXYZ")

    def test_column_absolute(self):
        self.assertEqual(render("abcdef\x1b[3GZ"), "abZdef")

    def test_sgr_colours_dropped(self):
        self.assertEqual(render("\x1b[38;2;243;139;168mzero\x1b[0m ok"), "zero ok")

    def test_osc_title_dropped(self):
        self.assertEqual(render("\x1b]0;my title\x07ok"), "ok")

    def test_osc_with_st_terminator(self):
        self.assertEqual(render("\x1b]2;t\x1b\\ok"), "ok")

    def test_bracketed_paste_dropped(self):
        self.assertEqual(render("\x1b[?2004hls\x1b[?2004l"), "ls")

    def test_alt_screen_content_discarded(self):
        self.assertEqual(render("before\r\n\x1b[?1049hVIM JUNK\x1b[?1049lafter"),
                         "before\nafter")

    def test_clear_keeps_transcript(self):
        out = render("first\r\n\x1b[H\x1b[2Jsecond")
        self.assertIn("first", out)
        self.assertIn("second", out)

    def test_tab_advances_to_stop(self):
        self.assertEqual(render("ab\tc"), "ab      c")

    def test_truncated_escape_does_not_hang(self):
        self.assertEqual(render("ok\x1b["), "ok")
        self.assertEqual(render("ok\x1b]unterminated"), "ok")

    def test_charset_designation_consumed(self):
        self.assertEqual(render("\x1b(Bhi"), "hi")

    def test_null_and_bell_ignored(self):
        self.assertEqual(render("a\x00b\x07c"), "abc")


class TestWrapping(unittest.TestCase):
    def test_wrapped_line_rejoined(self):
        text = "A" * 200
        self.assertEqual(render(text, width=80), text)

    def test_exact_width_line_not_joined(self):
        raw = "A" * 80 + "\r\n" + "BBB"
        self.assertEqual(render(raw, width=80), "A" * 80 + "\nBBB")

    def test_cursor_forward_clamped_to_width(self):
        out = render("x\x1b[200CY", width=80)
        self.assertEqual(len(out.split("\n")[0]), 80)

    def test_wrapping_follows_the_configured_width(self):
        raw = "Z" * 150
        self.assertEqual(render(raw, width=200), raw)


class TestWinsize(unittest.TestCase):
    def test_roundtrip(self):
        import pty
        master, slave = pty.openpty()
        try:
            set_winsize(master, 42, 173)
            self.assertEqual(get_winsize(slave)[:2], (42, 173))
        finally:
            os.close(master)
            os.close(slave)

    def test_unsized_pty_falls_back(self):
        import pty
        master, slave = pty.openpty()
        try:
            set_winsize(master, 0, 0)
            self.assertEqual(get_winsize(slave)[:2], (24, 80))
        finally:
            os.close(master)
            os.close(slave)

    def test_bad_fd_does_not_raise(self):
        self.assertEqual(get_winsize(-1)[:2], (24, 80))
        set_winsize(-1, 10, 10)


if __name__ == "__main__":
    unittest.main()
