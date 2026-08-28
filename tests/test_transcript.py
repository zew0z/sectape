import shutil
import tempfile
import unittest
from pathlib import Path

from sectape.markers import capture_width
from sectape.transcript import (Step, collect_steps, count_commands,
                                parse_heuristic_transcript, parse_transcript)
from tests.helpers import TempConfig, begin, end, legacy_marker, size, b64


class TestMarkerParsing(TempConfig):
    def test_exact_command_and_output(self):
        raw = "prompt$ " + begin("ls -la") + "total 4\r\nfile\r\n" + end(0)
        steps = parse_transcript(raw)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].cmd, "ls -la")
        self.assertEqual(steps[0].output, "total 4\nfile")
        self.assertEqual(steps[0].exit_code, 0)
        self.assertEqual(steps[0].source, "marker")

    def test_exit_code_cwd_duration(self):
        raw = begin("cat missing", 100.0) + "No such file\r\n" + end(1, 102.5, "/tmp/work")
        step = parse_transcript(raw)[0]
        self.assertEqual(step.exit_code, 1)
        self.assertTrue(step.failed)
        self.assertEqual(step.cwd, "/tmp/work")
        self.assertAlmostEqual(step.duration, 2.5, places=3)

    def test_multiline_command_survives(self):
        cmd = "for i in 1 2 3; do\n  echo $i\ndone"
        step = parse_transcript(begin(cmd) + "1\r\n2\r\n3\r\n" + end(0))[0]
        self.assertEqual(step.cmd, cmd)

    def test_prompt_noise_between_commands_ignored(self):
        raw = (begin("ls") + "a b\r\n" + end(0)
               + "\x1b[38;2;1;2;3m user ❯ \x1b[0m"
               + begin("pwd") + "/home\r\n" + end(0))
        self.assertEqual([s.cmd for s in parse_transcript(raw)], ["ls", "pwd"])

    def test_unfinished_command_still_recorded(self):
        step = parse_transcript(begin("sleep 100") + "interrupted\r\n")[0]
        self.assertEqual(step.cmd, "sleep 100")
        self.assertIsNone(step.exit_code)

    def test_recorder_and_shell_noise_ignored(self):
        raw = (begin("sectape stop") + end(0) + begin("exit") + end(0)
               + begin("id") + "uid=0\r\n" + end(0))
        self.assertEqual([s.cmd for s in parse_transcript(raw)], ["id"])

    def test_escape_sequences_in_output_resolved(self):
        raw = begin("wget f") + "10%\r50%\r100% done\r\n" + end(0)
        self.assertEqual(parse_transcript(raw)[0].output, "100% done")

    def test_logs_from_the_previous_name_still_parse(self):
        raw = (legacy_marker("b", b64("whoami"), "1.0") + "root\r\n"
               + legacy_marker("e", "0", "1.2", b64("/root")))
        step = parse_transcript(raw)[0]
        self.assertEqual(step.cmd, "whoami")
        self.assertEqual(step.cwd, "/root")

    def test_width_marker_applies_to_output(self):
        wide = "Z" * 150
        raw = size(200) + begin("echo z") + wide + "\r\n" + end(0)
        self.assertEqual(parse_transcript(raw)[0].output, wide)

    def test_capture_width_defaults_to_80(self):
        self.assertEqual(capture_width("no markers"), 80)
        self.assertEqual(capture_width(size(173)), 173)


class TestHeuristicParsing(TempConfig):
    def test_powerline_prompt(self):
        raw = " user  ~  ❯ whoami\r\nuser\r\n user  ~  ❯ id\r\nuid=501\r\n"
        steps = parse_heuristic_transcript(raw)
        self.assertEqual([s.cmd for s in steps], ["whoami", "id"])
        self.assertEqual(steps[0].output, "user")

    def test_ssh_style_prompt(self):
        raw = "user@target:/var/www$ ls -la\r\ntotal 4\r\n"
        self.assertEqual(parse_heuristic_transcript(raw)[0].cmd, "ls -la")

    def test_output_containing_dollar_is_not_a_command(self):
        raw = ("user ❯ cat script.sh\r\n"
               "export PATH=$HOME/bin:$PATH\r\n"
               "# a comment line\r\n"
               "echo $USER\r\n")
        steps = parse_heuristic_transcript(raw)
        self.assertEqual(len(steps), 1)
        self.assertIn("export PATH=$HOME/bin:$PATH", steps[0].output)

    def test_bare_prompt_closes_the_step(self):
        raw = ("user ❯ ls\r\na.txt\r\nuser ❯ \r\nuser ❯ \r\nuser ❯ pwd\r\n/home\r\n")
        steps = parse_heuristic_transcript(raw)
        self.assertEqual([s.cmd for s in steps], ["ls", "pwd"])
        self.assertEqual(steps[0].output, "a.txt")

    def test_markers_win_over_heuristics(self):
        raw = "user ❯ decoy\r\n" + begin("real") + "out\r\n" + end(0)
        self.assertEqual([s.cmd for s in parse_transcript(raw)], ["real"])


