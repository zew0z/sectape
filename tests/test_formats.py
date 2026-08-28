import json
import os
import unittest
from pathlib import Path

from sectape import config
from sectape.formats import (GEN_BEGIN, GEN_END, Recording, export, merge,
                             render, to_html, to_json, to_markdown, to_text)
from sectape.transcript import Step
from tests.helpers import TempConfig


def rec(*steps, label="demo", panes=1, **kw):
    return Recording(label=label, steps=list(steps), panes=panes, **kw)


OK_STEP = Step(cmd="echo hi", output="hi", exit_code=0, cwd="/home/u",
               started=1700000000.0, duration=0.25, source="marker")
BAD_STEP = Step(cmd="cat missing", output="No such file", exit_code=1,
                cwd="/home/u", started=1700000001.0, duration=0.01, source="marker")


class TestMarkdown(TempConfig):
    def test_structure(self):
        text = to_markdown(rec(OK_STEP, BAD_STEP))
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("type: terminal-capture", text)
        self.assertIn("commands: 2", text)
        self.assertIn("failed: 1", text)
        self.assertIn("# demo", text)
        self.assertIn(GEN_BEGIN, text)
        self.assertIn(GEN_END, text)

    def test_step_details(self):
        text = to_markdown(rec(BAD_STEP))
        self.assertIn("### 1. `cat missing` ⚠️", text)
        self.assertIn("**exit 1**", text)
        self.assertIn("`/home/u`", text)
        self.assertIn("No such file", text)

    def test_prompt_is_configurable(self):
        config.override(prompt="zero ❯")
        self.assertIn("zero ❯ echo hi", to_markdown(rec(OK_STEP)))

    def test_programs_summary_skips_shell_plumbing(self):
        steps = [Step(cmd="echo hi"), Step(cmd="cd /tmp"), Step(cmd="nmap -sV h"),
                 Step(cmd="cat f"), Step(cmd="for i in 1 2; do echo $i; done"),
                 Step(cmd="if true; then echo y; fi")]
        text = to_markdown(rec(*steps))
        line = [l for l in text.split("\n") if l.startswith("- **Programs**")][0]
        self.assertIn("`nmap`", line)
        self.assertIn("`cat`", line)
        self.assertNotIn("`echo`", line)
        self.assertNotIn("`cd`", line)
        self.assertNotIn("`for`", line)
        self.assertNotIn("`if`", line)

    def test_empty_recording(self):
        text = to_markdown(rec())
        self.assertIn("No commands were captured", text)
        self.assertIn(GEN_END, text)

    def test_heuristic_recordings_are_labelled(self):
        text = to_markdown(rec(Step(cmd="ls", source="heuristic")))
        self.assertIn("Reconstructed transcript", text)

    def test_marker_recordings_are_not_labelled(self):
        self.assertNotIn("Reconstructed", to_markdown(rec(OK_STEP)))

    def test_no_escape_sequences_leak(self):
        step = Step(cmd="ls", output="plain", exit_code=0)
        self.assertNotIn("\x1b", to_markdown(rec(step)))


class TestJsonAndText(TempConfig):
    def test_json_roundtrips(self):
        payload = json.loads(to_json(rec(OK_STEP, BAD_STEP)))
        self.assertEqual(payload["commands"], 2)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["steps"][1]["exit_code"], 1)
        self.assertEqual(payload["steps"][0]["cmd"], "echo hi")

    def test_json_programs(self):
        payload = json.loads(to_json(rec(Step(cmd="nmap x"), Step(cmd="echo y"))))
        self.assertEqual(payload["programs"], ["nmap"])

    def test_text_is_prompt_and_output(self):
        config.override(prompt="$")
        out = to_text(rec(OK_STEP, BAD_STEP))
        self.assertIn("$ echo hi", out)
        self.assertIn("$ cat missing [exit 1]", out)
        self.assertIn("hi", out)

    def test_render_picks_extension(self):
        for fmt, suffix in (("markdown", ".md"), ("json", ".json"), ("text", ".txt")):
            _, ext = render(rec(OK_STEP), fmt)
            self.assertEqual(ext, suffix)

    def test_unknown_format_raises(self):
        with self.assertRaises(config.ConfigError):
            render(rec(OK_STEP), "postscript")


