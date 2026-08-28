import json
import unittest

from sectape import config
from sectape.formats import Recording, to_html, to_json, to_markdown, to_text
from sectape.session import add_note, read_notes
from sectape.text import trim_for_export
from sectape.transcript import Step
from sectape.util import write_json_atomic
from tests.helpers import TempConfig


class TestAnnotationStore(TempConfig):
    def test_note_needs_an_active_session(self):
        self.assertIsNone(add_note("orphan"))

    def test_note_is_written_and_read_back(self):
        session_dir = self.make_session("s")
        write_json_atomic(config.settings.current_session_file,
                          {"label": "s", "slug": "s", "dir": str(session_dir)})
        add_note("first thought", when=10.0)
        add_note("second thought", when=20.0)
        notes = read_notes(session_dir)
        self.assertEqual([n["text"] for n in notes], ["first thought", "second thought"])

    def test_notes_are_ordered_by_time(self):
        d = self.make_session("s")
        add_note("later", d, when=99.0)
        add_note("earlier", d, when=1.0)
        self.assertEqual([n["text"] for n in read_notes(d)], ["earlier", "later"])

    def test_blank_notes_rejected(self):
        d = self.make_session("s")
        self.assertIsNone(add_note("   ", d))
        self.assertEqual(read_notes(d), [])

    def test_corrupt_lines_skipped(self):
        d = self.make_session("s")
        add_note("good", d, when=1.0)
        with (d / "notes.jsonl").open("a") as fh:
            fh.write("not json\n{}\n")
        self.assertEqual([n["text"] for n in read_notes(d)], ["good"])

    def test_missing_file_is_empty(self):
        self.assertEqual(read_notes(self.root / "nowhere"), [])

    def test_multiline_note_preserved(self):
        d = self.make_session("s")
        add_note("line one\nline two", d, when=1.0)
        self.assertEqual(read_notes(d)[0]["text"], "line one\nline two")


def steps_at(*times):
    return [Step(cmd=f"cmd{i}", output="out", exit_code=0, started=t, source="marker")
            for i, t in enumerate(times, 1)]


class TestTimeline(TempConfig):
    def test_notes_interleave_between_commands(self):
        rec = Recording("r", steps_at(10.0, 30.0), 1,
                        notes=[{"at": 20.0, "text": "in between"}])
        kinds = [kind for kind, _ in rec.timeline()]
        self.assertEqual(kinds, ["step", "note", "step"])

    def test_note_before_everything(self):
        rec = Recording("r", steps_at(10.0), 1, notes=[{"at": 1.0, "text": "early"}])
        self.assertEqual([k for k, _ in rec.timeline()], ["note", "step"])

    def test_note_after_everything(self):
        rec = Recording("r", steps_at(10.0), 1, notes=[{"at": 99.0, "text": "late"}])
        self.assertEqual([k for k, _ in rec.timeline()], ["step", "note"])

    def test_timeline_without_timestamps_keeps_order(self):
        rec = Recording("r", [Step(cmd="a"), Step(cmd="b")], 1,
                        notes=[{"at": 0.0, "text": "n"}])
        kinds = [k for k, _ in rec.timeline()]
        self.assertEqual(kinds.count("step"), 2)
        self.assertEqual(kinds.count("note"), 1)

    def test_every_step_appears_exactly_once(self):
        rec = Recording("r", steps_at(1.0, 2.0, 3.0), 1,
                        notes=[{"at": 1.5, "text": "x"}, {"at": 2.5, "text": "y"}])
        found = [item.cmd for kind, item in rec.timeline() if kind == "step"]
        self.assertEqual(found, ["cmd1", "cmd2", "cmd3"])


class TestNotesInExports(TempConfig):
    def rec(self):
        return Recording("r", steps_at(10.0, 30.0), 1,
                         notes=[{"at": 20.0, "text": "why I did this"}])

    def test_markdown_blockquote(self):
        text = to_markdown(self.rec())
        self.assertIn("> **note**", text)
        self.assertIn("> why I did this", text)
        self.assertIn("- **Notes**: 1", text)

    def test_markdown_numbers_only_commands(self):
        text = to_markdown(self.rec())
        self.assertIn("### 1. `cmd1`", text)
        self.assertIn("### 2. `cmd2`", text)
        self.assertNotIn("### 3.", text)

    def test_text_format(self):
        self.assertIn("# why I did this", to_text(self.rec()))

    def test_json_carries_notes(self):
        payload = json.loads(to_json(self.rec()))
        self.assertEqual(payload["notes"][0]["text"], "why I did this")

    def test_html_escapes_note_text(self):
        rec = Recording("r", [], 1, notes=[{"at": 1.0, "text": "<script>alert(1)</script>"}])
        html_out = to_html(rec)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)


