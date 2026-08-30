import shutil
import tempfile
import unittest
from pathlib import Path

from sectape.markers import capture_width
from sectape.transcript import (Step, collect_steps, count_commands,
                                dedupe_steps, parse_epoch,
                                parse_heuristic_transcript, parse_transcript,
                                render_capture)
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

    def test_a_private_key_cut_in_half_by_trimming_is_still_redacted(self):
        # The output is trimmed to max_output_lines before it reaches the
        # export. The private-key pattern needs its own -----END----- line to
        # match, and the trim used to run first: cutting the middle out of a
        # long log took the END line with it and left the header plus twenty
        # lines of key material in the document, unredacted.
        key = (["-----BEGIN RSA PRIVATE KEY-----"]
               + [f"MIIEow{i:04d}AAKCAQEA{'x' * 40}" for i in range(25)]
               + ["-----END RSA PRIVATE KEY-----"])
        body = ([f"log line {i}" for i in range(260)] + key
                + [f"log line {i}" for i in range(260, 634)])
        d = self.make_session(
            "leak", begin("cat dump.txt") + "\r\n".join(body) + "\r\n" + end(0))
        out = collect_steps(d)[0][0].output
        self.assertNotIn("-----BEGIN RSA PRIVATE KEY-----", out)
        self.assertEqual([l for l in out.split("\n") if l.startswith("MIIEow")], [])
        self.assertIn("<REDACTED: private key>", out)

    def test_a_secret_on_an_over_long_line_survives_that_truncation_too(self):
        # A line past 300 characters is cut down to 250. That cut used to
        # happen before redaction, so a one-line key lost its END marker the
        # same way.
        one_line = ("-----BEGIN PRIVATE KEY-----"
                    + "MIIEowIBAAKCAQEA" * 30 + "-----END PRIVATE KEY-----")
        d = self.make_session(
            "longline", begin("cat key.pem") + one_line + "\r\n" + end(0))
        out = collect_steps(d)[0][0].output
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertIn("<REDACTED: private key>", out)

    def test_no_redact_still_keeps_the_output_verbatim(self):
        d = self.make_session(
            "verbatim", begin("cat key.pem")
            + "-----BEGIN RSA PRIVATE KEY-----\r\nMIIEowIBAAKCAQEA\r\n"
            + "-----END RSA PRIVATE KEY-----\r\n" + end(0))
        out = collect_steps(d, do_redact=False)[0][0].output
        self.assertIn("MIIEowIBAAKCAQEA", out)

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

    def test_the_listing_agrees_with_the_export_on_unmarked_logs(self):
        # count_commands counted before dedupe, so a listing could promise
        # commands the document did not contain. Each of these exercises a
        # different reason the two used to drift apart.
        prompt = "user@host:~$ "
        cases = {
            "distinct": "".join(f"{prompt}grep -rn p{i} /var/log\r\nhit {i}\r\n"
                                for i in range(8)),
            # The same command, different output each time: genuinely run
            # eight times, and all eight are kept.
            "changing": "".join(f"{prompt}ls\r\nfile{i}.txt\r\n" for i in range(8)),
            # The same command with identical output. The screen-scraping
            # reader cannot tell these from a prompt it read twice, so they
            # collapse - and the listing has to say so too.
            "identical": f"{prompt}date\r\nMon 30 Aug 2026\r\n" * 6,
            # Two `less` sessions with different screens on them. Both are
            # rewritten to the same one-line summary before dedupe runs, which
            # is what makes them identical in the first place.
            "fullscreen": (f"{prompt}less notes.md\r\nSCREEN ONE\r\n"
                           f"{prompt}less notes.md\r\nSCREEN TWO\r\n"),
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                d = self.make_session(f"agree-{name}", raw)
                self.assertEqual(count_commands(d), len(collect_steps(d)[0]),
                                 f"{name}: the listing and the export disagree")

    def test_identical_repeats_collapse_in_both(self):
        d = self.make_session("collapse", "user@host:~$ date\r\nMon\r\n" * 6)
        self.assertEqual(count_commands(d), 1)
        self.assertEqual(len(collect_steps(d)[0]), 1)

    def test_a_marked_log_still_counts_every_run(self):
        # A marker pair is proof the command really ran, so identical repeats
        # are kept - and the count must not start collapsing those.
        raw = (begin("date") + "Mon\r\n" + end(0)) * 4
        d = self.make_session("marked-repeats", raw)
        self.assertEqual(count_commands(d), 4)
        self.assertEqual(len(collect_steps(d)[0]), 4)

    def test_oversized_log_reports_unknown(self):
        # Patched rather than written for real: the ceiling is tens of
        # megabytes, and the point is the comparison, not the disk.
        import unittest.mock as mock
        d = self.sessions / "big"
        d.mkdir(parents=True)
        (d / "pane_1.raw").write_bytes(b"x" * 4096)
        with mock.patch("sectape.transcript.MAX_SCAN_BYTES", 1024):
            self.assertIsNone(count_commands(d))

    def test_a_large_marked_log_is_still_counted(self):
        # Marked logs only need a regex sweep, so they are counted well past
        # the point where screen-scraping would be refused. One threshold for
        # both used to reject them together.
        import unittest.mock as mock
        raw = "".join(begin(f"echo {i}", 1700000000 + i) + "out\r\n"
                      + end(0, 1700000000 + i + 0.5) for i in range(200))
        d = self.make_session("marked-big", raw)
        with mock.patch("sectape.transcript.MAX_REPLAY_BYTES", 16):
            self.assertEqual(count_commands(d), 200)

    def test_a_large_unmarked_log_is_refused(self):
        # Replaying the screen is roughly forty times dearer per byte, so it
        # has a much lower ceiling of its own.
        import unittest.mock as mock
        d = self.make_session("plain-big",
                              "user@host:~$ ls\r\n" + "output\r\n" * 50)
        with mock.patch("sectape.transcript.MAX_REPLAY_BYTES", 16):
            self.assertIsNone(count_commands(d))

    def test_the_replay_ceiling_is_well_below_the_read_ceiling(self):
        from sectape.transcript import MAX_REPLAY_BYTES, MAX_SCAN_BYTES
        self.assertLess(MAX_REPLAY_BYTES, MAX_SCAN_BYTES)

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


class TestTimestampParsing(unittest.TestCase):
    """Shell timestamps arrive in whatever form the platform managed."""

    def test_plain_float(self):
        self.assertAlmostEqual(parse_epoch("1700000000.5"), 1700000000.5)

    def test_bsd_date_has_no_percent_n(self):
        # `date +%s.%N` on BSD/macOS prints a literal N. The whole recording
        # used to lose its timings because float() refused the string.
        self.assertAlmostEqual(parse_epoch("1700000000.N"), 1700000000.0)

    def test_locale_decimal_comma(self):
        # bash formats EPOCHREALTIME with the locale's decimal point.
        self.assertAlmostEqual(parse_epoch("1700000000,25"), 1700000000.25)

    def test_junk_is_none(self):
        for value in ("", None, "N", "not-a-time"):
            self.assertIsNone(parse_epoch(value), value)


class TestBrokenTimestampsInAMarkedLog(TempConfig):
    def test_bsd_style_stamps_still_give_durations(self):
        raw = (begin("sleep 2", "1700000000.N") + end(0, "1700000002.N"))
        step = parse_transcript(raw)[0]
        self.assertAlmostEqual(step.started, 1700000000.0)
        self.assertAlmostEqual(step.duration, 2.0)

    def test_comma_stamps_still_give_durations(self):
        raw = (begin("sleep 1", "1700000000,5") + end(0, "1700000002,0"))
        step = parse_transcript(raw)[0]
        self.assertAlmostEqual(step.duration, 1.5)


class TestACommandWhoseEndMarkerNeverArrived(TempConfig):
    def test_it_is_kept_when_another_command_follows(self):
        # The shell never reached its prompt hook, so no end marker was
        # written. The command still ran. It used to be overwritten by the
        # next begin marker and vanish - output and all - which made `list`
        # and the export disagree about how many commands the session held.
        raw = (begin("echo one") + "one\r\n" + end(0)
               + begin("./flaky-thing") + "partial output\r\n"
               + begin("echo three") + "three\r\n" + end(0))
        steps = parse_transcript(raw)
        self.assertEqual([s.cmd for s in steps],
                         ["echo one", "./flaky-thing", "echo three"])
        stranded = steps[1]
        self.assertEqual(stranded.output, "partial output")
        self.assertIsNone(stranded.exit_code)
        self.assertFalse(stranded.failed)

    def test_the_listing_and_the_export_agree(self):
        raw = (begin("echo one") + "one\r\n" + end(0)
               + begin("./flaky-thing") + "partial output\r\n"
               + begin("echo three") + "three\r\n" + end(0))
        d = self.make_session("stranded", raw)
        self.assertEqual(count_commands(d), len(collect_steps(d)[0]))

    def test_two_stranded_commands_in_a_row_both_survive(self):
        raw = (begin("first") + "a\r\n"
               + begin("second") + "b\r\n"
               + begin("third") + "c\r\n" + end(0))
        steps = parse_transcript(raw)
        self.assertEqual([s.cmd for s in steps], ["first", "second", "third"])
        self.assertEqual([s.output for s in steps], ["a", "b", "c"])

    def test_the_last_one_is_still_kept_at_the_end_of_the_log(self):
        # This path always worked; it is the reason the middle one going
        # missing was inconsistent rather than merely unfortunate.
        steps = parse_transcript(begin("sleep 100") + "interrupted\r\n")
        self.assertEqual([s.cmd for s in steps], ["sleep 100"])
        self.assertEqual(steps[0].output, "interrupted")


class TestTypedAheadEcho(TempConfig):
    """A terminal echoes what you type while the last command is still running.

    Start something slow and type the next command while you wait, and those
    keystrokes land inside the running command's output region. `sleep 3` came
    out of the parser with the next command as its entire output.
    """

    def test_the_next_command_is_not_this_command_s_output(self):
        raw = (begin("sleep 3") + "echo typed-during-the-sleep\r\n" + end(0)
               + begin("echo typed-during-the-sleep") + "typed-during-the-sleep\r\n"
               + end(0))
        steps = parse_transcript(raw)
        self.assertEqual(steps[0].output, "")
        self.assertEqual(steps[1].output, "typed-during-the-sleep")

    def test_several_commands_typed_ahead_are_all_taken(self):
        raw = (begin("sleep 5") + "real output\r\nsecond cmd\r\nthird cmd\r\n" + end(0)
               + begin("second cmd") + "b\r\n" + end(0)
               + begin("third cmd") + "c\r\n" + end(0))
        self.assertEqual(parse_transcript(raw)[0].output, "real output")

    def test_a_command_typed_ahead_of_an_ignored_one_still_goes(self):
        # `sectape note` is dropped from the document, but it echoes like
        # anything else, so the run has to be matched before that filter.
        raw = (begin("sleep 5") + "sectape note 'x'\r\n" + end(0)
               + begin("sectape note 'x'") + "noted\r\n" + end(0))
        steps = parse_transcript(raw)
        self.assertEqual([s.cmd for s in steps], ["sleep 5"])
        self.assertEqual(steps[0].output, "")

    def test_output_that_merely_resembles_the_next_command_in_the_middle_stays(self):
        # Only a run at the very end is an echo. The same text earlier in the
        # output is something the command printed.
        raw = (begin("cat notes") + "echo hello\r\nand more after it\r\n" + end(0)
               + begin("echo hello") + "hello\r\n" + end(0))
        self.assertEqual(parse_transcript(raw)[0].output,
                         "echo hello\nand more after it")

    def test_an_unrelated_last_line_is_left_alone(self):
        raw = (begin("ls") + "notes.txt\r\n" + end(0)
               + begin("cat notes.txt") + "hello\r\n" + end(0))
        self.assertEqual(parse_transcript(raw)[0].output, "notes.txt")

    def test_the_last_command_in_a_log_keeps_its_output(self):
        raw = begin("echo done") + "done\r\n" + end(0)
        self.assertEqual(parse_transcript(raw)[0].output, "done")

    def test_a_scraped_transcript_is_left_alone(self):
        # Without markers the command text is itself read off the screen, so
        # there is no separate echo to remove and no exact text to trust.
        raw = "user@host:~$ sleep 3\r\nuser@host:~$ echo hi\r\nhi\r\n"
        steps = parse_transcript(raw)
        self.assertTrue(all(s.source == "heuristic" for s in steps))


class TestDeduplication(unittest.TestCase):
    def test_the_same_command_run_twice_is_kept(self):
        # Two marker pairs are proof the command really ran twice; only the
        # heuristic reader invents duplicates out of redrawn prompts.
        run = lambda t: Step(cmd="id", output="uid=0(root)", exit_code=0,
                             started=t, source="marker")
        self.assertEqual(len(dedupe_steps([run(1.0), run(2.0)])), 2)

    def test_heuristic_duplicates_are_still_collapsed(self):
        run = lambda: Step(cmd="id", output="uid=0(root)", source="heuristic")
        self.assertEqual(len(dedupe_steps([run(), run()])), 1)

    def test_a_marker_step_never_absorbs_a_heuristic_one(self):
        steps = [Step(cmd="id", output="x", source="marker"),
                 Step(cmd="id", output="x", source="heuristic")]
        self.assertEqual(len(dedupe_steps(steps)), 2)

    def test_blank_commands_are_dropped(self):
        self.assertEqual(dedupe_steps([Step(cmd="   ")]), [])


class TestRepeatedCommandsEndToEnd(TempConfig):
    def test_a_repeated_command_survives_collect_steps(self):
        raw = (begin("whoami", 100.0) + "root\r\n" + end(0, 100.1)
               + begin("whoami", 200.0) + "root\r\n" + end(0, 200.1))
        d = self.make_session("repeat", raw)
        steps, _ = collect_steps(d)
        self.assertEqual([s.cmd for s in steps], ["whoami", "whoami"])


class TestListedCountMatchesTheExport(TempConfig):
    """`sectape list` counts commands cheaply, without a full VT replay.

    The two readers must not disagree: a listing that says 3 commands and an
    export that contains 1 is a bug report waiting to happen.
    """

    def marked(self, *pairs) -> str:
        raw = ""
        for i, (cmd, code) in enumerate(pairs):
            raw += begin(cmd, 1700000000 + i) + "out\r\n"
            raw += end(code, 1700000000 + i + 0.5)
        return raw

    def assert_agree(self, name, *raws):
        d = self.make_session(name, *raws)
        steps, _ = collect_steps(d)
        self.assertEqual(count_commands(d), len(steps), name)

    def test_distinct_commands(self):
        self.assert_agree("plain", self.marked(("ls", 0), ("id", 0)))

    def test_a_command_repeated(self):
        self.assert_agree("repeat", self.marked(("id", 0), ("id", 0), ("id", 0)))

    def test_shell_plumbing_is_excluded_by_both(self):
        self.assert_agree("noise", self.marked(
            ("ls", 0), ("exit", 0), ("clear", 0), ("sectape note x", 0)))

    def test_across_panes(self):
        self.assert_agree("panes", self.marked(("ls", 0)),
                          self.marked(("id", 0), ("id", 0)))

    def test_heuristic_capture(self):
        self.assert_agree("heur",
                          "user@host:~$ ls\r\nfile\r\nuser@host:~$ id\r\nuid=0\r\n")

    def test_one_marked_pane_and_one_not(self):
        self.assert_agree("mixed", self.marked(("ls", 0)),
                          "user@host:~$ id\r\nuid=0\r\n")


class TestResizeDuringARecording(TempConfig):
    """The wrap column changes when the window does."""

    def test_the_replay_follows_a_resize(self):
        # Everything after the resize used to be replayed at the original
        # width, so a `\r` returned to the wrong row.
        raw = (size(40) + "narrow phase\r\n"
               + size(100) + "x" * 60 + "\rOVERWRITTEN\r\n")
        text = render_capture(raw)
        self.assertEqual(text.split("\n")[1],
                         "OVERWRITTEN" + "x" * 49)

    def test_without_a_resize_nothing_changes(self):
        raw = size(40) + "x" * 60 + "\rOVER\r\n"
        self.assertEqual(render_capture(raw), render_capture(raw))
        self.assertIn("OVER", render_capture(raw))

    def test_a_capture_with_no_size_marker_uses_the_default(self):
        self.assertEqual(render_capture("hello\r\n"), "hello\n")

    def test_a_malformed_size_marker_is_ignored(self):
        raw = size(40) + "\x1b]7337;SECTAPE;w|wide|tall\x07" + "ok\r\n"
        self.assertEqual(render_capture(raw), "ok\n")

    def test_a_resize_while_a_command_is_running_is_followed(self):
        raw = (size(40) + begin("run it", 100.0)
               + size(100) + "y" * 60 + "\rDONE\r\n"
               + end(0, 101.0))
        step = parse_transcript(raw)[0]
        self.assertTrue(step.output.startswith("DONE"), step.output[:40])

    def test_heuristic_parsing_follows_a_resize_too(self):
        raw = (size(40) + "user@host:~$ echo hi\r\n"
               + size(100) + "z" * 60 + "\rRESULT\r\n")
        steps = parse_heuristic_transcript(raw)
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].output.startswith("RESULT"), steps[0].output[:40])


