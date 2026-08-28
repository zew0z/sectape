import os
import time
import unittest

from sectape import config
from sectape.session import (add_note, allocate_pane, clear_session_if_idle,
                             ensure_session_dir, live_panes,
                             pane_label, prune_dead_panes, read_notes,
                             read_session, register_pane, resolve_session_dir,
                             read_session_meta, signal_panes, unregister_pane,
                             wait_for_panes, write_session_meta)
from sectape.util import write_json_atomic
from tests.helpers import TempConfig


class TestPaneRegistry(TempConfig):
    def write_session(self, panes=None):
        write_json_atomic(config.settings.current_session_file,
                          {"slug": "s", "label": "s", "panes": panes or {}})

    def test_register_and_unregister(self):
        self.write_session()
        register_pane("111", self.root / "p.raw")
        self.assertIn("111", read_session()["panes"])
        self.assertEqual(unregister_pane("111"), 0)
        self.assertEqual(read_session()["panes"], {})

    def test_dead_panes_pruned(self):
        self.write_session({"dead": {"pid": 999999, "log": "x"}})
        register_pane("live", self.root / "p.raw")
        panes = read_session()["panes"]
        self.assertNotIn("dead", panes)
        self.assertIn("live", panes)

    def test_finished_session_is_not_resurrected(self):
        self.assertEqual(unregister_pane("ghost"), 0)
        self.assertFalse(config.settings.current_session_file.exists())

    def test_clear_only_when_idle(self):
        self.write_session({"a": {"pid": os.getpid(), "log": "x"}})
        self.assertFalse(clear_session_if_idle())
        self.assertTrue(config.settings.current_session_file.exists())
        unregister_pane("a")
        self.assertTrue(clear_session_if_idle())
        self.assertFalse(config.settings.current_session_file.exists())

    def test_prune_helper(self):
        data = {"panes": {"live": {"pid": os.getpid()}, "dead": {"pid": 999999}}}
        self.assertEqual(list(prune_dead_panes(data)), ["live"])

    def test_panes_are_numbered_from_one(self):
        session_dir = self.sessions / "s"
        first, first_log = allocate_pane(session_dir)
        self.assertEqual(first, "01")
        self.assertEqual(first_log.name, "pane_01.raw")
        first_log.write_text("x")
        second, second_log = allocate_pane(session_dir)
        self.assertEqual(second, "02")
        self.assertEqual(second_log.name, "pane_02.raw")

    def test_pane_numbers_fill_gaps_left_by_closed_panes(self):
        session_dir = self.sessions / "s2"
        session_dir.mkdir(parents=True)
        (session_dir / "pane_01.raw").write_text("x")
        (session_dir / "pane_03.raw").write_text("x")
        pane_id, _ = allocate_pane(session_dir)
        self.assertEqual(pane_id, "02")

    def test_pane_label_is_human(self):
        self.assertEqual(pane_label("01"), "1")
        self.assertEqual(pane_label("12"), "12")

    def test_live_panes_filters_dead_ones(self):
        self.write_session({"a": {"pid": os.getpid()}, "b": {"pid": 999999}})
        self.assertEqual(list(live_panes()), ["a"])


class TestResolveSession(TempConfig):
    def test_exact_slug(self):
        self.make_session("cert_renewal")
        self.assertIsNotNone(resolve_session_dir("cert_renewal"))

    def test_forgiving_name(self):
        self.make_session("cert_renewal")
        self.assertIsNotNone(resolve_session_dir("Cert Renewal"))

    def test_missing(self):
        self.assertIsNone(resolve_session_dir("nothing-here"))