class TestPaneAttribution(TempConfig):
    def test_pane_shown_only_when_several(self):
        step = Step(cmd="ls", output="", exit_code=0, started=1.0, pane="07")
        single = Recording("r", [step], panes=1)
        multi = Recording("r", [step], panes=2)
        self.assertNotIn("pane 7", to_markdown(single))
        self.assertIn("pane 7", to_markdown(multi))

    def test_collect_steps_records_the_pane(self):
        from sectape.transcript import collect_steps
        from tests.helpers import begin, end
        d = self.sessions / "p"
        d.mkdir(parents=True)
        (d / "pane_42.raw").write_text(begin("ls", 1.0) + "a\r\n" + end(0, 1.1))
        steps, _ = collect_steps(d)
        self.assertEqual(steps[0].pane, "42")


class TestNotesAreRedactedInExports(TempConfig):
    """A note lands in the same shared document as the transcript."""

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def load(self, session_dir):
        from sectape.cli import _load_recording
        return _load_recording(session_dir)

    def setUp(self):
        super().setUp()
        self.dir = self.make_session("secrets")
        add_note(f"reminder: the key is {self.SECRET}", self.dir, when=1700000000.0)

    def test_the_secret_is_scrubbed_from_the_export(self):
        text = to_markdown(self.load(self.dir))
        self.assertNotIn(self.SECRET, text)
        self.assertIn("<REDACTED: aws key id>", text)

    def test_the_notes_file_itself_is_untouched(self):
        # Redaction applies to exports, not to the raw record.
        self.assertIn(self.SECRET, (self.dir / "notes.jsonl").read_text())

    def test_no_redact_keeps_the_note_verbatim(self):
        config.override(redact=False)
        self.assertIn(self.SECRET, to_markdown(self.load(self.dir)))

    def test_every_writer_scrubs_it(self):
        rec = self.load(self.dir)
        for name, writer in (("markdown", to_markdown), ("text", to_text),
                             ("html", to_html), ("json", to_json)):
            self.assertNotIn(self.SECRET, writer(rec), name)


class TestLargeNotesAreBoundedInExports(TempConfig):
    """`cat big.log | sectape note` must not make the document unusable."""

    def load(self, session_dir):
        from sectape.cli import _load_recording
        return _load_recording(session_dir)

    def setUp(self):
        super().setUp()
        self.dir = self.make_session("huge")
        self.huge = "\n".join(f"log line {i} padding" for i in range(20000))
        add_note(self.huge, self.dir, when=1700000000.0)
        add_note("a short note", self.dir, when=1700000001.0)

    def test_the_export_is_bounded(self):
        text = to_markdown(self.load(self.dir))
        self.assertLess(len(text), 60000, "the whole file went into the export")
        self.assertIn("SNIP", text, "the export does not say what it left out")

    def test_every_writer_bounds_it(self):
        rec = self.load(self.dir)
        for name, writer in (("markdown", to_markdown), ("text", to_text),
                             ("html", to_html), ("json", to_json)):
            self.assertLess(len(writer(rec)), 60000, name)

    def test_a_short_note_is_untouched(self):
        self.assertIn("a short note", to_markdown(self.load(self.dir)))

    def test_the_notes_file_keeps_the_note_in_full(self):
        # Trimming is a property of the document, not of the record.
        raw = (self.dir / "notes.jsonl").read_text()
        self.assertGreater(len(raw), len(self.huge))
        self.assertEqual(len(read_notes(self.dir)), 2)
        self.assertEqual(read_notes(self.dir)[0]["text"], self.huge)

    def test_the_limits_are_the_configured_ones(self):
        config.override(max_output_lines=5, max_output_chars=400)
        text = to_markdown(self.load(self.dir))
        self.assertLess(len(text), 2000)


class TestTrimForExport(TempConfig):
    def test_short_text_is_returned_unchanged(self):
        self.assertEqual(trim_for_export("one\ntwo"), "one\ntwo")

    def test_empty_text(self):
        self.assertEqual(trim_for_export(""), "")

    def test_lines_are_capped_with_a_head_and_a_tail(self):
        text = "\n".join(str(i) for i in range(1000))
        out = trim_for_export(text, max_lines=10, max_chars=100000)
        self.assertIn("<SNIP: 990 more lines>", out)
        self.assertTrue(out.startswith("0\n"))
        self.assertTrue(out.endswith("999"))

    def test_characters_are_capped(self):
        out = trim_for_export("x" * 5000, max_lines=1000, max_chars=100)
        self.assertLess(len(out), 200)
        self.assertIn("truncated at 100 chars", out)

    def test_carriage_returns_are_normalised(self):
        self.assertEqual(trim_for_export("a\r\nb\rc"), "a\nb\nc")


if __name__ == "__main__":
    unittest.main()