class TestExport(TempConfig):
    def test_writes_into_the_output_directory(self):
        path = export(rec(OK_STEP, label="my session"))
        self.assertEqual(path.parent, config.settings.output_dir)
        self.assertEqual(path.name, "my session.md")
        self.assertIn("echo hi", path.read_text())

    def test_explicit_destination(self):
        dest = self.root / "elsewhere" / "out.json"
        path = export(rec(OK_STEP), "json", dest)
        self.assertEqual(path, dest)
        json.loads(path.read_text())

    def test_label_cannot_escape_the_output_directory(self):
        path = export(rec(OK_STEP, label="../../../../tmp/pwned"))
        self.assertEqual(path.parent, config.settings.output_dir)
        self.assertFalse(Path("/tmp/pwned.md").exists())

    def test_no_temp_files_left_behind(self):
        export(rec(OK_STEP, label="clean"))
        names = sorted(p.name for p in config.settings.output_dir.iterdir())
        self.assertEqual(names, ["clean.md"])


class TestMerge(TempConfig):
    def setUp(self):
        super().setUp()
        self.path = self.root / "note.md"

    def test_new_file_written_verbatim(self):
        self.assertEqual(merge("hello", self.path), "hello")

    def test_generated_block_replaced_in_place(self):
        self.path.write_text(f"HEAD\n{GEN_BEGIN}\nOLD\n{GEN_END}\n## My notes\nkeep me\n")
        merged = merge(f"NEWHEAD\n{GEN_BEGIN}\nNEW\n{GEN_END}\n", self.path)
        self.assertIn("NEW", merged)
        self.assertNotIn("OLD", merged)
        self.assertIn("keep me", merged)
        self.assertTrue(merged.startswith("HEAD"))

    def test_rebuild_is_idempotent(self):
        new = f"H\n{GEN_BEGIN}\nBODY\n{GEN_END}\n"
        self.path.write_text(merge(new, self.path))
        once = self.path.read_text()
        self.path.write_text(merge(new, self.path))
        self.assertEqual(once, self.path.read_text())

    def test_handwritten_notes_survive_a_re_export(self):
        path = export(rec(OK_STEP, label="keep"))
        path.write_text(path.read_text() + "\n## Follow-up\n\n- ask about the cert\n")
        export(rec(OK_STEP, BAD_STEP, label="keep"))
        text = path.read_text()
        self.assertIn("ask about the cert", text)
        self.assertIn("cat missing", text)

    def test_file_without_markers_is_replaced(self):
        self.path.write_text("no markers here")
        self.assertEqual(merge("fresh", self.path), "fresh")


class TestMarkdownEscaping(TempConfig):
    """A recording of someone reading markdown must still be markdown."""

    def test_output_with_a_code_fence_does_not_close_the_block(self):
        step = Step(cmd="cat snippet.md", output="```\nprint(1)\n```",
                    exit_code=0, source="marker")
        text = to_markdown(rec(step))
        self.assertIn("````console", text)
        # the block opens and closes with the longer fence, once each
        self.assertEqual(text.count("````"), 2)

    def test_a_longer_run_of_backticks_gets_a_longer_fence(self):
        step = Step(cmd="cat x", output="````\nstuff\n````", exit_code=0,
                    source="marker")
        self.assertIn("`````console", to_markdown(rec(step)))

    def test_ordinary_output_still_uses_three_backticks(self):
        self.assertIn("```console", to_markdown(rec(OK_STEP)))

    def test_a_command_containing_backticks_stays_inline_code(self):
        step = Step(cmd="echo `date`", exit_code=0, source="marker")
        text = to_markdown(rec(step))
        # CommonMark: a longer delimiter, and symmetric padding because the
        # content ends with a backtick.
        self.assertIn("### 1. `` echo `date` ``", text)

    def test_merge_survives_output_containing_the_end_marker(self):
        # `cat`ting an older export captures the marker as ordinary output.
        # Splitting on the first one truncated the document there.
        steps = [Step(cmd="cat old.md", output=f"text\n{GEN_END}\nmore",
                      exit_code=0, source="marker"),
                 Step(cmd="echo tail-step", output="tail-step", exit_code=0,
                      source="marker")]
        text = to_markdown(rec(*steps))
        path = self.root / "note.md"
        path.write_text(f"HEADER\n{GEN_BEGIN}\nold\n{GEN_END}\nFOOTER\n",
                        encoding="utf-8")
        merged = merge(text, path)
        self.assertIn("tail-step", merged, "steps after the marker were dropped")
        self.assertTrue(merged.startswith("HEADER"))
        self.assertIn("FOOTER", merged)


