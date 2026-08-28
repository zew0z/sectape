"""Filesystem and formatting helpers.

Small functions, but they decide where a recording lives, what its export is
called, and whether a half-written file can ever be left behind.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sectape.util import (human_duration, load_json, one_line, pid_alive,
                          plural,
                          safe_filename, short_path, slugify, squash,
                          write_json_atomic, write_text_atomic)


class TestSlugify(unittest.TestCase):
    def test_ordinary_labels(self):
        self.assertEqual(slugify("Cert Renewal"), "cert_renewal")
        self.assertEqual(slugify("a  b"), "a_b")
        self.assertEqual(slugify("-lead-"), "lead")

    def test_never_traverses_or_hides(self):
        for text in ("../../etc", "..", "/etc/passwd", ".hidden"):
            slug = slugify(text)
            self.assertNotIn("/", slug)
            self.assertNotIn("..", slug)
            self.assertFalse(slug.startswith("."), slug)

    def test_length_is_bounded(self):
        self.assertLessEqual(len(slugify("x" * 500)), 80)

    def test_labels_in_another_script_stay_distinct(self):
        # Nothing ASCII survives, and the fallback used to be the single fixed
        # name `room` - so every such recording shared one directory and one
        # export.
        labels = ["日本語のセッション", "別のセッション", "Привет мир",
                  "데이터 수집", "🚀🚀🚀"]
        slugs = [slugify(text) for text in labels]
        self.assertEqual(len(set(slugs)), len(labels), slugs)
        for slug in slugs:
            self.assertRegex(slug, r"^session-[0-9a-f]{10}$")

    def test_the_same_label_always_gives_the_same_slug(self):
        self.assertEqual(slugify("日本語"), slugify("日本語"))

    def test_an_empty_label_is_named_plainly(self):
        for text in ("", "   ", None):
            self.assertEqual(slugify(text), "session")


class TestOneLine(unittest.TestCase):
    """A label is a heading, a filename and a row in a listing at once."""

    def test_newlines_become_spaces(self):
        self.assertEqual(one_line("first\nsecond"), "first second")

    def test_tabs_and_runs_collapse(self):
        self.assertEqual(one_line("a\tb   c"), "a b c")

    def test_surrounding_space_is_dropped(self):
        self.assertEqual(one_line("  spaced  "), "spaced")

    def test_empty_and_none(self):
        for value in ("", "   ", "\n\t", None):
            self.assertEqual(one_line(value), "")

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(one_line("cert renewal"), "cert renewal")


class TestSafeFilename(unittest.TestCase):
    def test_stays_one_path_component(self):
        for text in ("../../etc/passwd", "a/b\\c", "x\x00y", "tab\there"):
            name = safe_filename(text)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn("\x00", name)

    def test_empty_and_dot_names_get_a_fallback(self):
        for text in ("", "   ", ".", ".."):
            self.assertEqual(safe_filename(text), "untitled")

    def test_a_readable_name_is_left_alone(self):
        self.assertEqual(safe_filename("cert renewal"), "cert renewal")
        self.assertEqual(safe_filename("日本語のセッション"), "日本語のセッション")

    def test_length_is_bounded(self):
        self.assertLessEqual(len(safe_filename("y" * 400)), 120)


class TestFormatting(unittest.TestCase):
    def test_human_duration(self):
        cases = {0: "0ms", 0.5: "500ms", 1: "1.0s", 59.9: "59.9s",
                 60: "1m 0s", 3599: "59m 59s", 3600: "1h 0m", 3661: "1h 1m"}
        for seconds, expected in cases.items():
            self.assertEqual(human_duration(seconds), expected, seconds)

    def test_a_negative_duration_is_not_rendered_as_one(self):
        self.assertEqual(human_duration(-5), "0ms")

    def test_plural(self):
        self.assertEqual(plural(1, "command"), "1 command")
        self.assertEqual(plural(0, "command"), "0 commands")
        self.assertEqual(plural(2, "pane"), "2 panes")
        self.assertEqual(plural(2, "entry", "entries"), "2 entries")

    def test_short_path_collapses_home(self):
        home = Path.home()
        self.assertEqual(short_path(home), "~")
        self.assertEqual(short_path(home / "sectape" / "a.md"), "~/sectape/a.md")
        self.assertEqual(short_path("/etc/hosts"), "/etc/hosts")

    def test_short_path_does_not_collapse_a_lookalike(self):
        self.assertEqual(short_path(str(Path.home()) + "-backup"),
                         str(Path.home()) + "-backup")

    def test_squash(self):
        self.assertEqual(squash("Cert Renewal!"), "certrenewal")
        self.assertEqual(squash(None), "")


class TestAtomicWrites(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sectape-atomic-"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def temporaries(self):
        return [p.name for p in self.dir.iterdir() if p.name.startswith(".tmp-")]

    def test_text_round_trip(self):
        path = self.dir / "note.md"
        write_text_atomic(path, "hello\n")
        self.assertEqual(path.read_text(), "hello\n")
        self.assertEqual(self.temporaries(), [])

    def test_json_round_trip(self):
        path = self.dir / "state.json"
        write_json_atomic(path, {"a": 1, "b": "é"})
        self.assertEqual(load_json(path), {"a": 1, "b": "é"})
        self.assertEqual(self.temporaries(), [])

    def test_parent_directories_are_created(self):
        path = self.dir / "deep" / "deeper" / "note.md"
        write_text_atomic(path, "x")
        self.assertTrue(path.exists())

    def test_an_existing_file_is_replaced_whole(self):
        path = self.dir / "note.md"
        write_text_atomic(path, "a much longer first version\n")
        write_text_atomic(path, "short\n")
        self.assertEqual(path.read_text(), "short\n")

    def test_failure_names_the_target_and_leaves_no_scratch_file(self):
        # The error used to name the internal `.tmp-3f9a1c.md` instead.
        locked = self.dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.chmod(0o700))
        target = locked / "note.md"
        with self.assertRaises(OSError) as caught:
            write_text_atomic(target, "x")
        self.assertEqual(caught.exception.filename, str(target))
        locked.chmod(0o700)
        self.assertEqual([p.name for p in locked.iterdir()], [])

    def test_a_failing_replace_names_the_target_and_cleans_up(self):
        # Disk full, or a cross-device rename. The half-written scratch file
        # must not survive and the real file must not be damaged.
        import unittest.mock as mock
        target = self.dir / "note.md"
        write_text_atomic(target, "original\n")
        with mock.patch("sectape.util.os.replace",
                        side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError) as caught:
                write_text_atomic(target, "replacement\n")
        self.assertEqual(caught.exception.filename, str(target))
        self.assertEqual(caught.exception.errno, 28)
        self.assertEqual(self.temporaries(), [])
        self.assertEqual(target.read_text(), "original\n",
                         "the existing file was damaged by a failed write")

    def test_a_non_oserror_is_re_raised_unchanged(self):
        import unittest.mock as mock

        class Boom(Exception):
            pass

        with mock.patch("sectape.util.os.fdopen", side_effect=Boom("kaboom")):
            with self.assertRaises(Boom):
                write_text_atomic(self.dir / "note.md", "x")
        self.assertEqual(self.temporaries(), [])

    def test_an_interrupt_still_cleans_up(self):
        # `except BaseException` earns its keep here: Ctrl-C during the write
        # must not leave a .tmp- file behind.
        import unittest.mock as mock
        with mock.patch("sectape.util.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                write_text_atomic(self.dir / "note.md", "x")
        self.assertEqual(self.temporaries(), [])

    def test_unserialisable_json_leaves_nothing_behind(self):
        with self.assertRaises(TypeError):
            write_json_atomic(self.dir / "bad.json", {"x": object()})
        self.assertFalse((self.dir / "bad.json").exists())
        self.assertEqual(self.temporaries(), [])


class TestLoadJson(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sectape-json-"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def test_missing_file(self):
        self.assertIsNone(load_json(self.dir / "absent.json"))

    def test_malformed_file(self):
        path = self.dir / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_json(path))

    def test_a_directory(self):
        self.assertIsNone(load_json(self.dir))

    def test_valid_json_that_is_not_an_object(self):
        # `[1, 2, 3]` parses fine, so a state file holding one used to reach
        # callers that immediately do `.get` on it.
        for payload in ("[1, 2, 3]", '"a string"', "42", "null", "true"):
            path = self.dir / "odd.json"
            path.write_text(payload, encoding="utf-8")
            self.assertIsNone(load_json(path), payload)

    def test_an_object_is_returned(self):
        path = self.dir / "good.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(load_json(path), {"a": 1})


class TestPidAlive(unittest.TestCase):
    def test_this_process_is_alive(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_rubbish_is_not(self):
        for value in (None, "", "abc", -1, 0.5):
            self.assertFalse(pid_alive(value), value)

    def test_a_reaped_child_is_not_alive(self):
        import subprocess
        child = subprocess.Popen([shutil.which("true") or "/usr/bin/true"])
        child.wait()
        # The pid may be recycled in principle; in practice this is stable.
        self.assertFalse(pid_alive(child.pid))


if __name__ == "__main__":
    unittest.main()
