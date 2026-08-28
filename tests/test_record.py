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
            read_until(master, "REC", 25, sink)
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
            read_until(master, "REC", 25, sink)
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

    def test_sighup_restores_and_still_exports(self):
        # Closing the terminal window sends SIGHUP, which is the commonest
        # unclean end to a recording - commoner than SIGTERM.
        pid, master = self.spawn("rec", "hangup")
        sink = []
        try:
            read_until(master, "REC", 25, sink)
            time.sleep(1.0)
            os.write(master, b"echo work-before-hangup\n")
            time.sleep(0.8)
            os.kill(pid, signal.SIGHUP)
            read_until(master, None, 15, sink)
            self.assertTrue(self.reap(pid, 15), "recorder did not exit on SIGHUP")
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        self.assertIn("\x1b[?1049l", "".join(sink), "terminal not reset on SIGHUP")
        note = self.exports / "hangup.md"
        self.assertTrue(note.exists(),
                        sorted(p.name for p in self.exports.iterdir()))
        self.assertIn("work-before-hangup", note.read_text(),
                      "the work done before the hangup was lost")
        self.assertFalse((self.root / "state" / "current.json").exists(),
                         "the session was left marked active")

    def test_a_recording_survives_the_recorder_being_killed_outright(self):
        # SIGKILL gives the recorder no chance to unwind, so nothing is
        # exported at the time. The pane log is on disk, and that has to be
        # enough to get the work back.
        state = self.root / "state"
        pid, master = self.spawn("rec", "killed")
        try:
            read_until(master, "REC", 25)
            time.sleep(1.0)
            os.write(master, b"echo work-before-kill\n")
            time.sleep(0.8)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        finally:
            try:
                os.close(master)
            except OSError:
                pass

        self.assertFalse((self.exports / "killed.md").exists(),
                         "a killed recorder cannot have exported")
        self.assertTrue(list((state / "sessions" / "killed").glob("pane_*.raw")),
                        "the pane log did not survive")

        # `status` must not claim a pane is still recording.
        status = subprocess.run(
            [sys.executable, "-m", "sectape", "status"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn("0 live panes", status.stdout)
        self.assertNotIn("REC", status.stdout, "a dead pane was reported as live")

        # The work is recoverable by name.
        result = subprocess.run(
            [sys.executable, "-m", "sectape", "export", "killed"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("work-before-kill", (self.exports / "killed.md").read_text())

        # And `stop` clears the session that was left behind.
        subprocess.run([sys.executable, "-m", "sectape", "stop"],
                       env=self.env, capture_output=True, text=True, timeout=60)
        self.assertFalse((state / "current.json").exists(),
                         "the abandoned session was never cleared")

    def banner_of(self, *argv):
        """Start a recording, capture its banner, and stop it promptly."""
        pid, master = self.spawn(*argv)
        sink = []
        try:
            read_until(master, "integration", 25, sink)
            # The banner is printed before the shell is exec'd, so `exit`
            # sent straight away is swallowed by a shell that is not yet
            # reading - and then the test waits out its own timeout.
            time.sleep(1.0)
            os.write(master, b"exit\n")
            # Drain while waiting: a recorder blocked on a full pty cannot
            # exit, and a blind sleep here made these tests take a minute.
            deadline = time.time() + 20
            while time.time() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                read_until(master, None, 0.2, sink)
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        lines = [l for l in "".join(sink).split("\n") if "integration" in l]
        return lines[0] if lines else ""

    def test_the_banner_admits_when_integration_is_off(self):
        # It used to read the command-line flag alone, so it said
        # "integration on" for a shell whose hooks sectape cannot write, and
        # for `shell_integration = false` in the config.
        self.assertIn("integration on", self.banner_of("rec", "on-check"))
        self.assertIn("integration off",
                      self.banner_of("rec", "off-check", "--no-integration"))

    def test_the_banner_says_off_for_a_shell_without_hooks(self):
        self.env["SECTAPE_SHELL"] = shutil.which("sh") or "/bin/sh"
        self.assertIn("integration off", self.banner_of("rec", "no-hooks"),
                      "the banner promised hooks a plain sh cannot have")

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
            self.assertIn("rejoining", out.lower())
            session = json.loads((state / "current.json").read_text())
            self.assertIn("99999", session["panes"],
                          "the other pane's registration was destroyed")
        finally:
            keeper.terminate()
            keeper.wait(timeout=10)

    def test_stop_from_inside_ends_the_session(self):
        # `sectape stop` typed into a recorded shell must end the recording,
        # not just export while the pane keeps running.
        import pty
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execve(sys.executable,
                          [sys.executable, "-m", "sectape", "rec", "inside"], self.env)
            except Exception:
                pass
            os._exit(127)
        set_winsize(master, self.ROWS, self.COLS)
        sink = []
        try:
            read_until(master, "REC", 25, sink)
            time.sleep(1.0)
            os.write(master, b"echo before-stop\n")
            time.sleep(0.6)
            os.write(master,
                     f"{sys.executable} -m sectape stop\n".encode())
            read_until(master, None, 25, sink)
            exited = self.reap(pid, 20)
            self.assertTrue(exited, "recorder did not exit when stopped from inside")
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        self.assertTrue((self.exports / "inside.md").exists(),
                        sorted(p.name for p in self.exports.iterdir()))
        self.assertIn("echo before-stop", (self.exports / "inside.md").read_text())

    def test_note_helper_is_available_inside_a_recording(self):
        self.session(["note 'annotated from inside'", "echo after"], label="helper")
        text = (self.exports / "helper.md").read_text()
        self.assertIn("annotated from inside", text)
        # the helper itself is plumbing, not a recorded command
        self.assertNotIn("### 1. `note", text)

    def test_panes_are_numbered_one_and_two(self):
        self.session(["echo first-pane"], label="numbered")
        logs = sorted((self.root / "state" / "sessions" / "numbered").glob("pane_*.raw"))
        self.assertEqual([p.name for p in logs], ["pane_01.raw"])

    def test_the_last_pane_out_finishes_the_session_even_when_it_attached(self):
        # Leaving the first tab before the attached one stranded the whole
        # recording: no panes left, nothing exported, and current.json still
        # on disk claiming to be live. Only `rec` used to finish a session.
        state = self.root / "state"
        first_pid, first = self.spawn("rec", "handover")
        read_until(first, "REC", 25)
        time.sleep(1.0)
        os.write(first, b"echo from-first\n")
        time.sleep(0.8)

        second_pid, second = self.spawn("attach")
        read_until(second, "REC", 25)
        time.sleep(1.0)
        os.write(second, b"echo from-second\n")
        time.sleep(0.8)

        try:
            os.write(first, b"exit\n")
            left, _ = read_until(first, None, 20)
            self.reap(first_pid)
            self.assertIn("still recording", left,
                          "the recorder that left first ended the session")
            self.assertTrue((state / "current.json").exists(),
                            "session was closed while a pane was still live")

            os.write(second, b"exit\n")
            read_until(second, None, 20)
            self.reap(second_pid)
        finally:
            for fd in (first, second):
                try:
                    os.close(fd)
                except OSError:
                    pass

        note = self.exports / "handover.md"
        self.assertTrue(note.exists(),
                        "the attached pane left without exporting: "
                        + str(sorted(p.name for p in self.exports.iterdir())))
        text = note.read_text()
        self.assertIn("echo from-first", text)
        self.assertIn("echo from-second", text)
        self.assertFalse((state / "current.json").exists(),
                         "the finished session is still marked active")

    def test_no_integration_reads_commands_off_the_screen(self):
        # The documented fallback for other shells and ssh sessions inside a
        # recording. It needs a prompt the reader recognises, so use the
        # classic user@host form rather than this suite's bare `%`.
        for name in (".zshrc", ".bashrc"):
            (self.rc / name).write_text("PS1='user@host:~$ '\n")
        pid, master = self.spawn("rec", "reconstructed", "--no-integration")
        try:
            read_until(master, "REC", 25)
            time.sleep(1.2)
            for line in ("echo alpha-one", "echo omega-two"):
                os.write(master, (line + "\n").encode())
                time.sleep(0.6)
            os.write(master, b"exit\n")
            read_until(master, None, 25)
            self.reap(pid)
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        note = self.exports / "reconstructed.md"
        self.assertTrue(note.exists(),
                        sorted(p.name for p in self.exports.iterdir()))
        text = note.read_text()
        self.assertIn("Reconstructed", text,
                      "an export read off the screen must say so")
        self.assertIn("### 1. `echo alpha-one`", text)
        self.assertIn("### 2. `echo omega-two`", text)
        self.assertIn("alpha-one", text)

    def test_output_written_as_the_shell_exits_is_captured(self):
        # The recorder drains the pty after its loop ends. Without that, a
        # command printing on its way out lost the tail of its output.
        pid, master = self.spawn("rec", "lastword")
        try:
            read_until(master, "REC", 25)
            time.sleep(1.2)
            os.write(master, b"seq 1 4000 | tail -3; exit\n")
            read_until(master, None, 25)
            self.reap(pid)
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        text = (self.exports / "lastword.md").read_text()
        self.assertIn("seq 1 4000", text)
        self.assertIn("4000", text, "the last command's output was lost")

    def test_stop_force_stops_the_recorder_rather_than_orphaning_it(self):
        # --force used to delete the session and leave the recorder running,
        # so the user was left in a shell still logging to a session that no
        # longer existed, and that work never reached an export.
        state = self.root / "state"
        pid, master = self.spawn("rec", "forced")
        try:
            read_until(master, "REC", 25)
            time.sleep(1.2)
            os.write(master, b"echo before-force\n")
            time.sleep(0.8)

            # Keep reading the pty while `stop` runs: a real terminal always
            # drains, and a recorder blocked on a full buffer cannot act on a
            # signal.
            stopper = subprocess.Popen(
                [sys.executable, "-m", "sectape", "stop", "--force"],
                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            deadline = time.time() + 40
            while time.time() < deadline and stopper.poll() is None:
                read_until(master, None, 0.3)
            out, err = stopper.communicate(timeout=20)
            self.assertEqual(stopper.returncode, 0, out + err)

            deadline = time.time() + 20
            stopped = False
            while time.time() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    stopped = True
                    break
                read_until(master, None, 0.3)
            if not stopped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            self.assertTrue(stopped,
                            f"--force left the recorder running\nstdout={out!r}")
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        self.assertFalse((state / "current.json").exists())
        note = self.exports / "forced.md"
        self.assertTrue(note.exists(),
                        sorted(p.name for p in self.exports.iterdir()))
        self.assertIn("echo before-force", note.read_text())

    def test_a_session_killed_inside_a_full_screen_program_still_exports(self):
        # The alternate screen is never left when the recorder is killed in
        # `less` or `vim`, and the transcript used to be discarded in favour
        # of the redrawn screen.
        pid, master = self.spawn("rec", "fullscreen")
        try:
            read_until(master, "REC", 25)
            time.sleep(1.2)
            os.write(master, b"echo important-work\n")
            time.sleep(0.8)
            # enter the alternate screen and stay there
            os.write(master, b"printf '\\033[?1049hVIM-SCREEN-NOISE\\n'\n")
            time.sleep(0.8)
            os.kill(pid, signal.SIGTERM)
            read_until(master, None, 20)
            self.assertTrue(self.reap(pid, 20), "recorder did not stop")
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        note = self.exports / "fullscreen.md"
        self.assertTrue(note.exists(),
                        sorted(p.name for p in self.exports.iterdir()))
        text = note.read_text()
        self.assertIn("important-work", text, "the transcript was thrown away")
        # The marker text is part of the command the user typed, so it appears
        # in that step's heading and in its echoed command line - twice. A
        # third occurrence would mean the redrawn screen was kept as output.
        self.assertEqual(text.count("VIM-SCREEN-NOISE"), 2,
                         "the alternate-screen redraw was captured as output")

    def test_recording_again_under_the_same_label_says_it_is_appending(self):
        # The panes of an earlier recording are kept and numbered as though
        # they had been open alongside this one, so say so rather than
        # letting the export quietly contain both.
        self.session(["echo first-day"], label="reused")
        pid, master = self.spawn("rec", "reused")
        sink = []
        try:
            read_until(master, "REC", 25, sink)
            time.sleep(1.0)
            os.write(master, b"echo second-day\n")
            time.sleep(0.7)
            os.write(master, b"exit\n")
            read_until(master, None, 25, sink)
            self.reap(pid)
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        printed = "".join(sink)
        self.assertIn("already holds", printed,
                      "appending to an existing label was silent")
        text = (self.exports / "reused.md").read_text()
        self.assertIn("echo first-day", text)
        self.assertIn("echo second-day", text)

    def test_show_prints_the_active_recording(self):
        self.session(["echo showable"])
        result = subprocess.run(
            [sys.executable, "-m", "sectape", "show", "e2e"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("echo showable", result.stdout)


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
            read_until(master, "REC", 25, sink)
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


if __name__ == "__main__":
    unittest.main()