class TestPromptShapes(TempConfig):
    """The fallback reader has to recognise the prompts people actually use."""

    def read(self, prompt: str):
        raw = (f"{prompt}echo hello\r\nhello\r\n"
               f"{prompt}id\r\nuid=0(root)\r\n{prompt}")
        return [(s.cmd, s.output) for s in parse_heuristic_transcript(raw)]

    def assert_reads(self, prompt: str, label: str):
        self.assertEqual(self.read(prompt),
                         [("echo hello", "hello"), ("id", "uid=0(root)")], label)

    def test_plain_user_at_host(self):
        self.assert_reads("user@host:~/code$ ", "plain")

    def test_a_coloured_prompt(self):
        self.assert_reads("\x1b[32muser@host\x1b[0m:\x1b[34m~/code\x1b[0m$ ",
                          "coloured")

    def test_root(self):
        self.assert_reads("root@box:/tmp# ", "root")

    def test_a_virtualenv_prefix(self):
        # `(venv) user@host:~$` matched nothing at all, so a session recorded
        # without shell integration came back completely empty.
        self.assert_reads("(venv) user@host:~$ ", "venv")

    def test_a_conda_prefix(self):
        self.assert_reads("(base) user@host:~/p$ ", "conda")

    def test_an_env_name_with_dots_and_dashes(self):
        self.assert_reads("(my-env-3.11) user@host:~$ ", "named env")

    def test_a_prompt_with_no_path(self):
        self.assert_reads("(venv) user@host$ ", "venv, no path")
        self.assert_reads("user@host$ ", "no path")

    def test_a_chevron_prompt(self):
        self.assert_reads("\x1b[34m~/code\x1b[0m \x1b[32m❯\x1b[0m ", "starship")
        self.assert_reads("❯ ", "bare chevron")

    def test_ordinary_output_is_not_read_as_a_command(self):
        for line in ("the cost is $5 for a@b:c",
                     "see user@example.com for details",
                     "(note) this is prose",
                     "Traceback (most recent call last):"):
            raw = f"user@host:~$ cat f\r\n{line}\r\n"
            steps = parse_heuristic_transcript(raw)
            self.assertEqual([s.cmd for s in steps], ["cat f"], line)


