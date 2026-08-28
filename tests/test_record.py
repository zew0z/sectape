"""End-to-end recording, driven through a real pseudo-terminal.

These are the regression tests for the failure that started the project: a
recorded shell that does not inherit the terminal's size, and a terminal left
in raw mode afterwards.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sectape.terminal import set_winsize


def read_until(fd, needle, timeout=20.0, sink=None):
    buf = sink if sink is not None else []
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf.append(chunk.decode("utf-8", "replace"))
        if needle and needle in "".join(buf):
            return "".join(buf), True
    return "".join(buf), False


@unittest.skipUnless(shutil.which("zsh") or shutil.which("bash"),
                     "no supported shell available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestRecording(unittest.TestCase):
    ROWS, COLS = 48, 173

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sectape-e2e-"))
        self.shell = shutil.which("zsh") or shutil.which("bash")
        self.rc = self.root / "rc"
        self.rc.mkdir(parents=True)
        (self.rc / ".zshrc").write_text("PS1='%% '\n")
        (self.rc / ".bashrc").write_text("PS1='$ '\n")
        self.env = dict(os.environ)
        self.env.update({
            "SECTAPE_STATE_DIR": str(self.root / "state"),
            "SECTAPE_OUTPUT_DIR": str(self.root / "out"),
            "SECTAPE_CONFIG": str(self.root / "none.toml"),
            "SECTAPE_SHELL": self.shell,
            "ZDOTDIR": str(self.rc),
            "HOME": str(self.rc) if self.shell.endswith("bash") else os.environ["HOME"],
            "TERM": "xterm-256color",
            "PYTHONUNBUFFERED": "1",
        })
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    # -- harness -----------------------------------------------------------
    def spawn(self, *argv):
        import pty
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execve(sys.executable,
                          [sys.executable, "-m", "sectape", *argv], self.env)
            except Exception:
                pass
            os._exit(127)
        set_winsize(master, self.ROWS, self.COLS)
        return pid, master

    def reap(self, pid, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return True
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        return False

    def session(self, lines, label="e2e"):
        pid, master = self.spawn("rec", label)
        sink = []
        try:
            read_until(master, "recording", 25, sink)
            time.sleep(1.0)
            for line in lines:
                if callable(line):
                    line(master)
                    continue
                os.write(master, (line + "\n").encode())
                time.sleep(0.5)
            os.write(master, b"exit\n")
            read_until(master, None, 25, sink)
        finally:
            self.reap(pid)
            try:
                os.close(master)
            except OSError:
                pass
        return "".join(sink)

    @property
    def exports(self):
        return self.root / "out"

    # -- the size regression ----------------------------------------------
    def test_recorded_shell_inherits_the_real_terminal_size(self):
        out = self.session(["echo COLS=$COLUMNS LINES=$LINES"])
        self.assertIn(f"COLS={self.COLS}", out,
                      "recorded shell did not inherit the terminal width")
        self.assertIn(f"LINES={self.ROWS}", out)
        self.assertNotIn("COLS=80", out)

    def test_resize_is_propagated(self):
        def resize(master):
            set_winsize(master, 30, 99)
            time.sleep(0.6)
        out = self.session([resize, "echo AFTER=$COLUMNS"])
        self.assertIn("AFTER=99", out, "SIGWINCH was not propagated into the pty")

    # -- terminal restoration ---------------------------------------------
    def test_terminal_state_is_reset_on_exit(self):
        out = self.session(["echo bye"])
        self.assertIn("\x1b[?1049l", out, "alt screen not exited")
        self.assertIn("\x1b[?2004l", out, "bracketed paste not disabled")
        self.assertIn("\x1b[?25h", out, "cursor not restored")

    def test_sigterm_restores_and_still_exports(self):
        pid, master = self.spawn("rec", "term")
        sink = []
        try:
            read_until(master, "recording", 25, sink)
            time.sleep(1.0)
            os.write(master, b"echo before-term\n")
            time.sleep(0.8)
            os.kill(pid, signal.SIGTERM)
            read_until(master, None, 15, sink)
            exited = self.reap(pid, 15)
            self.assertTrue(exited, "recorder did not exit on SIGTERM")
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        self.assertIn("\x1b[?1049l", "".join(sink), "terminal not reset on SIGTERM")
        self.assertTrue(list(self.exports.glob("term.*")), "nothing exported")

    # -- the export --------------------------------------------------------
    def test_export_has_exact_commands_and_exit_codes(self):
        self.session(["echo hello-from-e2e", "false"])
        note = self.exports / "e2e.md"
        self.assertTrue(note.exists(), sorted(p.name for p in self.exports.iterdir()))
        text = note.read_text()
        self.assertIn("echo hello-from-e2e", text)
        self.assertIn("hello-from-e2e", text)
        self.assertIn("`false`", text)
        self.assertIn("**exit 1**", text)
        self.assertNotIn("\x1b", text, "escape sequences leaked into the export")

    def test_multiline_output_is_not_a_staircase(self):
        self.session(["printf 'one\\ntwo\\nthree\\n'"])
        text = (self.exports / "e2e.md").read_text()
        self.assertIn("\none\ntwo\nthree\n", text)

    def test_json_format_via_environment(self):
        self.env["SECTAPE_FORMAT"] = "json"
        self.session(["echo json-please"])
        payload = json.loads((self.exports / "e2e.json").read_text())
        self.assertTrue(any("json-please" in s["cmd"] for s in payload["steps"]))
        self.assertEqual(payload["steps"][0]["exit_code"], 0)

    def test_session_file_removed_after_exit(self):
        self.session(["echo done"])
        self.assertFalse((self.root / "state" / "current.json").exists())

    def test_nested_recording_is_refused(self):
        result = subprocess.run(
            [sys.executable, "-m", "sectape", "rec", "nested"],
            env=dict(self.env, SECTAPE_ACTIVE="1"),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("already being recorded", result.stdout)

    def test_rejoining_the_same_label_keeps_other_panes(self):
        state = self.root / "state"
        sessions = state / "sessions" / "e2e"
        sessions.mkdir(parents=True)
        keeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            (state / "current.json").write_text(json.dumps({
                "label": "e2e", "slug": "e2e", "dir": str(sessions),
                "started": time.time(),
                "panes": {"99999": {"pid": keeper.pid,
                                    "log": str(sessions / "pane_99999.raw"),
                                    "started": time.time()}},
            }))
            out = self.session(["echo second-pane"])
            self.assertIn("Rejoining", out)
            session = json.loads((state / "current.json").read_text())
            self.assertIn("99999", session["panes"],
                          "the other pane's registration was destroyed")
        finally:
            keeper.terminate()
            keeper.wait(timeout=10)

    def test_show_prints_the_active_recording(self):
        self.session(["echo showable"])
        result = subprocess.run(
            [sys.executable, "-m", "sectape", "show", "e2e"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("echo showable", result.stdout)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("bash"), "bash not available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestBashRecording(unittest.TestCase):
    """bash uses a DEBUG trap and PROMPT_COMMAND rather than zsh's hooks, so it
    needs its own coverage - the two paths share nothing but the marker format."""

    ROWS, COLS = 40, 131

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sectape-bash-"))
        home = self.root / "home"
        home.mkdir(parents=True)
        (home / ".bashrc").write_text("PS1='bash$ '\n")
        self.env = dict(os.environ)
        self.env.update({
            "SECTAPE_STATE_DIR": str(self.root / "state"),
            "SECTAPE_OUTPUT_DIR": str(self.root / "out"),
            "SECTAPE_CONFIG": str(self.root / "none.toml"),
            "SECTAPE_SHELL": shutil.which("bash"),
            "HOME": str(home),
            "TERM": "xterm-256color",
            "PYTHONUNBUFFERED": "1",
        })
        self.env.pop("ZDOTDIR", None)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def session(self, lines, label="bash-e2e"):
        import pty
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execve(sys.executable,
                          [sys.executable, "-m", "sectape", "rec", label], self.env)
            except Exception:
                pass
            os._exit(127)
        set_winsize(master, self.ROWS, self.COLS)
        sink = []
        try:
            read_until(master, "recording", 25, sink)
            time.sleep(1.2)
            for line in lines:
                os.write(master, (line + "\n").encode())
                time.sleep(0.6)
            os.write(master, b"exit\n")
            read_until(master, None, 25, sink)
        finally:
            deadline = time.time() + 20
            while time.time() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            try:
                os.close(master)
            except OSError:
                pass
        return "".join(sink)

    def test_bash_inherits_the_terminal_size(self):
        out = self.session(["echo COLS=$COLUMNS"])
        self.assertIn(f"COLS={self.COLS}", out)

    def test_bash_integration_captures_commands_and_exit_codes(self):
        self.session(["echo bash-hello", "false"])
        note = self.root / "out" / "bash-e2e.md"
        self.assertTrue(note.exists(),
                        sorted(p.name for p in (self.root / "out").iterdir()))
        text = note.read_text()
        self.assertIn("echo bash-hello", text)
        self.assertIn("bash-hello", text)
        self.assertIn("`false`", text)
        self.assertIn("**exit 1**", text)

    def test_bash_output_is_not_a_staircase(self):
        self.session(["printf 'a\\nb\\nc\\n'"])
        text = (self.root / "out" / "bash-e2e.md").read_text()
        self.assertIn("\na\nb\nc\n", text)

    def test_bash_does_not_record_its_own_setup(self):
        # The DEBUG trap used to be installed before PROMPT_COMMAND was set, so
        # the trap captured that assignment as the session's first command.
        self.session(["echo real-command"])
        text = (self.root / "out" / "bash-e2e.md").read_text()
        self.assertNotIn("PROMPT_COMMAND", text)
        self.assertNotIn("_sectape_precmd", text)
        self.assertIn("### 1. `echo real-command`", text)

    def test_bash_records_the_whole_typed_line(self):
        # BASH_COMMAND is only the current simple command, so compound lines
        # were truncated to their first clause.
        self.session(["echo one; echo two", "for i in 1 2; do echo n$i; done"])
        text = (self.root / "out" / "bash-e2e.md").read_text()
        self.assertIn("### 1. `echo one; echo two`", text)
        self.assertIn("### 2. `for i in 1 2; do echo n$i; done`", text)
