import json
import os
import subprocess
import sys
import unittest

from sectape import __version__, config
from sectape.cli import build_parser, main, snapshot
from tests.helpers import TempConfig, begin, end


def run(*argv, env=None, **kw):
    environment = dict(os.environ)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "sectape", *argv],
                          env=environment, capture_output=True, text=True,
                          timeout=90, **kw)


class TestParser(unittest.TestCase):
    def test_every_command_parses(self):
        parser = build_parser()
        for argv in (["rec"], ["rec", "my", "label"], ["start"], ["attach"], ["join"],
                     ["stop"], ["finish"], ["export"], ["export", "x", "-f", "json"],
                     ["show"], ["cat"], ["list"], ["ls"], ["status"],
                     ["rm", "x"], ["config"], ["config", "init"], ["doctor"]):
            self.assertIsNotNone(parser.parse_args(argv).func, argv)

    def test_no_command_prints_help(self):
        self.assertEqual(main([]), 1)

    def test_unknown_format_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["export", "-f", "postscript"])


class TestSnapshot(TempConfig):
    def test_without_session(self):
        snap = snapshot(None)
        self.assertFalse(snap["active"])
        self.assertEqual(snap["version"], __version__)

    def test_with_session(self):
        session_dir = self.make_session("s", begin("ls") + "a\r\n" + end(0))
        snap = snapshot({"label": "S", "slug": "s", "dir": str(session_dir), "panes": {}})
        self.assertTrue(snap["active"])
        self.assertEqual(snap["commands"], 1)
        self.assertEqual(snap["panes"], [])
        json.dumps(snap)


class TestSubprocessSmoke(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.root = Path(tempfile.mkdtemp(prefix="sectape-cli-"))
        self.env = {"SECTAPE_STATE_DIR": str(self.root / "state"),
                    "SECTAPE_OUTPUT_DIR": str(self.root / "out"),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")}
        self.addCleanup(self._clean)

    def _clean(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_version(self):
        r = run("--version", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn(__version__, r.stdout)

    def test_help_lists_commands(self):
        out = run("--help", env=self.env).stdout
        for word in ("rec", "attach", "stop", "export", "show", "doctor"):
            self.assertIn(word, out)

    def test_status_without_session(self):
        r = run("status", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("none active", r.stdout)

    def test_status_json(self):
        r = run("status", "--json", env=self.env)
        self.assertEqual(json.loads(r.stdout)["version"], __version__)

    def test_list_empty(self):
        r = run("list", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("No recordings yet", r.stdout)

    def test_list_json_empty(self):
        self.assertEqual(json.loads(run("list", "--json", env=self.env).stdout), [])

    def test_doctor(self):
        r = run("doctor", env=self.env)
        self.assertIn("doctor", r.stdout)
        self.assertIn(r.returncode, (0, 1))

    def test_stop_without_session(self):
        r = run("stop", env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No active session", r.stdout)

    def test_export_unknown_session(self):
        r = run("export", "nope", env=self.env)
        self.assertEqual(r.returncode, 1)

    def test_config_init_then_show(self):
        cfg = self.root / "cfg" / "config.toml"
        env = dict(self.env, SECTAPE_CONFIG=str(cfg))
        self.assertEqual(run("config", "init", env=env).returncode, 0)
        self.assertTrue(cfg.exists())
        # a second init refuses without --force
        self.assertEqual(run("config", "init", env=env).returncode, 1)
        self.assertEqual(run("config", "init", "--force", env=env).returncode, 0)
        self.assertIn("markdown", run("config", "show", env=env).stdout)

    def test_broken_config_reports_cleanly(self):
        cfg = self.root / "broken.toml"
        cfg.write_text("[general\nnope")
        r = run("status", env=dict(self.env, SECTAPE_CONFIG=str(cfg)))
        self.assertEqual(r.returncode, 2)
        self.assertIn("configuration error", r.stderr)

    def test_note_without_session_fails(self):
        r = run("note", "hello", env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("No active session", r.stdout)

    def test_note_appends_to_the_active_session(self):
        import json as _json
        state = self.root / "state" / "sessions" / "s"
        state.mkdir(parents=True)
        (self.root / "state" / "current.json").write_text(
            _json.dumps({"label": "s", "slug": "s", "dir": str(state)}))
        r = run("note", "remember", "this", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("noted", r.stdout)
        self.assertIn("remember this", (state / "notes.jsonl").read_text())

    def test_note_reads_stdin_when_piped(self):
        import json as _json
        state = self.root / "state" / "sessions" / "s2"
        state.mkdir(parents=True)
        (self.root / "state" / "current.json").write_text(
            _json.dumps({"label": "s2", "slug": "s2", "dir": str(state)}))
        r = run("note", env=self.env, input="from stdin\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("from stdin", (state / "notes.jsonl").read_text())

    def test_completion_scripts_emitted(self):
        for shell in ("zsh", "bash"):
            r = run("completion", shell, env=self.env)
            self.assertEqual(r.returncode, 0)
            self.assertIn("sectape", r.stdout)
            self.assertGreater(len(r.stdout), 200)

    def test_export_filters_reach_the_writer(self):
        import base64 as _b64, json as _json
        state = self.root / "state" / "sessions" / "f"
        state.mkdir(parents=True)

        def b(c, t):
            return f"\x1b]7337;SECTAPE;b|{_b64.b64encode(c.encode()).decode()}|{t}\x07"

        def e(code, t):
            return (f"\x1b]7337;SECTAPE;e|{code}|{t}|"
                    f"{_b64.b64encode(b'/x').decode()}\x07")

        (state / "pane_1.raw").write_text(
            b("good", 1.0) + "fine\r\n" + e(0, 1.1)
            + b("bad", 2.0) + "broken\r\n" + e(1, 2.1))
        out = self.root / "only-failed.json"
        r = run("export", "f", "-f", "json", "-o", str(out), "--only-failed",
                env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = _json.loads(out.read_text())
        self.assertEqual([s["cmd"] for s in payload["steps"]], ["bad"])

    def test_rm_requires_confirmation(self):
        session = self.root / "state" / "sessions" / "demo"
        session.mkdir(parents=True)
        (session / "pane_1.raw").write_text("x")
        r = run("rm", "demo", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Re-run with --yes", r.stdout)
        self.assertTrue(session.exists())
        self.assertEqual(run("rm", "demo", "--yes", env=self.env).returncode, 0)
        self.assertFalse(session.exists())


if __name__ == "__main__":
    unittest.main()