class TestSshInsideARecording(TempConfig):
    """The remote shell has no hooks, so its commands are the ssh step's output.

    The fallback reader is chosen per pane log: a log with any marked step
    never uses it, so an ssh session inside an ordinary recording is not read
    off the screen.
    """

    def capture(self) -> str:
        return (begin("ssh admin@box", 1700000000)
                + "Welcome to box\r\n"
                + "admin@box:~$ whoami\r\nadmin\r\n"
                + "admin@box:~$ cat /etc/hostname\r\nbox\r\n"
                + "admin@box:~$ exit\r\nlogout\r\n"
                + end(0, 1700000030)
                + begin("echo back-home", 1700000031) + "back-home\r\n"
                + end(0, 1700000032))

    def test_the_remote_commands_are_not_separate_steps(self):
        steps = parse_transcript(self.capture())
        self.assertEqual([s.cmd for s in steps],
                         ["ssh admin@box", "echo back-home"])

    def test_the_remote_session_is_kept_as_output(self):
        step = parse_transcript(self.capture())[0]
        for expected in ("whoami", "admin", "cat /etc/hostname", "box"):
            self.assertIn(expected, step.output)

    def test_the_local_command_after_it_is_unaffected(self):
        step = parse_transcript(self.capture())[1]
        self.assertEqual(step.cmd, "echo back-home")
        self.assertEqual(step.output, "back-home")
        self.assertEqual(step.source, "marker")

    def test_a_log_with_no_markers_at_all_does_use_the_screen_reader(self):
        raw = "admin@box:~$ whoami\r\nadmin\r\n"
        steps = parse_transcript(raw)
        self.assertEqual([s.cmd for s in steps], ["whoami"])
        self.assertEqual(steps[0].source, "heuristic")