class TestReExportRefreshesTheSummary(TempConfig):
    """The summary is derived, so it has to be regenerated with the body."""

    def steps(self, n):
        return [Step(cmd=f"echo {i}", output=str(i), exit_code=0,
                     started=1700000000.0 + i, duration=0.1, source="marker")
                for i in range(n)]

    def test_a_second_export_updates_the_counts(self):
        # The summary used to live outside the regenerated block, so a header
        # claiming one command sat above a body listing four, forever.
        export(rec(*self.steps(1), started=1700000000.0))
        path = export(rec(*self.steps(4), started=1700000000.0))
        text = path.read_text()
        self.assertIn("- **Commands**: 4", text)
        self.assertNotIn("- **Commands**: 1", text)

    def test_a_second_export_updates_the_frontmatter(self):
        export(rec(*self.steps(1), started=1700000000.0))
        path = export(rec(*self.steps(4), started=1700000000.0))
        self.assertIn("commands: 4", path.read_text())

    def test_prose_around_the_block_is_still_preserved(self):
        new = to_markdown(rec(*self.steps(2), started=1700000000.0))
        path = self.root / "note.md"
        path.write_text(
            f"---\ntype: terminal-capture\ncommands: 1\n---\n\n# demo\n\n"
            f"MY INTRO\n\n{GEN_BEGIN}\nold\n{GEN_END}\n\nMY CONCLUSION\n",
            encoding="utf-8")
        merged = merge(new, path)
        self.assertIn("MY INTRO", merged)
        self.assertIn("MY CONCLUSION", merged)
        self.assertIn("- **Commands**: 2", merged)

    def test_a_summary_left_by_the_old_layout_is_cleaned_up(self):
        new = to_markdown(rec(*self.steps(2), started=1700000000.0))
        path = self.root / "old.md"
        path.write_text(
            f"---\ntype: terminal-capture\ncommands: 1\n---\n\n# demo\n\n"
            f"- **Commands**: 1\n- **Shell**: `/bin/zsh`\n\n"
            f"{GEN_BEGIN}\nold\n{GEN_END}\n", encoding="utf-8")
        merged = merge(new, path)
        self.assertEqual(merged.count("- **Commands**"), 1)
        self.assertIn("- **Commands**: 2", merged)

    def test_a_readers_own_bullet_list_is_not_eaten(self):
        new = to_markdown(rec(*self.steps(2), started=1700000000.0))
        path = self.root / "prose.md"
        path.write_text(
            f"# demo\n\n- **Ticket**: OPS-1234\n- **Reviewer**: someone\n\n"
            f"{GEN_BEGIN}\nold\n{GEN_END}\n", encoding="utf-8")
        merged = merge(new, path)
        self.assertIn("- **Ticket**: OPS-1234", merged)
        self.assertIn("- **Reviewer**: someone", merged)


class TestNotesOnlyRecording(TempConfig):
    """Notes alone are a recording worth keeping."""

    NOTE = [{"at": 1700000000.0, "text": "the important insight"}]

    def test_every_writer_keeps_a_note_with_no_commands(self):
        # markdown announced "Notes: 1" and then dropped it.
        r = rec(notes=self.NOTE)
        for name, writer in (("markdown", to_markdown), ("text", to_text),
                             ("html", to_html), ("json", to_json)):
            self.assertIn("the important insight", writer(r), name)

    def test_markdown_does_not_claim_nothing_was_captured(self):
        self.assertNotIn("No commands were captured",
                         to_markdown(rec(notes=self.NOTE)))

    def test_a_truly_empty_recording_still_says_so(self):
        self.assertIn("No commands were captured", to_markdown(rec()))

    def test_no_reconstructed_warning_without_steps(self):
        self.assertNotIn("Reconstructed", to_markdown(rec(notes=self.NOTE)))


class TestExportPermissions(TempConfig):
    """An export is a document to hand to someone, not a private file."""

    def mode(self, path) -> int:
        return path.stat().st_mode & 0o777

    def test_a_new_export_follows_the_umask(self):
        # Atomic writes go through mkstemp, which always creates 0600, so
        # exports came out unshareable.
        previous = os.umask(0o022)
        try:
            path = export(rec(OK_STEP))
            self.assertEqual(self.mode(path), 0o644)
        finally:
            os.umask(previous)

    def test_a_restrictive_umask_is_respected(self):
        previous = os.umask(0o077)
        try:
            path = export(rec(OK_STEP, label="tight"))
            self.assertEqual(self.mode(path), 0o600)
        finally:
            os.umask(previous)

    def test_an_existing_file_keeps_the_permissions_it_was_given(self):
        previous = os.umask(0o022)
        try:
            path = export(rec(OK_STEP, label="chosen"))
            path.chmod(0o640)
            export(rec(OK_STEP, BAD_STEP, label="chosen"))
            self.assertEqual(self.mode(path), 0o640)
        finally:
            os.umask(previous)

    def test_the_state_tree_is_unaffected(self):
        from sectape.util import write_json_atomic
        previous = os.umask(0o022)
        try:
            target = self.root / "state.json"
            write_json_atomic(target, {"a": 1})
            self.assertEqual(self.mode(target), 0o600)
        finally:
            os.umask(previous)


