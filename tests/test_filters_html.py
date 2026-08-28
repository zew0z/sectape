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
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("<script", out)

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


if __name__ == "__main__":
    unittest.main()