class TestFullScreenProgramsInAPipeline(TempConfig):
    """Piping into a pager is how most people meet one."""

    def output_for(self, cmd: str) -> str:
        raw = begin(cmd, 1.0) + "MANGLED SCREEN REDRAW\r\n" + end(0, 2.0)
        return parse_transcript(raw)[0].output

    def assert_summarised(self, cmd: str, program: str):
        self.assertEqual(self.output_for(cmd),
                         f"<interactive {program} session - "
                         "screen output not recorded>", cmd)

    def test_a_pager_on_its_own(self):
        self.assert_summarised("less /var/log/syslog", "less")

    def test_a_pager_at_the_end_of_a_pipeline(self):
        # git and systemd both set LESS=...X, so the pager stays on the
        # primary screen and its redraw really does reach the export.
        self.assert_summarised("cat /var/log/syslog | less", "less")
        self.assert_summarised("git log --oneline | less -X", "less")
        self.assert_summarised("journalctl -u nginx | less", "less")
        self.assert_summarised("dmesg | more", "more")

    def test_an_editor_at_the_end_of_a_pipeline(self):
        self.assert_summarised("ps aux | vim -", "vim")

    def test_an_ordinary_pipeline_keeps_its_output(self):
        self.assertEqual(self.output_for("cat f | grep x"),
                         "MANGLED SCREEN REDRAW")

    def test_the_name_as_an_argument_does_not_count(self):
        self.assertEqual(self.output_for("echo hi | grep less"),
                         "MANGLED SCREEN REDRAW")

    def test_the_name_inside_quotes_does_not_count(self):
        self.assertEqual(self.output_for("echo 'pipe to less' | wc -l"),
                         "MANGLED SCREEN REDRAW")


