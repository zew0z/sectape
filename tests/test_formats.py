import json
import unittest
from pathlib import Path

from sectape import config
from sectape.formats import (GEN_BEGIN, GEN_END, Recording, export, merge,
                             render, to_json, to_markdown, to_text)
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


class TestRecordingSummary(unittest.TestCase):
    def test_busy_and_wall_time(self):
        r = rec(OK_STEP, BAD_STEP, started=1700000000.0, ended=1700000010.0)
        self.assertAlmostEqual(r.busy_time, 0.26, places=6)
        self.assertAlmostEqual(r.wall_time, 10.0, places=6)

    def test_wall_time_falls_back_to_step_stamps(self):
        r = rec(OK_STEP, BAD_STEP)
        self.assertGreater(r.wall_time, 0)

    def test_programs_preserve_first_use_order(self):
        r = rec(Step(cmd="nmap a"), Step(cmd="curl b"), Step(cmd="nmap c"))
        self.assertEqual(r.programs(), ["nmap", "curl"])


if __name__ == "__main__":
    unittest.main()