class TestTextHeader(TempConfig):
    def test_one_command_is_singular(self):
        self.assertIn("(1 command, 0 failed)", to_text(rec(OK_STEP)))

    def test_several_commands_are_plural(self):
        self.assertIn("(2 commands, 1 failed)", to_text(rec(OK_STEP, BAD_STEP)))


class TestRecordingSummary(unittest.TestCase):
    def test_busy_and_wall_time(self):
        r = rec(OK_STEP, BAD_STEP, started=1700000000.0, ended=1700000010.0)
        self.assertAlmostEqual(r.busy_time, 0.26, places=6)
        self.assertAlmostEqual(r.wall_time, 10.0, places=6)

    def test_wall_time_falls_back_to_step_stamps(self):
        r = rec(OK_STEP, BAD_STEP)
        self.assertGreater(r.wall_time, 0)

    def test_wall_time_of_an_old_recording_is_not_time_since_it_started(self):
        # Exporting a week-old session reported an elapsed time of days,
        # because "ended" defaulted to the moment of the export.
        import time
        long_ago = time.time() - 7 * 86400
        r = rec(Step(cmd="echo hi", started=long_ago, duration=2.0,
                     source="marker"),
                started=long_ago - 1.0)
        self.assertAlmostEqual(r.wall_time, 3.0, places=3)
        self.assertAlmostEqual(r.finished, long_ago + 2.0, places=3)

    def test_finished_falls_back_to_the_last_note(self):
        import time
        long_ago = time.time() - 86400
        r = rec(started=long_ago, notes=[{"at": long_ago + 5.0, "text": "n"}])
        self.assertAlmostEqual(r.wall_time, 5.0, places=3)

    def test_empty_old_recording_has_no_elapsed_time(self):
        import time
        r = rec(started=time.time() - 86400)
        self.assertEqual(r.wall_time, 0.0)

    def test_explicit_end_still_wins(self):
        r = rec(OK_STEP, started=1700000000.0, ended=1700000010.0)
        self.assertAlmostEqual(r.finished, 1700000010.0, places=6)

    def test_json_reports_the_derived_end(self):
        r = rec(OK_STEP, started=1699999999.0)
        self.assertAlmostEqual(json.loads(to_json(r))["ended"],
                               1700000000.25, places=6)

    def test_elapsed_covers_the_content_not_just_this_session(self):
        # Recording again under an existing label appends to it, so steps can
        # predate the session being written. Measuring from the later start
        # reported two seconds for a document spanning two days.
        monday = Step(cmd="echo monday", started=1700000000.0, duration=2.0,
                      source="marker")
        tuesday = Step(cmd="echo tuesday", started=1700086400.0, duration=2.0,
                       source="marker")
        r = rec(monday, tuesday, started=1700086400.0)      # Tuesday's start
        self.assertAlmostEqual(r.began, 1700000000.0, places=3)
        self.assertAlmostEqual(r.wall_time, 86402.0, places=3)

    def test_began_is_the_session_start_when_nothing_predates_it(self):
        r = rec(OK_STEP, started=1699999999.0)
        self.assertAlmostEqual(r.began, 1699999999.0, places=3)

    def test_began_falls_back_to_the_steps(self):
        self.assertAlmostEqual(rec(OK_STEP).began, OK_STEP.started, places=3)

    def test_a_note_before_the_session_start_counts(self):
        r = rec(OK_STEP, started=1700000000.0,
                notes=[{"at": 1699999000.0, "text": "earlier"}])
        self.assertAlmostEqual(r.began, 1699999000.0, places=3)

    def test_an_empty_recording_has_no_elapsed_time(self):
        self.assertEqual(rec().wall_time, 0.0)

    def test_programs_preserve_first_use_order(self):
        r = rec(Step(cmd="nmap a"), Step(cmd="curl b"), Step(cmd="nmap c"))
        self.assertEqual(r.programs(), ["nmap", "curl"])


if __name__ == "__main__":
    unittest.main()
