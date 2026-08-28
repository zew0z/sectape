import os
import unittest
from pathlib import Path

from sectape import config
from tests.helpers import TempConfig


class TestConfigFile(TempConfig):
    def write(self, text: str) -> Path:
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_defaults_when_missing(self):
        s = config.load(self.root / "absent.toml")
        self.assertEqual(s.format, "markdown")
        self.assertTrue(s.redact)
        self.assertIsNone(s.config_path)

    def test_values_are_read(self):
        path = self.write('''
[general]
state_dir = "~/somewhere"
prompt = "» "
redact = false
shell_integration = false

[output]
dir = "~/exports"
format = "json"
max_output_lines = 42
''')
        s = config.load(path)
        self.assertEqual(s.state_dir, Path.home() / "somewhere")
        self.assertEqual(s.prompt, "» ")
        self.assertFalse(s.redact)
        self.assertFalse(s.shell_integration)
        self.assertEqual(s.output_dir, Path.home() / "exports")
        self.assertEqual(s.format, "json")
        self.assertEqual(s.max_output_lines, 42)
        self.assertEqual(s.config_path, path)

    def test_invalid_toml_is_reported(self):
        path = self.write("[general\nbroken")
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_invalid_format_is_rejected(self):
        path = self.write('[output]\nformat = "postscript"\n')
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_environment_beats_the_file(self):
        path = self.write('[output]\nformat = "json"\n')
        os.environ["SECTAPE_FORMAT"] = "text"
        try:
            self.assertEqual(config.load(path).format, "text")
        finally:
            del os.environ["SECTAPE_FORMAT"]

    def test_explicit_overrides_beat_everything(self):
        path = self.write('[output]\nformat = "json"\n')
        os.environ["SECTAPE_FORMAT"] = "text"
        try:
            self.assertEqual(config.load(path, format="markdown").format, "markdown")
        finally:
            del os.environ["SECTAPE_FORMAT"]

    def test_boolean_env_parsing(self):
        for value, expected in (("0", False), ("false", False), ("no", False),
                                ("off", False), ("1", True), ("true", True)):
            os.environ["SECTAPE_REDACT"] = value
            try:
                self.assertIs(config.load(self.root / "absent.toml").redact, expected)
            finally:
                del os.environ["SECTAPE_REDACT"]

    def test_bad_env_port_is_reported(self):
        os.environ["SECTAPE_STATE_DIR"] = str(self.root / "st")
        try:
            self.assertEqual(config.load(self.root / "absent.toml").state_dir,
                             self.root / "st")
        finally:
            del os.environ["SECTAPE_STATE_DIR"]

    def test_derived_paths(self):
        s = config.Settings(state_dir=Path("/tmp/x"))
        self.assertEqual(s.sessions_dir, Path("/tmp/x/sessions"))
        self.assertEqual(s.current_session_file, Path("/tmp/x/current.json"))
        self.assertEqual(s.lock_file, Path("/tmp/x/.lock"))

    def test_template_is_valid_toml_and_round_trips(self):
        import tomllib
        parsed = tomllib.loads(config.TEMPLATE)
        self.assertIn("general", parsed)
        self.assertIn("output", parsed)
        path = self.write(config.TEMPLATE)
        s = config.load(path)
        self.assertEqual(s.format, "markdown")

    def test_ensure_dirs_creates_everything(self):
        config.override(state_dir=self.root / "fresh", output_dir=self.root / "freshout")
        config.ensure_dirs()
        for path in (config.settings.state_dir, config.settings.sessions_dir,
                     config.settings.output_dir):
            self.assertTrue(path.is_dir(), path)

    def test_nothing_unused_is_created(self):
        # `backups/` was created in every state directory and never written
        # to, so every user had an empty directory for a feature that does
        # not exist.
        config.override(state_dir=self.root / "tidy", output_dir=self.root / "tidyout")
        config.ensure_dirs()
        made = {p.name for p in config.settings.state_dir.iterdir() if p.is_dir()}
        self.assertEqual(made, {"sessions"})


