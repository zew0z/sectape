import os
import unittest
from pathlib import Path

from sectape.terminal import (VTScreen, char_width, get_winsize, render,
                              set_winsize)


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


class TestResetSequence(unittest.TestCase):
    """CSI r homes the cursor, so the reset must not leave it there."""

    def test_scroll_region_reset_is_bracketed_by_save_restore(self):
        from sectape.terminal import TERMINAL_RESET
        self.assertTrue(TERMINAL_RESET.startswith("\x1b7"), "no DECSC at the start")
        self.assertTrue(TERMINAL_RESET.endswith("\x1b8"), "no DECRC at the end")
        save = TERMINAL_RESET.index("\x1b7")
        region = TERMINAL_RESET.index("\x1b[r")
        restore = TERMINAL_RESET.rindex("\x1b8")
        self.assertLess(save, region)
        self.assertLess(region, restore)

    def test_reset_still_covers_the_modes_that_break_a_terminal(self):
        from sectape.terminal import TERMINAL_RESET
        for sequence in ("\x1b[?1049l", "\x1b[?2004l", "\x1b[?25h",
                         "\x1b[?7h", "\x1b[m", "\x1b[?1000l"):
            self.assertIn(sequence, TERMINAL_RESET)


class TestCharacterWidth(unittest.TestCase):
    def test_ascii_is_one_column(self):
        for ch in "aZ0 ~":
            self.assertEqual(char_width(ch), 1, ch)

    def test_cjk_is_two_columns(self):
        for ch in "日本語漢가":
            self.assertEqual(char_width(ch), 2, ch)

    def test_emoji_is_two_columns(self):
        self.assertEqual(char_width("\U0001F600"), 2)

    def test_combining_marks_take_no_column(self):
        self.assertEqual(char_width("\u0301"), 0)      # combining acute
        self.assertEqual(char_width("\uFE0F"), 0)      # variation selector
        self.assertEqual(char_width("\u200D"), 0)      # zero-width joiner

    def test_accented_latin_is_one_column(self):
        for ch in "éñüÅ":
            self.assertEqual(char_width(ch), 1, ch)


class TestWideCharacterColumns(unittest.TestCase):
    """The shell computes its cursor moves in real screen columns."""

    def test_absolute_column_lands_after_wide_text(self):
        # 名前 occupies columns 1-4, so CSI 5 G is the column just after it.
        # Counting each character as one column left two stray spaces.
        self.assertEqual(render("名前\x1b[5Gcmd\r\n", 20), "名前cmd\n")

    def test_cursor_forward_is_in_columns_not_characters(self):
        self.assertEqual(render("日\x1b[2Cx\r\n", 20), "日  x\n")

    def test_a_wide_line_still_rejoins_to_one_logical_line(self):
        # 15 double-width characters are 30 columns, so a 20-column terminal
        # wraps them; the rejoin has to put the line back together whole.
        text = "日" * 15
        self.assertEqual(render(text + "\r\nnext\r\n", 20), text + "\nnext\n")

    def test_overwriting_half_a_wide_character_blanks_both_cells(self):
        # Return to column 0 and type one narrow character over 日.
        self.assertEqual(render("日x\rA\r\n", 20), "A x\n")

    def test_combining_mark_stays_with_its_base_character(self):
        self.assertEqual(render("e\u0301tat\r\n", 20), "e\u0301tat\n")

    def test_emoji_prompt_does_not_shift_the_line(self):
        self.assertEqual(render("\U0001F680 \x1b[4Gready\r\n", 20),
                         "\U0001F680 ready\n")


class TestControlCharacters(unittest.TestCase):
    def test_c1_controls_are_not_printed(self):
        # Raw C1 bytes used to be written into the export as text.
        self.assertEqual(render("abc\x9bdef\r\n", 40), "abcdef\n")

    def test_nul_and_bell_are_dropped(self):
        self.assertEqual(render("a\x00b\x07c\r\n", 40), "abc\n")


class TestNarrowTerminals(unittest.TestCase):
    """A split tmux pane is easily narrower than twenty columns."""

    def test_a_narrow_width_is_honoured(self):
        # The width was floored at 20, so a 12-column pane was replayed as if
        # it were 20 and every wrap landed in the wrong place.
        self.assertEqual(VTScreen(12).width, 12)
        self.assertEqual(VTScreen(1).width, 1)

    def test_a_missing_width_still_falls_back_to_the_default(self):
        self.assertEqual(VTScreen(0).width, VTScreen.DEFAULT_WIDTH)
        self.assertEqual(VTScreen(None).width, VTScreen.DEFAULT_WIDTH)

    def test_a_carriage_return_returns_to_the_right_row(self):
        # 15 characters on a 12-column terminal wrap to a second row, so the
        # \r goes to the start of *that* row.
        self.assertEqual(render("abcdefghijklmno\rXY\r\n", 12),
                         "abcdefghijklXYo\n")

    def test_wrapped_rows_still_rejoin(self):
        self.assertEqual(render("abcdefghijklmno\r\n", 12), "abcdefghijklmno\n")

    def test_resize_accepts_a_narrow_width(self):
        screen = VTScreen(80)
        screen.resize(10)
        self.assertEqual(screen.width, 10)

    def test_resize_ignores_nonsense(self):
        screen = VTScreen(80)
        screen.resize(0)
        self.assertEqual(screen.width, 80)

    def test_a_wide_character_on_a_one_column_screen_terminates(self):
        # It cannot fit in one column, so each wraps to a row of its own -
        # and those rows are marked wrapped, so they rejoin into the logical
        # line, which is the whole point of the replay. The test is that it
        # terminates and loses nothing.
        self.assertEqual(render("日本\r\n", 1), "日本\n")

    def test_narrow_widths_lose_no_characters(self):
        text = "abcdefghij"
        for width in (1, 2, 3, 7):
            self.assertEqual(render(text + "\r\n", width), text + "\n", width)