class TestResolutionStaysInsideTheSessionTree(TempConfig):
    """A recording is a direct child of the sessions directory. Nothing else.

    The name comes straight off the command line and used to be tried as a
    raw path, so `sectape rm ../../work --yes` resolved - and then deleted -
    a directory that was never a recording.
    """

    def setUp(self):
        super().setUp()
        (self.sessions / "real").mkdir(parents=True, exist_ok=True)
        self.outside = self.root / "precious"
        self.outside.mkdir(parents=True, exist_ok=True)
        (self.outside / "keep.txt").write_text("data", encoding="utf-8")

    def test_a_real_recording_still_resolves(self):
        self.assertEqual(resolve_session_dir("real"),
                         (self.sessions / "real").resolve())

    def test_relative_escape_is_refused(self):
        for name in ("../precious", "../../precious", "../..", "..",
                     "real/../../precious"):
            self.assertIsNone(resolve_session_dir(name), name)

    def test_absolute_path_is_refused(self):
        self.assertIsNone(resolve_session_dir(str(self.outside)))
        self.assertIsNone(resolve_session_dir("/etc"))

    def test_a_nested_directory_is_not_a_recording(self):
        (self.sessions / "real" / "inner").mkdir(exist_ok=True)
        self.assertIsNone(resolve_session_dir("real/inner"))

    def test_rm_refuses_to_delete_outside_the_tree(self):
        import subprocess
        import sys
        env = dict(os.environ)
        env.update({"SECTAPE_STATE_DIR": str(config.settings.state_dir),
                    "SECTAPE_OUTPUT_DIR": str(config.settings.output_dir),
                    "SECTAPE_CONFIG": str(self.root / "none.toml")})
        result = subprocess.run(
            [sys.executable, "-m", "sectape", "rm", "../../precious", "--yes"],
            env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.outside / "keep.txt").exists(),
                        "sectape rm deleted a directory outside the tree")


class TestConcurrentPanes(TempConfig):
    """Several tabs record into one session at once, so the registry and the
    notes file are both written under a lock."""

    def test_parallel_processes_get_distinct_pane_numbers(self):
        import multiprocessing as mp

        session_dir = self.sessions / "race"

        def worker(index, state_dir, out_dir, queue):
            os.environ["SECTAPE_STATE_DIR"] = str(state_dir)
            os.environ["SECTAPE_OUTPUT_DIR"] = str(out_dir)
            config.load()
            from sectape.session import add_note as note
            from sectape.session import allocate_pane as claim
            target = config.settings.sessions_dir / "race"
            pane_id, path = claim(target)
            path.write_text(str(pane_id), encoding="utf-8")
            for i in range(4):
                note(f"note {index}-{i}", target)
            queue.put((pane_id, str(path)))

        context = mp.get_context("fork")
        queue = context.Queue()
        workers = [context.Process(target=worker,
                                   args=(i, config.settings.state_dir,
                                         config.settings.output_dir, queue))
                   for i in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)
        results = [queue.get(timeout=10) for _ in workers]

        ids = [r[0] for r in results]
        self.assertEqual(len(set(ids)), len(ids), f"pane numbers collided: {ids}")
        self.assertEqual(len(set(r[1] for r in results)), len(results),
                         "two panes were handed the same log file")
        self.assertEqual(len(read_notes(session_dir)), 8 * 4,
                         "notes were lost to interleaved writes")


class TestPaneRecordsWithoutAUsablePid(TempConfig):
    """`pane.get("pid", -1)` is the default for a record that lost its pid.

    `os.kill(-1, 0)` does not raise - it addresses every process we may
    signal - so such a pane counted as alive forever: the session could never
    go idle, and signalling it would have gone to everything.
    """

    def session_with(self, pane: dict) -> dict:
        data = {"label": "x", "slug": "x", "dir": str(self.sessions / "x"),
                "started": 1700000000.0, "panes": {"01": pane}}
        write_json_atomic(config.settings.current_session_file, data)
        return data

    def test_a_pane_with_no_pid_is_not_live(self):
        self.assertEqual(live_panes(self.session_with({"log": "a.raw"})), {})

    def test_a_pane_with_a_negative_pid_is_not_live(self):
        self.assertEqual(live_panes(self.session_with({"pid": -1})), {})

    def test_a_pane_with_pid_zero_is_not_live(self):
        self.assertEqual(live_panes(self.session_with({"pid": 0})), {})

    def test_such_panes_are_pruned(self):
        data = {"panes": {"01": {"pid": -1}, "02": {}, "03": {"pid": 0},
                          "04": {"pid": os.getpid()}}}
        self.assertEqual(sorted(prune_dead_panes(data)), ["04"])

    def test_the_session_can_go_idle_again(self):
        self.session_with({"pid": -1})
        self.assertTrue(clear_session_if_idle())
        self.assertFalse(config.settings.current_session_file.exists())

    def test_nothing_is_signalled_for_them(self):
        # os.kill is stubbed out on purpose. The bug being guarded against is
        # precisely that a pid of -1 reaches os.kill, which signals every
        # process the user owns - running this for real against a regressed
        # pid_alive would kill the test runner and the developer's session
        # with it.
        import unittest.mock as mock
        for pane in ({"pid": -1}, {"pid": 0}, {}, {"pid": "nonsense"}):
            session = self.session_with(pane)
            with mock.patch("sectape.session.os.kill") as killed:
                sent = signal_panes(session)
            self.assertEqual(sent, 0, pane)
            for call in killed.call_args_list:
                self.assertGreater(int(call.args[0]), 0,
                                   f"os.kill was handed {call.args[0]!r}")

    def test_a_live_pane_is_still_signalled(self):
        import signal as signals
        import unittest.mock as mock
        session = self.session_with({"pid": 4242, "log": "a.raw"})
        with mock.patch("sectape.session.pid_alive", return_value=True), \
                mock.patch("sectape.session.os.kill") as killed:
            self.assertEqual(signal_panes(session), 1)
        killed.assert_called_once_with(4242, signals.SIGTERM)


