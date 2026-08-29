import pathlib
import re
import unittest

from sectape import config
from sectape.formats import Recording, filter_steps, render, to_html
from sectape.transcript import Step
from tests.helpers import TempConfig

OK1 = Step(cmd="ls -la", output="total 4", exit_code=0, started=1.0)
BAD = Step(cmd="cat missing", output="No such file", exit_code=1, started=2.0)
OK2 = Step(cmd="grep foo bar.txt", output="foo=1", exit_code=0, started=3.0)
ALL = [OK1, BAD, OK2]


class TestFilters(TempConfig):
    def test_no_filters_is_a_passthrough(self):
        self.assertEqual(filter_steps(ALL), ALL)

    def test_only_failed(self):
        self.assertEqual([s.cmd for s in filter_steps(ALL, only_failed=True)],
                         ["cat missing"])

    def test_last_n(self):
        self.assertEqual([s.cmd for s in filter_steps(ALL, last=2)],
                         ["cat missing", "grep foo bar.txt"])

    def test_last_larger_than_the_transcript(self):
        self.assertEqual(len(filter_steps(ALL, last=99)), 3)

    def test_last_must_be_positive(self):
        with self.assertRaises(config.ConfigError):
            filter_steps(ALL, last=0)

    def test_grep_matches_command_or_output(self):
        self.assertEqual([s.cmd for s in filter_steps(ALL, grep="grep")],
                         ["grep foo bar.txt"])
        self.assertEqual([s.cmd for s in filter_steps(ALL, grep="No such")],
                         ["cat missing"])

    def test_grep_is_case_insensitive(self):
        self.assertEqual(len(filter_steps(ALL, grep="TOTAL")), 1)

    def test_bad_grep_reports_cleanly(self):
        with self.assertRaises(config.ConfigError):
            filter_steps(ALL, grep="(unclosed")

    def test_drop_output_leaves_originals_untouched(self):
        stripped = filter_steps(ALL, drop_output=True)
        self.assertEqual([s.output for s in stripped], ["", "", ""])
        self.assertEqual(OK1.output, "total 4", "the source step was mutated")

    def test_filters_compose(self):
        result = filter_steps(ALL, grep="a", last=1)
        self.assertEqual(len(result), 1)