class TestIgnoringPlumbingOnCompoundLines(TempConfig):
    """A line is only plumbing if everything on it is."""

    def kept(self, cmd: str) -> bool:
        raw = begin(cmd, 1.0) + "out\r\n" + end(0, 2.0)
        return bool(parse_transcript(raw))

    def test_a_note_on_its_own_is_dropped(self):
        self.assertFalse(self.kept("sectape note 'checkpoint'"))
        self.assertFalse(self.kept("note 'checkpoint'"))

    def test_a_note_sharing_a_line_with_real_work_keeps_the_work(self):
        # The whole line used to be dropped, taking the command with it.
        self.assertTrue(self.kept("sectape note 'checkpoint'; nmap -sV host"))
        self.assertTrue(self.kept("note 'done' && systemctl restart app"))

    def test_shell_plumbing_alone_is_dropped(self):
        for cmd in ("exit", "clear", "clear; exit", "clear && exit"):
            self.assertFalse(self.kept(cmd), cmd)

    def test_plumbing_followed_by_work_is_kept(self):
        self.assertTrue(self.kept("clear && ls -la"))
        self.assertTrue(self.kept("cd /tmp; nmap -sV host"))

    def test_an_ordinary_pipeline_is_kept(self):
        self.assertTrue(self.kept("cat f | grep x"))

    def test_the_listing_count_still_agrees_with_the_export(self):
        raw = "".join(
            begin(cmd, 1700000000 + i) + "out\r\n" + end(0, 1700000000 + i + 0.5)
            for i, cmd in enumerate([
                "nmap -sV host", "sectape note 'x'", "clear; exit",
                "note 'y' && systemctl restart app", "cat f | grep x"]))
        d = self.make_session("mixed-plumbing", raw)
        steps, _ = collect_steps(d)
        self.assertEqual(count_commands(d), len(steps))
        self.assertEqual(len(steps), 3)