class TestInteractiveSummary(TempConfig):
    def test_full_screen_output_replaced(self):
        step = parse_transcript(begin("vim notes.md") + "MANGLED REDRAW\r\n" + end(0))[0]
        self.assertIn("interactive vim session", step.output)
        self.assertNotIn("MANGLED", step.output)

    def test_sudo_prefix_still_detected(self):
        step = parse_transcript(begin("sudo less /var/log/x") + "junk\r\n" + end(0))[0]
        self.assertIn("interactive less session", step.output)

    def test_ordinary_command_untouched(self):
        step = parse_transcript(begin("cat notes.txt") + "real content\r\n" + end(0))[0]
        self.assertEqual(step.output, "real content")

    def test_exit_code_still_recorded(self):
        self.assertEqual(parse_transcript(begin("less m") + "j\r\n" + end(1))[0].exit_code, 1)


class TestCollecting(TempConfig):
    def test_crlf_survives_reading_the_log(self):
        d = self.make_session("crlf", begin("seq 3") + "1\r\n2\r\n3\r\n" + end(0))
        steps, _ = collect_steps(d)
        self.assertEqual(steps[0].output, "1\n2\n3")

    def test_multiple_panes_in_time_order(self):
        import time
        d = self.sessions / "multi"
        d.mkdir(parents=True)
        (d / "pane_1.raw").write_text(begin("first") + "1\r\n" + end(0))
        time.sleep(0.02)
        (d / "pane_2.raw").write_text(begin("second") + "2\r\n" + end(0))
        steps, panes = collect_steps(d)
        self.assertEqual(panes, 2)
        self.assertEqual([s.cmd for s in steps], ["first", "second"])

    def test_redaction_applied(self):
        d = self.make_session(
            "sec", begin("curl -H 'Authorization: Bearer sekrit.token'") + "ok\r\n" + end(0))
        steps, _ = collect_steps(d)
        self.assertNotIn("sekrit.token", steps[0].cmd)

    def test_redaction_can_be_disabled(self):
        d = self.make_session(
            "sec2", begin("curl -H 'Authorization: Bearer sekrit.token'") + "ok\r\n" + end(0))
        steps, _ = collect_steps(d, do_redact=False)
        self.assertIn("sekrit.token", steps[0].cmd)

    def test_empty_session(self):
        d = self.sessions / "empty"
        d.mkdir(parents=True)
        self.assertEqual(collect_steps(d), ([], 0))


class TestChronologicalMerge(TempConfig):
    def test_panes_interleave_by_timestamp(self):
        # Reading pane logs end to end put all of pane 1 before pane 2.
        import time as _t
        d = self.sessions / "multi"
        d.mkdir(parents=True)
        (d / "pane_1.raw").write_text(begin("first", 10.0) + "a\r\n" + end(0, 10.5)
                                      + begin("third", 30.0) + "c\r\n" + end(0, 30.5))
        _t.sleep(0.02)
        (d / "pane_2.raw").write_text(begin("second", 20.0) + "b\r\n" + end(0, 20.5))
        steps, _ = collect_steps(d)
        self.assertEqual([s.cmd for s in steps], ["first", "second", "third"])

    def test_output_stays_with_its_command(self):
        d = self.sessions / "multi2"
        d.mkdir(parents=True)
        (d / "pane_1.raw").write_text(begin("one", 10.0) + "OUT-ONE\r\n" + end(0, 10.5))
        (d / "pane_2.raw").write_text(begin("two", 5.0) + "OUT-TWO\r\n" + end(0, 5.5))
        steps, _ = collect_steps(d)
        by_cmd = {s.cmd: s.output for s in steps}
        self.assertEqual(by_cmd["one"], "OUT-ONE")
        self.assertEqual(by_cmd["two"], "OUT-TWO")
        self.assertEqual([s.cmd for s in steps], ["two", "one"])

    def test_unmarked_transcripts_keep_their_order(self):
        d = self.make_session("plain", "user ❯ alpha\r\nx\r\nuser ❯ beta\r\ny\r\n")
        steps, _ = collect_steps(d)
        self.assertEqual([s.cmd for s in steps], ["alpha", "beta"])


class TestCounting(TempConfig):
    def test_counts_markers_without_rendering(self):
        d = self.make_session("c1", begin("a") + "x\r\n" + end(0) + begin("b") + "y\r\n" + end(0))
        self.assertEqual(count_commands(d), 2)

    def test_falls_back_for_unmarked_logs(self):
        d = self.make_session("c2", "user ❯ ls\r\na\r\nuser ❯ pwd\r\n/x\r\n")
        self.assertEqual(count_commands(d), 2)

    def test_oversized_log_reports_unknown(self):
        from sectape.transcript import MAX_SCAN_BYTES
        d = self.sessions / "big"
        d.mkdir(parents=True)
        (d / "pane_1.raw").write_bytes(b"x" * (MAX_SCAN_BYTES + 1))
        self.assertIsNone(count_commands(d))

    def test_ignored_commands_are_not_counted(self):
        # `list` used to report every marker, so it disagreed with the export.
        d = self.make_session("noise", begin("ls") + "a\r\n" + end(0)
                              + begin("clear") + end(0)
                              + begin("exit") + end(0)
                              + begin("sectape stop") + end(0))
        steps, _ = collect_steps(d)
        self.assertEqual(count_commands(d), len(steps))
        self.assertEqual(count_commands(d), 1)

    def test_agrees_with_full_parse(self):
        d = self.make_session("c3", begin("a") + "x\r\n" + end(0)
                              + begin("vim f") + "junk\r\n" + end(0))
        steps, _ = collect_steps(d)
        self.assertEqual(count_commands(d), len(steps))


if __name__ == "__main__":
    unittest.main()