class TestMalformedValuesAreRejected(TempConfig):
    """A typo in the config must say so, not corrupt an export or traceback."""

    def write(self, text: str) -> Path:
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def assert_rejected(self, toml: str, needle: str):
        with self.assertRaises(config.ConfigError) as caught:
            config.load(self.write(toml))
        self.assertIn(needle, str(caught.exception))

    def test_patterns_as_a_bare_string(self):
        # A string is iterable, so this was read as one regex per character:
        # every `o`, `p` and `s` in the export replaced with <REDACTED>.
        self.assert_rejected('[redaction]\npatterns = "oops"\n',
                             "redaction.patterns")

    def test_patterns_holding_a_non_string(self):
        self.assert_rejected('[redaction]\npatterns = [1, 2]\n',
                             "redaction.patterns")

    def test_a_list_of_patterns_is_accepted(self):
        settings = config.load(
            self.write('[redaction]\npatterns = ["A-[0-9]+", "b"]\n'))
        self.assertEqual(settings.redact_patterns, ("A-[0-9]+", "b"))

    def test_a_limit_that_is_not_a_number(self):
        # This escaped as a raw ValueError and a traceback.
        self.assert_rejected('[output]\nmax_output_lines = "many"\n',
                             "max_output_lines")

    def test_a_fractional_limit(self):
        self.assert_rejected('[output]\nmax_output_lines = 3.7\n',
                             "max_output_lines")

    def test_a_boolean_limit(self):
        self.assert_rejected('[output]\nmax_output_chars = true\n',
                             "max_output_chars")

    def test_a_non_boolean_flag(self):
        # `bool("no")` is True, so this silently meant the opposite.
        self.assert_rejected('[general]\nredact = "no"\n', "general.redact")

    def test_a_path_that_is_not_a_string(self):
        self.assert_rejected('[general]\nstate_dir = 7\n', "general.state_dir")

    def test_a_section_that_is_not_a_table(self):
        self.assert_rejected('general = "nonsense"\n', "[general]")

    def test_the_shipped_template_still_loads(self):
        path = self.root / "template.toml"
        path.write_text(config.TEMPLATE, encoding="utf-8")
        settings = config.load(path)
        self.assertEqual(settings.format, "markdown")
        self.assertEqual(settings.redact_patterns, ())
        self.assertTrue(settings.redact)


class TestUnknownKeysAreReported(TempConfig):
    """A misspelled key is simply not read, which is invisible on its own."""

    def write(self, text: str) -> Path:
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_misspelled_key_is_listed(self):
        path = self.write('[output]\nfromat = "json"\n')
        self.assertEqual(config.unknown_keys(path), ["output.fromat"])

    def test_a_misspelled_section_is_listed(self):
        path = self.write('[genral]\nredact = false\n')
        self.assertEqual(config.unknown_keys(path), ["[genral]"])

    def test_a_correct_file_lists_nothing(self):
        path = self.write('[output]\nformat = "json"\n'
                          '[general]\nredact = false\n')
        self.assertEqual(config.unknown_keys(path), [])

    def test_the_shipped_template_has_no_unknown_keys(self):
        path = self.root / "template.toml"
        path.write_text(config.TEMPLATE, encoding="utf-8")
        self.assertEqual(config.unknown_keys(path), [])

    def test_every_documented_key_is_actually_read(self):
        # KNOWN_KEYS is what `doctor` measures typos against, so it must not
        # drift from what the loader understands.
        path = self.write(
            '[general]\nstate_dir = "~/s"\nprompt = "> "\n'
            'redact = false\nshell_integration = false\n'
            '[output]\ndir = "~/o"\nformat = "text"\n'
            'max_output_lines = 11\nmax_output_chars = 22\n'
            '[redaction]\npatterns = ["x"]\nreplacement = "R"\n')
        self.assertEqual(config.unknown_keys(path), [])
        settings = config.load(path)
        self.assertEqual(settings.prompt, "> ")
        self.assertEqual(settings.format, "text")
        self.assertEqual(settings.max_output_lines, 11)
        self.assertEqual(settings.max_output_chars, 22)
        self.assertEqual(settings.redact_patterns, ("x",))
        self.assertEqual(settings.redact_replacement, "R")
        self.assertFalse(settings.redact)
        self.assertFalse(settings.shell_integration)

    def test_an_unreadable_file_reports_nothing(self):
        self.assertEqual(config.unknown_keys(self.root / "absent.toml"), [])
        self.assertEqual(config.unknown_keys(self.write("[broken\n")), [])


if __name__ == "__main__":
    unittest.main()