class TestHtml(TempConfig):
    def rec(self, steps=None, **kw):
        return Recording("my session", steps if steps is not None else ALL, 1, **kw)

    def test_self_contained_document(self):
        out = to_html(self.rec())
        self.assertTrue(out.startswith("<!doctype html>"))
        self.assertIn("<style>", out)
        # No external resources of any kind - the page must work offline.
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("src=", out)
        self.assertNotIn("@import", out)
        self.assertNotIn("url(", out)

    def test_script_is_inline_and_small(self):
        out = to_html(self.rec())
        self.assertIn("<script>", out)
        script = out.split("<script>")[1].split("</script>")[0]
        self.assertIn("localStorage", script)
        self.assertNotIn("src=", out.split("<script>")[0].split("<body>")[-1])

    def test_toggles_are_present(self):
        out = to_html(self.rec())
        self.assertIn('data-toggle="wrap"', out)
        self.assertIn('data-toggle="failed"', out)
        self.assertIn('aria-pressed="false"', out)

    def test_stats_strip(self):
        out = to_html(self.rec())
        self.assertIn('class="stats"', out)
        self.assertIn(">commands<", out)
        self.assertIn(">failed<", out)

    def test_pane_breaks_only_for_multi_pane(self):
        steps = [Step(cmd="a", started=1.0, pane="01"),
                 Step(cmd="b", started=2.0, pane="02")]
        # the class name also appears in the stylesheet, so match the element
        self.assertIn('class="pane-break"', to_html(Recording("r", steps, panes=2)))
        self.assertNotIn('class="pane-break"', to_html(Recording("r", steps, panes=1)))

    def test_notes_render_as_tape_labels(self):
        rec = Recording("r", [], 1, notes=[{"at": 1.0, "text": "hand written"}])
        out = to_html(rec)
        self.assertIn('class="entry note"', out)
        self.assertIn("hand written", out)

    def test_title_and_commands_present(self):
        out = to_html(self.rec())
        self.assertIn("<title>my session</title>", out)
        self.assertIn("ls -la", out)
        self.assertIn("total 4", out)

    def test_failed_command_marked(self):
        out = to_html(self.rec())
        self.assertIn("step failed", out)
        self.assertIn("<b>exit 1</b>", out)

    def test_html_in_output_is_escaped(self):
        step = Step(cmd="echo '<img onerror=x>'", output="<b>not bold</b>", exit_code=0)
        out = to_html(self.rec([step]))
        self.assertNotIn("<b>not bold</b>", out)
        self.assertIn("&lt;b&gt;not bold&lt;/b&gt;", out)
        self.assertNotIn("<img onerror", out)

    def test_empty_recording(self):
        self.assertIn("No commands were captured", to_html(self.rec([])))

    def test_registered_as_a_format(self):
        _, suffix = render(self.rec(), "html")
        self.assertEqual(suffix, ".html")

    def test_dark_mode_rule_present(self):
        self.assertIn("prefers-color-scheme: dark", to_html(self.rec()))

    def test_tags_are_balanced_enough_to_parse(self):
        from html.parser import HTMLParser

        class Checker(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.bad = []

            def handle_starttag(self, tag, attrs):
                if tag not in ("meta", "br", "img", "link", "input"):
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
                else:
                    self.bad.append(tag)

        checker = Checker()
        checker.feed(to_html(self.rec()))
        self.assertEqual(checker.bad, [])
        self.assertEqual(checker.stack, [])


class TestCustomRedaction(TempConfig):
    def test_custom_pattern_applied(self):
        from sectape.text import redact
        config.override(redact_patterns=(r"CORP-\d{6}",))
        self.assertEqual(redact("ticket CORP-123456 filed"), "ticket <REDACTED> filed")

    def test_custom_replacement(self):
        from sectape.text import redact
        config.override(redact_patterns=(r"secretword",), redact_replacement="***")
        self.assertEqual(redact("say secretword now"), "say *** now")

    def test_builtins_still_apply(self):
        from sectape.text import redact
        config.override(redact_patterns=(r"nothing",))
        self.assertIn("<REDACTED>", redact("Authorization: Bearer abc.def"))

    def test_invalid_pattern_rejected_at_load(self):
        path = self.root / "c.toml"
        path.write_text('[redaction]\npatterns = ["(unclosed"]\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_patterns_read_from_file(self):
        path = self.root / "c2.toml"
        path.write_text('[redaction]\npatterns = ["hunter2"]\nreplacement = "[gone]"\n')
        settings = config.load(path)
        self.assertEqual(settings.redact_patterns, ("hunter2",))
        self.assertEqual(settings.redact_replacement, "[gone]")


class TestCompletion(unittest.TestCase):
    def test_scripts_mention_every_command(self):
        from sectape.cli import BASH_COMPLETION, ZSH_COMPLETION, build_parser
        commands = set()
        for action in build_parser()._subparsers._group_actions[0].choices:
            commands.add(action)
        for script in (BASH_COMPLETION, ZSH_COMPLETION):
            for name in ("rec", "attach", "stop", "note", "export", "show",
                         "list", "status", "rm", "config", "doctor"):
                self.assertIn(name, script)


class TestFilteredExportKeepsItsOwnFile(TempConfig):
    """A filtered export is a subset, not a replacement.

    Writing it to the recording's own file replaced the complete document
    with it - four commands down to one, silently.
    """

    def setUp(self):
        super().setUp()
        import base64
        session = self.sessions / "lab"
        session.mkdir(parents=True, exist_ok=True)
        b64 = lambda t: base64.b64encode(t.encode()).decode()
        raw = ""
        for i, (cmd, code) in enumerate([("nmap -sV host", 0), ("gobuster dir", 0),
                                         ("hydra ssh", 1), ("ssh user@box", 0)]):
            raw += f"\x1b]7337;SECTAPE;b|{b64(cmd)}|{1700000000 + i}\x07out\r\n"
            raw += (f"\x1b]7337;SECTAPE;e|{code}|{1700000000 + i}.5"
                    f"|{b64('/tmp')}\x07")
        (session / "pane_01.raw").write_text(raw, encoding="utf-8")
        self.out = config.settings.output_dir

    def run_export(self, *argv):
        from sectape.cli import cmd_export
        from sectape.cli import build_parser
        import contextlib
        import io
        args = build_parser().parse_args(["export", "lab", *argv])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cmd_export(args), 0)
        return buffer.getvalue().strip()

    def steps_in(self, path):
        return sum(1 for line in pathlib.Path(path).read_text().split("\n")
                   if line.startswith("### "))

    def test_the_whole_recording_goes_to_the_plain_name(self):
        path = self.run_export()
        self.assertEqual(pathlib.Path(path).name, "lab.md")
        self.assertEqual(self.steps_in(path), 4)

    def test_a_filtered_export_does_not_overwrite_it(self):
        whole = self.run_export()
        filtered = self.run_export("--only-failed")
        self.assertNotEqual(whole, filtered)
        self.assertEqual(pathlib.Path(filtered).name, "lab (failed).md")
        self.assertEqual(self.steps_in(filtered), 1)
        self.assertEqual(self.steps_in(whole), 4, "the full export was gutted")

    def test_each_filter_names_itself(self):
        self.assertEqual(pathlib.Path(self.run_export("--last", "2")).name,
                         "lab (last 2).md")
        self.assertEqual(
            pathlib.Path(self.run_export("--grep", "nmap", "--no-output")).name,
            "lab (filtered, commands).md")

    def test_dash_o_still_decides(self):
        target = self.root / "mine.md"
        path = self.run_export("--only-failed", "-o", str(target))
        self.assertEqual(pathlib.Path(path), target)

    def test_the_extension_follows_the_format(self):
        self.assertEqual(
            pathlib.Path(self.run_export("--only-failed", "-f", "html")).name,
            "lab (failed).html")


class TestFilteredNotes(TempConfig):
    """A filtered document is about a subset; its notes should be too.

    Keeping every note from the whole session buried `--last 2` under ten
    annotations that had nothing to do with the two commands asked for.
    """

    def fresh(self):
        steps = [Step(cmd=f"command-{i}", output="out",
                      exit_code=0 if i % 4 else 1,
                      started=100.0 + i * 10, duration=1.0, source="marker")
                 for i in range(12)]
        notes = [{"at": 105.0 + i * 10, "text": f"note-{i}"} for i in range(12)]
        return Recording(label="x", steps=steps, panes=1, notes=notes)

    def filtered(self, **kw):
        import argparse
        from sectape.cli import _apply_filters
        return _apply_filters(self.fresh(), argparse.Namespace(**kw))

    def test_an_unfiltered_recording_keeps_every_note(self):
        rec = self.filtered()
        self.assertEqual(len(rec.steps), 12)
        self.assertEqual(len(rec.notes), 12)

    def test_last_n_keeps_the_notes_belonging_to_those_commands(self):
        # A note belongs to the last command that had started when it was
        # written, which is the rule the timeline places it by.
        rec = self.filtered(last=2)
        self.assertEqual([s.cmd for s in rec.steps], ["command-10", "command-11"])
        self.assertEqual([n["text"] for n in rec.notes], ["note-10", "note-11"])

    def test_one_command_keeps_its_own_note(self):
        # Written just after the command, which is when most notes are.
        rec = self.filtered(last=1)
        self.assertEqual([s.cmd for s in rec.steps], ["command-11"])
        self.assertEqual([n["text"] for n in rec.notes], ["note-11"])

    def test_every_note_kept_belongs_to_a_kept_command(self):
        rec = self.filtered(only_failed=True)
        starts = sorted(s.started for s in rec.steps)
        for note in rec.notes:
            owner = max((t for t in starts if t <= note["at"]), default=None)
            self.assertIsNotNone(owner, note)

    def test_a_note_before_the_first_command_is_not_attached(self):
        import argparse
        from sectape.cli import _apply_filters
        steps = [Step(cmd="c0", started=200.0, duration=1.0, source="marker"),
                 Step(cmd="c1", started=300.0, duration=1.0, source="marker")]
        notes = [{"at": 100.0, "text": "before everything"}]
        rec = Recording(label="x", steps=steps, panes=1, notes=notes)
        rec = _apply_filters(rec, argparse.Namespace(last=1))
        self.assertEqual(rec.notes, [])

    def test_matching_nothing_keeps_nothing(self):
        rec = self.filtered(grep="no-such-command")
        self.assertEqual(rec.steps, [])
        self.assertEqual(rec.notes, [])

    def test_a_capture_with_no_timestamps_keeps_its_notes(self):
        # Nothing to compare against, so narrowing them would be guesswork.
        import argparse
        from sectape.cli import _apply_filters
        steps = [Step(cmd=f"c{i}") for i in range(4)]
        notes = [{"at": 100.0 + i, "text": f"n{i}"} for i in range(4)]
        rec = Recording(label="x", steps=steps, panes=1, notes=notes)
        rec = _apply_filters(rec, argparse.Namespace(last=2))
        self.assertEqual(len(rec.steps), 2)
        self.assertEqual(len(rec.notes), 4)

    def test_notes_still_read_in_order_around_the_kept_commands(self):
        rec = self.filtered(last=3)
        kinds = "".join("S" if kind == "step" else "n"
                        for kind, _ in rec.timeline())
        self.assertEqual(kinds.count("S"), 3)
        self.assertLessEqual(kinds.count("n"), 3)


class TestHtmlPaneAttribution(TempConfig):
    """The failed-only view hides pane breaks, which is exactly when you are
    comparing what went wrong across tabs."""

    STEPS = [Step(cmd="deploy", exit_code=1, started=100.0, pane="01",
                  source="marker"),
             Step(cmd="rollback", exit_code=1, started=102.0, pane="02",
                  source="marker")]

    def metas(self, page):
        import re as _re
        return [" ".join(_re.sub(r"<[^>]+>", " ", m.group(1)).split())
                for m in _re.finditer(r'<span class="meta">(.*?)</span></header>',
                                      page)]

    def test_each_step_names_its_pane(self):
        page = to_html(Recording(label="x", steps=self.STEPS, panes=2))
        self.assertTrue(any("pane 1" in m for m in self.metas(page)))
        self.assertTrue(any("pane 2" in m for m in self.metas(page)))

    def test_a_single_pane_session_says_nothing_about_panes(self):
        page = to_html(Recording(label="x", steps=self.STEPS[:1], panes=1))
        self.assertNotIn("pane", "".join(self.metas(page)))

    def test_the_attribution_survives_the_pane_breaks_being_hidden(self):
        # The breaks are display:none in that view, so the step itself has to
        # carry it.
        page = to_html(Recording(label="x", steps=self.STEPS, panes=2))
        self.assertIn("body.failed-only .pane-break", page)
        for meta in self.metas(page):
            self.assertIn("pane", meta)

    def test_it_matches_what_markdown_says(self):
        from sectape.formats import to_markdown
        recording = Recording(label="x", steps=self.STEPS, panes=2)
        markdown = to_markdown(recording)
        page = to_html(recording)
        for pane in ("pane 1", "pane 2"):
            self.assertIn(pane, markdown)
            self.assertIn(pane, page)


class TestHtmlWorksWithoutStorage(TempConfig):
    """An exported page is opened from disk, where localStorage is refused.

    Chrome throws SecurityError for `file://` and `data:` documents. The
    toggles have to keep working; only remembering the choice may be lost.
    """

    def script(self) -> str:
        page = to_html(Recording(label="x", panes=1, steps=[
            Step(cmd="echo hi", exit_code=0, started=1.0, source="marker")]))
        return page.split("<script>")[1].split("</script>")[0]

    def test_every_storage_read_is_guarded(self):
        script = self.script()
        self.assertIn("getItem", script)
        for call in ("localStorage.getItem", "localStorage.setItem"):
            index = script.index(call)
            before = script[max(0, index - 120):index]
            self.assertIn("try", before,
                          f"{call} is not inside a try block")

    def test_a_failed_read_does_not_leave_the_toggle_undefined(self):
        # `load` has to return something falsy rather than propagate.
        script = self.script()
        self.assertRegex(script, r"catch\s*\([^)]*\)\s*\{\s*return false")

    def test_the_toggles_are_wired_by_attribute(self):
        page = to_html(Recording(label="x", panes=1, steps=[
            Step(cmd="echo hi", exit_code=0, started=1.0, source="marker")]))
        self.assertIn('data-toggle="wrap"', page)
        self.assertIn('data-toggle="failed"', page)
        self.assertIn("[data-toggle]", self.script())

    def test_the_page_needs_nothing_from_the_network(self):
        # It is handed to people as a single file.
        page = to_html(Recording(label="x", panes=1, steps=[
            Step(cmd="echo hi", exit_code=0, started=1.0, source="marker")]))
        for pattern in ("<script src=", "<link ", "@import", "http://", "https://"):
            self.assertNotIn(pattern, page, pattern)


if __name__ == "__main__":
    unittest.main()