class TestLogsFromThePreviousName(TempConfig):
    """Recordings made before the tool was renamed still have to open.

    Their markers carry the old `THM` payload tag and pre-3.0 logs have no
    size marker at all, because that recorder never sized its pty.
    """

    def legacy_log(self):
        raw = ""
        for i, (cmd, code) in enumerate([("nmap -sV 10.10.1.1", 0),
                                         ("cat /etc/passwd | grep root", 0),
                                         ("hydra -l admin ssh://box", 1)]):
            raw += legacy_marker("b", b64(cmd), str(1600000000 + i * 10))
            raw += f"output of {cmd}\r\n"
            raw += legacy_marker("e", str(code), str(1600000000 + i * 10 + 2),
                                 b64("/root"))
        return raw

    def test_the_commands_and_exit_codes_are_read(self):
        d = self.make_session("legacy", self.legacy_log())
        steps, _ = collect_steps(d)
        self.assertEqual([s.cmd for s in steps],
                         ["nmap -sV 10.10.1.1", "cat /etc/passwd | grep root",
                          "hydra -l admin ssh://box"])
        self.assertEqual([s.exit_code for s in steps], [0, 0, 1])
        self.assertTrue(all(s.source == "marker" for s in steps))

    def test_durations_are_read(self):
        d = self.make_session("legacy", self.legacy_log())
        steps, _ = collect_steps(d)
        self.assertTrue(all(s.duration == 2.0 for s in steps))

    def test_a_log_with_no_size_marker_still_replays(self):
        # Those recordings were always made at 80 columns.
        raw = legacy_marker("b", b64("echo hi"), "1.0") + "hi\r\n" \
            + legacy_marker("e", "0", "1.2", b64("/root"))
        self.assertEqual(parse_transcript(raw)[0].output, "hi")

    def test_the_listing_count_agrees(self):
        d = self.make_session("legacy", self.legacy_log())
        steps, _ = collect_steps(d)
        self.assertEqual(count_commands(d), len(steps))

    def test_a_pipeline_in_an_old_log_is_read_whole(self):
        from sectape.cli import _load_recording
        d = self.make_session("legacy", self.legacy_log())
        self.assertEqual(_load_recording(d).programs(),
                         ["nmap", "cat", "grep", "hydra"])

    def test_old_and_new_panes_in_one_session(self):
        # A directory can hold a pane from before the rename and one from
        # after, if the label was reused.
        new = begin("systemctl status app", 1600000100.0) + "active\r\n" \
            + end(0, 1600000101.0)
        d = self.make_session("mixed-era", self.legacy_log(), new)
        steps, panes = collect_steps(d)
        self.assertEqual(panes, 2)
        self.assertIn("systemctl status app", [s.cmd for s in steps])
        self.assertIn("nmap -sV 10.10.1.1", [s.cmd for s in steps])
        self.assertEqual(count_commands(d), len(steps))


if __name__ == "__main__":
    unittest.main()