class TestAlternateScreen(unittest.TestCase):
    """Full-screen programs redraw; that noise is discarded, not the session."""

    ON, OFF = "\x1b[?1049h", "\x1b[?1049l"

    def test_a_program_that_opens_and_closes_leaves_no_trace(self):
        raw = "before\r\n" + self.ON + "VIM\r\n~\r\n" + self.OFF + "after\r\n"
        self.assertEqual(render(raw, 40), "before\nafter\n")

    def test_a_capture_that_ends_inside_a_full_screen_program(self):
        # Killed in `less`, or stopped from another tab. The alternate screen
        # is never left, and the whole transcript used to be thrown away in
        # favour of a picture of vim.
        raw = "important work\r\n" + self.ON + "VIM SCREEN\r\n~\r\n~\r\n"
        text = render(raw, 40)
        self.assertIn("important work", text)
        self.assertNotIn("VIM SCREEN", text)

    def test_the_older_47_and_1047_variants_behave_the_same(self):
        for code in ("47", "1047"):
            raw = f"before\r\n\x1b[?{code}h" + "X\r\n"
            self.assertEqual(render(raw, 40), "before\n", code)

    def test_an_unbalanced_close_is_harmless(self):
        self.assertEqual(render("before\r\n" + self.OFF + "after\r\n", 40),
                         "before\nafter\n")

    def test_a_repeated_open_does_not_lose_the_transcript(self):
        raw = "before\r\n" + self.ON + self.ON + "X\r\n" + self.OFF + "after\r\n"
        self.assertEqual(render(raw, 40), "before\nafter\n")

    def test_clearing_inside_the_alternate_screen_is_contained(self):
        raw = "before\r\n" + self.ON + "\x1b[2J" + "X\r\n" + self.OFF + "after\r\n"
        self.assertEqual(render(raw, 40), "before\nafter\n")


def _vt_corpus(seed=7, count=1500):
    """Random byte-soup that looks like terminal output."""
    import random
    import string
    rnd = random.Random(seed)
    esc = "\x1b"
    pieces = [
        lambda: "".join(rnd.choices(string.printable[:94], k=rnd.randint(1, 40))),
        lambda: rnd.choice(["\r", "\n", "\r\n", "\t", "\b", "\x07", "\x00"]),
        lambda: f"{esc}[{rnd.randint(0, 5)}{rnd.choice('ABCDEFGJKLMPX@d')}",
        lambda: f"{esc}[{rnd.randint(1, 30)};{rnd.randint(1, 30)}H",
        lambda: f"{esc}[{rnd.randint(0, 107)}m",
        lambda: f"{esc}[?{rnd.choice([1049, 47, 25, 2004])}{rnd.choice('hl')}",
        lambda: f"{esc}]0;a title\x07",
        lambda: rnd.choice([f"{esc}7", f"{esc}8", f"{esc}M", f"{esc}D", f"{esc}c"]),
        lambda: rnd.choice("日本語漢字가나\U0001F680\U0001F600") * rnd.randint(1, 6),
        lambda: "e\u0301u\u0308",
        lambda: rnd.choice(["\x9b", "\x85", "\x7f"]),
        lambda: f"{esc}({rnd.choice('AB0')}",
    ]
    return ["".join(rnd.choice(pieces)() for _ in range(rnd.randint(1, 25)))
            for _ in range(count)]


class TestReplayerOnRandomInput(unittest.TestCase):
    """Whatever a program emits, the replay has to be safe to put in a file."""

    CORPUS = _vt_corpus()

    def test_never_raises(self):
        for case in self.CORPUS:
            for width in (1, 2, 5, 12, 20, 80, 200):
                try:
                    render(case, width)
                except Exception as exc:            # pragma: no cover
                    self.fail(f"render raised {exc!r} on {case!r:.120}")

    def test_output_carries_no_control_characters(self):
        # Only newlines survive; escape sequences, C0 and C1 do not.
        for case in self.CORPUS:
            text = render(case, 80)
            bad = [ch for ch in text
                   if (ch < " " and ch != "\n") or "\x7f" <= ch <= "\x9f"]
            self.assertEqual(bad, [], f"control characters leaked: {bad!r}")

    def test_is_deterministic(self):
        for case in self.CORPUS[:300]:
            self.assertEqual(render(case, 80), render(case, 80))

    def test_no_row_is_padded_past_the_terminal_width(self):
        # Unwrapped rows are what was on one screen line, so none of them can
        # be wider than the terminal - a widthless replay used to emit
        # enormous space-padded lines.
        for case in self.CORPUS:
            screen = VTScreen(80).feed(case)
            for index, cells in enumerate(screen.lines):
                self.assertLessEqual(
                    len(cells), 80,
                    f"row {index} holds {len(cells)} cells on an 80-column screen")


if __name__ == "__main__":
    unittest.main()