class TestSessionMeta(TempConfig):
    """A recording remembers its own details next to its logs."""

    def test_the_readable_label_survives_the_slug(self):
        # Without this an old recording only knows its directory name, so it
        # exported as `cert_renewal` instead of `cert renewal`.
        session_dir = self.sessions / "cert_renewal"
        write_session_meta(session_dir, {
            "label": "cert renewal", "slug": "cert_renewal",
            "started": 1700000000.0, "shell": "/bin/zsh", "host": "box"})
        self.assertEqual(read_session_meta(session_dir)["label"], "cert renewal")

    def test_only_the_known_keys_are_kept(self):
        session_dir = self.sessions / "s"
        write_session_meta(session_dir, {"label": "x", "panes": {"01": {}},
                                         "secret": "do not keep"})
        self.assertEqual(set(read_session_meta(session_dir)), {"label"})

    def test_absent_values_are_not_written(self):
        session_dir = self.sessions / "s2"
        write_session_meta(session_dir, {"label": "x", "started": None})
        self.assertNotIn("started", read_session_meta(session_dir))

    def test_a_session_with_no_meta_reads_as_empty(self):
        self.assertEqual(read_session_meta(self.sessions / "never"), {})

    def test_a_corrupt_meta_file_reads_as_empty(self):
        session_dir = self.sessions / "bad"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "meta.json").write_text("[not an object]", encoding="utf-8")
        self.assertEqual(read_session_meta(session_dir), {})


class TestWaitForPanes(TempConfig):
    def test_returns_zero_when_nothing_is_live(self):
        write_json_atomic(config.settings.current_session_file,
                          {"label": "x", "panes": {}})
        self.assertEqual(wait_for_panes(timeout=0.5), 0)

    def test_reports_what_is_still_alive_after_the_timeout(self):
        write_json_atomic(config.settings.current_session_file,
                          {"label": "x",
                           "panes": {"01": {"pid": os.getpid(), "log": "a"}}})
        started = time.monotonic()
        self.assertEqual(wait_for_panes(timeout=0.4), 1)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)


class TestStateTreePermissions(TempConfig):
    """Everything holding raw terminal output is owner-only.

    The README promises it, and the state tree was 0700 while the recordings
    inside it were 0755 and the notes file 0644.
    """

    def mode(self, path) -> int:
        return path.stat().st_mode & 0o777

    def test_a_session_directory_is_owner_only(self):
        session_dir = ensure_session_dir(self.sessions / "private")
        self.assertEqual(self.mode(session_dir), 0o700)

    def test_an_existing_directory_is_tightened(self):
        session_dir = self.sessions / "loose"
        session_dir.mkdir(parents=True)
        session_dir.chmod(0o755)
        ensure_session_dir(session_dir)
        self.assertEqual(self.mode(session_dir), 0o700)

    def test_allocate_pane_makes_an_owner_only_directory(self):
        _, path = allocate_pane(self.sessions / "claimed")
        self.assertEqual(self.mode(path.parent), 0o700)

    def test_the_notes_file_is_owner_only(self):
        session_dir = self.sessions / "noted"
        add_note("something private", session_dir, when=1700000000.0)
        self.assertEqual(self.mode(session_dir / "notes.jsonl"), 0o600)

    def test_appending_a_note_keeps_it_owner_only(self):
        session_dir = self.sessions / "noted2"
        add_note("first", session_dir, when=1.0)
        add_note("second", session_dir, when=2.0)
        self.assertEqual(self.mode(session_dir / "notes.jsonl"), 0o600)
        self.assertEqual(len(read_notes(session_dir)), 2)

    def test_the_meta_file_is_owner_only(self):
        session_dir = self.sessions / "meta"
        write_session_meta(session_dir, {"label": "x"})
        self.assertEqual(self.mode(session_dir / "meta.json"), 0o600)


if __name__ == "__main__":
    unittest.main()
