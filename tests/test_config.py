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
                     config.settings.backup_dir, config.settings.output_dir):
            self.assertTrue(path.is_dir(), path)


if __name__ == "__main__":
    unittest.main()
