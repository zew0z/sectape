import json
import os
import pathlib
import re
import shutil
import time
import subprocess
import sys
import unittest

from sectape import __version__, config
from sectape.cli import (_confirm, _finish, build_parser, cmd_list,
                         main, snapshot)
from sectape.ui import display_width, fit
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
        self.assertIn("idle", r.stdout)

    def test_status_json(self):
        r = run("status", "--json", env=self.env)
        self.assertEqual(json.loads(r.stdout)["version"], __version__)

    def test_list_empty(self):
        r = run("list", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no recordings", r.stdout)

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

    def test_list_numbers_sessions_and_show_accepts_the_number(self):
        import base64 as _b64
        sessions = self.root / "state" / "sessions"
        for name, cmd in (("older", "alpha"), ("newer", "beta")):
            d = sessions / name
            d.mkdir(parents=True)
            payload = _b64.b64encode(cmd.encode()).decode()
            (d / "pane_01.raw").write_text(
                f"\x1b]7337;SECTAPE;b|{payload}|1.0\x07out\r\n"
                f"\x1b]7337;SECTAPE;e|0|1.1|{_b64.b64encode(b'/x').decode()}\x07")
            time.sleep(0.05)

        listing = run("list", env=self.env).stdout
        self.assertIn("older", listing)
        self.assertIn("newer", listing)
        self.assertIn(" 1  ", listing)

        rows = json.loads(run("list", "--json", env=self.env).stdout)
        self.assertEqual(rows[0]["index"], 1)
        first = rows[0]["session"]

        by_number = run("show", "1", env=self.env)
        by_name = run("show", first, env=self.env)
        self.assertEqual(by_number.returncode, 0, by_number.stderr)
        self.assertEqual(by_number.stdout, by_name.stdout)

    def test_exported_label_survives_the_slug(self):
        # A session directory is a slug; the document should still carry the
        # label that was typed.
        import base64 as _b64
        d = self.root / "state" / "sessions" / "cert_renewal"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"label": "cert renewal", "slug": "cert_renewal"}))
        (d / "pane_01.raw").write_text(
            f"\x1b]7337;SECTAPE;b|{_b64.b64encode(b'ls').decode()}|1.0\x07x\r\n"
            f"\x1b]7337;SECTAPE;e|0|1.1|{_b64.b64encode(b'/x').decode()}\x07")
        out = self.root / "out"
        r = run("export", "cert_renewal", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((out / "cert renewal.md").exists(),
                        sorted(p.name for p in out.iterdir()))
        self.assertIn("# cert renewal", (out / "cert renewal.md").read_text())

    def test_show_rejects_an_out_of_range_number(self):
        r = run("show", "99", env=self.env)
        self.assertEqual(r.returncode, 1)

    def test_a_named_config_that_is_missing_is_an_error(self):
        # A missing default config is normal. One you typed on the command
        # line is a typo, and silently using the defaults hid it.
        r = run("--config", str(self.root / "typo.toml"), "config", "show",
                env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no such configuration file", r.stderr)

    def test_a_named_config_that_exists_is_used(self):
        path = self.root / "real.toml"
        path.write_text('[output]\nformat = "json"\n', encoding="utf-8")
        r = run("--config", str(path), "config", "show", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("json", r.stdout)

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


    def test_rm_refuses_a_session_that_is_still_recording(self):
        session = self.root / "state" / "sessions" / "live"
        session.mkdir(parents=True)
        (session / "pane_1.raw").write_text("recorded output")
        (self.root / "state" / "current.json").write_text(json.dumps({
            "label": "live", "slug": "live", "dir": str(session),
            "started": time.time(),
            "panes": {"01": {"pid": os.getpid(), "log": "x"}},
        }), encoding="utf-8")
        r = run("rm", "live", "--yes", env=self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("still active", r.stdout)
        self.assertTrue(session.exists(), "a live recording was deleted")

    def test_rm_refuses_it_through_a_symlinked_state_directory(self):
        # current.json stores the directory unresolved; the resolver returns it
        # resolved. Compared as text those disagree the moment any component of
        # the path is a symlink - which on macOS is true of /tmp itself - and
        # the guard above never fired: `sectape rm --yes` deleted the pane logs
        # of a session that was still being written to.
        real = self.root / "real-state"
        (real / "sessions" / "live").mkdir(parents=True)
        (real / "sessions" / "live" / "pane_1.raw").write_text("recorded output")
        link = self.root / "linked-state"
        os.symlink(real, link)
        (real / "current.json").write_text(json.dumps({
            "label": "live", "slug": "live",
            # As cmd_rec writes it: built from the configured state dir, which
            # is the symlink, so it never matches the resolved form.
            "dir": str(link / "sessions" / "live"),
            "started": time.time(),
            "panes": {"01": {"pid": os.getpid(), "log": "x"}},
        }), encoding="utf-8")
        env = dict(self.env)
        env["SECTAPE_STATE_DIR"] = str(link)
        r = run("rm", "live", "--yes", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("still active", r.stdout)
        self.assertTrue((real / "sessions" / "live" / "pane_1.raw").exists(),
                        "a live recording was deleted through a symlink")


class TestPipeClosedEarly(TempConfig):
    """`sectape show | head` is an ordinary thing to type."""

    def setUp(self):
        super().setUp()
        import base64
        session = self.sessions / "big"
        session.mkdir(parents=True, exist_ok=True)
        b64 = lambda t: base64.b64encode(t.encode()).decode()
        raw = ""
        for i in range(400):
            body = "\r\n".join(f"line {j} of command {i}" for j in range(30))
            raw += f"\x1b]7337;SECTAPE;b|{b64('echo cmd' + str(i))}|{1700000000 + i}\x07"
            raw += body + "\r\n"
            raw += (f"\x1b]7337;SECTAPE;e|0|{1700000000 + i}.5"
                    f"|{b64('/tmp')}\x07")
        (session / "pane_01.raw").write_text(raw, encoding="utf-8")
        self.env = {"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                    "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")}

    def show_into_head(self, *extra):
        environment = dict(os.environ)
        environment.update(self.env)
        show = subprocess.Popen(
            [sys.executable, "-m", "sectape", "show", "big", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        head = subprocess.Popen(["head", "-3"], stdin=show.stdout,
                                stdout=subprocess.DEVNULL)
        show.stdout.close()
        head.wait()
        stderr = show.stderr.read().decode()
        show.stderr.close()
        show.wait(timeout=60)
        return show.returncode, stderr

    def test_output_is_larger_than_a_pipe_buffer(self):
        # Otherwise the write never blocks and there is nothing to test.
        environment = dict(os.environ)
        environment.update(self.env)
        result = subprocess.run([sys.executable, "-m", "sectape", "show", "big"],
                                capture_output=True, env=environment, timeout=90)
        self.assertGreater(len(result.stdout), 256 * 1024)

    def test_nothing_is_printed_when_the_reader_goes_away(self):
        # This used to be a traceback, then `error: Broken pipe`. Neither
        # belongs in `sectape show | head`.
        _, stderr = self.show_into_head()
        self.assertEqual(stderr, "")

    def test_the_exit_code_matches_a_sigpipe_death(self):
        code, _ = self.show_into_head()
        self.assertEqual(code, 141)

    def test_the_same_holds_for_json(self):
        code, stderr = self.show_into_head("-f", "json")
        self.assertEqual(stderr, "")
        self.assertEqual(code, 141)

    def test_a_real_write_error_is_still_reported(self):
        environment = dict(os.environ)
        environment.update(self.env)
        result = subprocess.run(
            [sys.executable, "-m", "sectape", "export", "big",
             "-o", "/nope/nowhere/x.md"],
            capture_output=True, text=True, env=environment, timeout=90)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)


class TestUnusableSessionState(TempConfig):
    """current.json is on disk and can be damaged; nothing may traceback."""

    PAYLOADS = ("[1, 2, 3]", '"a string"', "42", "null", "true",
                "not json at all {{{", "")

    def setUp(self):
        super().setUp()
        self.env = {"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                    "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")}

    COMMANDS = (["status"], ["status", "--json"], ["list"], ["list", "--json"],
                ["doctor"], ["config", "show"])

    def assert_survives(self, command, payload):
        config.settings.current_session_file.write_text(payload, encoding="utf-8")
        result = run(*command, env=self.env)
        self.assertNotIn("Traceback", result.stderr,
                         f"{command} on {payload!r}:\n{result.stderr}")
        self.assertIn(result.returncode, (0, 1),
                      f"{command} on {payload!r} -> {result.returncode}")

    def test_every_read_only_command_survives_a_damaged_state_file(self):
        # The damage is all handled in one place, so the payloads and the
        # commands are covered separately rather than as a cross product of
        # forty-two subprocesses.
        for command in self.COMMANDS:
            self.assert_survives(command, "[1, 2, 3]")

    def test_every_shape_of_damage_survives(self):
        for payload in self.PAYLOADS:
            self.assert_survives(["status"], payload)

    def test_stop_reports_no_session_rather_than_crashing(self):
        for payload in self.PAYLOADS:
            config.settings.current_session_file.write_text(payload,
                                                            encoding="utf-8")
            result = run("stop", env=self.env)
            self.assertNotIn("Traceback", result.stderr, payload)
            self.assertIn("No active session", result.stdout, payload)

    def test_status_json_is_still_valid_json(self):
        for payload in self.PAYLOADS:
            config.settings.current_session_file.write_text(payload,
                                                            encoding="utf-8")
            result = run("status", "--json", env=self.env)
            snap = json.loads(result.stdout)
            self.assertFalse(snap["active"], payload)


def parser_commands() -> list[str]:
    """The primary name of every subcommand, aliases excluded."""
    import argparse
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(choice.dest for choice in action._choices_actions)
    return []


class TestCompletionScripts(unittest.TestCase):
    """These are shipped as shell source and pasted into a user's shell."""

    def script(self, shell: str) -> str:
        import contextlib
        import io
        args = build_parser().parse_args(["completion", shell])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(args.func(args), 0)
        return buffer.getvalue()

    def check_syntax(self, shell_path: str, script: str, suffix: str):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
            fh.write(script)
            name = fh.name
        self.addCleanup(lambda: os.unlink(name))
        result = subprocess.run([shell_path, "-n", name],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"{shell_path} -n rejected it:\n{result.stderr}")

    @unittest.skipUnless(shutil.which("zsh"), "zsh not available")
    def test_the_zsh_script_is_valid_zsh(self):
        self.check_syntax(shutil.which("zsh"), self.script("zsh"), ".zsh")

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_the_bash_script_is_valid_bash(self):
        self.check_syntax(shutil.which("bash"), self.script("bash"), ".bash")

    def test_every_command_is_offered_by_both_scripts(self):
        # Otherwise a command added here quietly stops being completable.
        zsh, bash = self.script("zsh"), self.script("bash")
        for command in parser_commands():
            self.assertIn(command, zsh, f"{command} missing from zsh completion")
            self.assertIn(command, bash, f"{command} missing from bash completion")

    def test_no_script_offers_a_command_that_does_not_exist(self):
        import re as _re
        known = set(parser_commands())
        listed = _re.search(r'commands="([^"]+)"', self.script("bash"))
        self.assertIsNotNone(listed, "the bash script no longer declares a list")
        for command in listed.group(1).split():
            self.assertIn(command, known, f"{command} is completed but not real")

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_the_bash_function_returns_the_commands(self):
        script = self.script("bash")
        result = subprocess.run(
            [shutil.which("bash"), "-c",
             f'{script}\nCOMP_WORDS=(sectape ""); COMP_CWORD=1; '
             '_sectape; printf "%s\\n" "${COMPREPLY[@]}"'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        offered = set(result.stdout.split())
        self.assertEqual(offered, set(parser_commands()))


class TestListShowsTheReadableName(TempConfig):
    """A recording's directory is a slug; its name is in its own meta."""

    def setUp(self):
        super().setUp()
        from sectape.session import write_session_meta
        from sectape.util import slugify
        self.labels = ["cert renewal", "日本語のセッション", "별개의 세션"]
        for label in self.labels:
            session_dir = self.sessions / slugify(label)
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "pane_01.raw").write_text("", encoding="utf-8")
            write_session_meta(session_dir, {"label": label,
                                             "slug": slugify(label),
                                             "started": 1700000000.0})
        self.env = {"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                    "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")}

    def test_the_label_is_shown_not_the_slug(self):
        # A label with no ASCII in it slugs to a digest, which tells the
        # reader nothing at all.
        out = run("list", env=self.env).stdout
        for label in self.labels:
            self.assertIn(label, out)
        self.assertNotIn("session-", out, "a digest slug was shown to the user")

    def test_json_carries_both_the_id_and_the_label(self):
        rows = json.loads(run("list", "--json", env=self.env).stdout)
        by_label = {row["label"]: row["session"] for row in rows}
        self.assertEqual(set(by_label), set(self.labels))
        self.assertEqual(by_label["cert renewal"], "cert_renewal")
        self.assertTrue(by_label["日本語のセッション"].startswith("session-"))

    def test_a_recording_with_no_meta_falls_back_to_its_directory(self):
        (self.sessions / "bare").mkdir(parents=True, exist_ok=True)
        self.assertIn("bare", run("list", env=self.env).stdout)

    def test_every_name_shown_can_be_used_as_an_argument(self):
        for label in self.labels:
            result = run("show", label, env=self.env)
            self.assertEqual(result.returncode, 0, f"{label}: {result.stdout}")
            self.assertIn(label, result.stdout)

    def test_the_rows_still_line_up(self):
        from sectape.ui import display_width
        rows = [line.rstrip() for line in run("list", env=self.env).stdout.split("\n")
                if re.match(r"^\s+\d+\s", line)]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({display_width(r) for r in rows}), 1,
                         "rows with wide characters do not line up")


class TestGlobalOverrides(TempConfig):
    """`--state-dir` / `--output-dir` / `--no-redact` override file and env.

    The documented precedence is defaults < file < environment < flags.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def setUp(self):
        super().setUp()
        import base64
        session = self.sessions / "demo"
        session.mkdir(parents=True, exist_ok=True)
        b64 = lambda t: base64.b64encode(t.encode()).decode()
        (session / "pane_01.raw").write_text(
            f"\x1b]7337;SECTAPE;b|{b64('echo ' + self.SECRET)}|1700000000\x07"
            f"{self.SECRET}\r\n"
            f"\x1b]7337;SECTAPE;e|0|1700000001|{b64('/tmp')}\x07",
            encoding="utf-8")
        self.env = {"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                    "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")}

    def test_state_dir_flag_beats_the_environment(self):
        elsewhere = self.root / "elsewhere"
        seen = json.loads(run("--state-dir", str(elsewhere), "list", "--json",
                              env=self.env).stdout)
        self.assertEqual(seen, [], "the flag did not override the environment")
        # ...and without the flag the environment's own session is there
        seen = json.loads(run("list", "--json", env=self.env).stdout)
        self.assertEqual([r["session"] for r in seen], ["demo"])

    def test_output_dir_flag_redirects_the_export(self):
        target = self.root / "somewhere-else"
        result = run("--output-dir", str(target), "export", "demo", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pathlib.Path(result.stdout.strip()).parent, target)
        self.assertTrue((target / "demo.md").exists())

    def test_no_redact_keeps_the_secret(self):
        self.assertNotIn(self.SECRET, run("show", "demo", env=self.env).stdout)
        self.assertIn(self.SECRET,
                      run("--no-redact", "show", "demo", env=self.env).stdout)

    def test_a_tilde_is_expanded_in_both_flags(self):
        out = run("--state-dir", "~/sectape-flag-probe",
                  "--output-dir", "~/sectape-flag-probe-out",
                  "config", "show", env=self.env).stdout
        home = str(pathlib.Path.home())
        self.addCleanup(self._remove_probe_dirs)
        self.assertIn(f"{home}/sectape-flag-probe", out)
        self.assertNotIn("~/sectape-flag-probe", out)

    def _remove_probe_dirs(self):
        home = pathlib.Path.home()
        for name in ("sectape-flag-probe", "sectape-flag-probe-out"):
            target = home / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    def test_a_flag_beats_a_config_file_too(self):
        path = self.root / "conf.toml"
        path.write_text(f'[output]\ndir = "{self.root / "from-file"}"\n',
                        encoding="utf-8")
        env = dict(self.env)
        env.pop("SECTAPE_OUTPUT_DIR")
        target = self.root / "from-flag"
        out = run("--config", str(path), "--output-dir", str(target),
                  "config", "show", env=env).stdout
        self.assertIn(str(target), out)
        self.assertNotIn(str(self.root / "from-file"), out)


class TestConfirm(unittest.TestCase):
    """The y/N prompt that gates stopping panes other terminals are using."""

    def ask(self, answer, default=False, tty=True):
        import unittest.mock as mock
        with mock.patch("sectape.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = tty
            with mock.patch("builtins.input", side_effect=answer):
                return _confirm("Stop them too?", default=default)

    def test_a_plain_yes(self):
        for answer in ("y", "Y", "yes", "YES", " yes "):
            self.assertTrue(self.ask([answer]), answer)

    def test_anything_else_is_no(self):
        for answer in ("n", "no", "nope", "maybe", "1", "yeah"):
            self.assertFalse(self.ask([answer]), answer)

    def test_an_empty_answer_takes_the_default(self):
        self.assertFalse(self.ask([""], default=False))
        self.assertTrue(self.ask([""], default=True))

    def test_end_of_input_is_no(self):
        self.assertFalse(self.ask(EOFError()))

    def test_an_interrupt_is_no_even_when_the_default_is_yes(self):
        # Ctrl-C at a prompt must never be read as consent.
        self.assertFalse(self.ask(KeyboardInterrupt(), default=True))

    def test_without_a_terminal_it_never_asks(self):
        # Piped or scripted: take the default rather than block on input.
        import unittest.mock as mock
        with mock.patch("sectape.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            with mock.patch("builtins.input",
                            side_effect=AssertionError("must not prompt")):
                self.assertFalse(_confirm("q", default=False))
                self.assertTrue(_confirm("q", default=True))


class TestExportFailureAtTheEndOfASession(TempConfig):
    """A full disk as your shell exits must not read like lost work."""

    def setUp(self):
        super().setUp()
        import base64
        self.session_dir = self.sessions / "readonly"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        b64 = lambda t: base64.b64encode(t.encode()).decode()
        (self.session_dir / "pane_01.raw").write_text(
            f"\x1b]7337;SECTAPE;b|{b64('echo precious')}|1700000000\x07out\r\n"
            f"\x1b]7337;SECTAPE;e|0|1700000001|{b64('/tmp')}\x07",
            encoding="utf-8")
        self.session = {"label": "readonly", "slug": "readonly",
                        "dir": str(self.session_dir), "started": 1700000000.0}

    def finish(self):
        import contextlib
        import io
        import unittest.mock as mock
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sectape.cli.export",
                        side_effect=OSError(13, "Permission denied",
                                            str(self.root / "out" / "readonly.md"))):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                path = _finish(self.session, quiet=False)
        return path, out.getvalue(), err.getvalue()

    def test_the_failure_does_not_propagate(self):
        path, _, _ = self.finish()
        self.assertIsNone(path)

    def test_it_says_what_went_wrong(self):
        _, _, err = self.finish()
        self.assertIn("could not write the export", err)
        self.assertIn("Permission denied", err)

    def test_it_says_the_recording_is_safe(self):
        _, _, err = self.finish()
        self.assertIn("the recording is safe", err)
        self.assertIn("readonly", err)

    def test_it_gives_the_command_to_retry(self):
        _, _, err = self.finish()
        self.assertIn("sectape export readonly", err)

    def test_the_pane_log_is_untouched(self):
        self.finish()
        self.assertTrue((self.session_dir / "pane_01.raw").exists())

    def test_and_the_export_really_does_work_afterwards(self):
        self.finish()
        result = run("export", "readonly",
                     env={"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                          "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                          "SECTAPE_CONFIG": str(self.root / "none.toml")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("echo precious",
                      (config.settings.output_dir / "readonly.md").read_text())


class TestDoctorAgreesWithTheRecorder(TempConfig):
    """`doctor` exists to say whether things will work.

    Claiming hooks the recording will not install is the one thing it must
    never do.
    """

    def setUp(self):
        super().setUp()
        self.env = {"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                    "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")}

    def line(self, **extra):
        result = run("doctor", env=dict(self.env, **extra))
        for line in result.stdout.split("\n"):
            if "shell supports integration" in line:
                return line
        self.fail(f"no such check in:\n{result.stdout}")

    def test_a_supported_shell_passes(self):
        import shutil as _shutil
        shell = _shutil.which("zsh") or _shutil.which("bash")
        if not shell:
            self.skipTest("no supported shell installed")
        self.assertIn("✓", self.line(SECTAPE_SHELL=shell))

    def test_a_shell_that_is_not_installed_is_flagged(self):
        line = self.line(SECTAPE_SHELL="/nonexistent/zsh")
        self.assertNotIn("✓", line)
        self.assertIn("not installed", line)

    def test_an_unsupported_shell_says_why(self):
        line = self.line(SECTAPE_SHELL="/bin/sh")
        self.assertNotIn("✓", line)
        self.assertIn("hooks", line)

    def test_integration_turned_off_is_reported(self):
        import shutil as _shutil
        shell = _shutil.which("zsh") or _shutil.which("bash")
        if not shell:
            self.skipTest("no supported shell installed")
        line = self.line(SECTAPE_SHELL=shell, SECTAPE_SHELL_INTEGRATION="0")
        self.assertNotIn("✓", line)
        self.assertIn("configuration", line)

    def test_the_verdict_matches_integration_available(self):
        # The check and the recorder must never disagree.
        import shutil as _shutil
        cases = ["/nonexistent/zsh", "/bin/sh"]
        found = _shutil.which("zsh") or _shutil.which("bash")
        if found:
            cases.append(found)
        import subprocess
        import sys
        for shell in cases:
            passed = "✓" in self.line(SECTAPE_SHELL=shell)
            probe = subprocess.run(
                [sys.executable, "-c",
                 "from sectape import config; config.load();"
                 " from sectape.recorder import integration_available;"
                 " print(integration_available())"],
                env=dict(os.environ, **dict(self.env, SECTAPE_SHELL=shell)),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(passed, probe.stdout.strip() == "True", shell)


class TestColumnFitting(unittest.TestCase):
    def test_ascii_pads_to_the_asked_width(self):
        self.assertEqual(fit("abc", 6), "abc   ")

    def test_ascii_truncates_to_the_asked_width(self):
        self.assertEqual(fit("abcdefgh", 4), "abcd")

    def test_wide_characters_count_as_two_columns(self):
        self.assertEqual(display_width("日本語"), 6)
        self.assertEqual(display_width("emoji-\U0001F680"), 8)

    def test_a_wide_name_is_padded_to_the_right_screen_width(self):
        for text in ("short", "日本語", "emoji-\U0001F680-tape", "a" * 40):
            self.assertEqual(display_width(fit(text, 20)), 20, text)

    def test_truncation_never_splits_a_wide_character(self):
        # Nine columns cannot hold five two-column glyphs; it keeps four.
        self.assertEqual(fit("日本語です", 9), "日本語で ")


class TestListAlignment(TempConfig):
    """Every row of `sectape list` has to line up, whatever it is called."""

    def test_rows_are_all_the_same_screen_width(self):
        import io
        import contextlib
        for name in ("short", "unicode-日本語-session",
                     "emoji-🚀-tape",
                     "a-really-very-extremely-long-session-name-that-overflows"):
            (self.sessions / name).mkdir(parents=True, exist_ok=True)
        args = build_parser().parse_args(["list"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cmd_list(args)
        rows = [line for line in buffer.getvalue().split("\n")
                if re.match(r"^\s+\d+\s", line)]
        self.assertEqual(len(rows), 4)
        widths = {display_width(line.rstrip()) for line in rows}
        self.assertEqual(len(widths), 1,
                         f"rows have different screen widths: {widths}")


if __name__ == "__main__":
    unittest.main()
