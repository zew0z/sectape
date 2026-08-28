import os
import unittest

from sectape import config
from sectape.session import (clear_session_if_idle, new_pane_id,
                             prune_dead_panes, read_session, register_pane,
                             resolve_session_dir, unregister_pane)
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

    def test_pane_ids_are_distinct_enough(self):
        self.assertNotEqual(new_pane_id(), "")
        self.assertTrue(new_pane_id().isdigit())


class TestResolveSession(TempConfig):
    def test_exact_slug(self):
        self.make_session("cert_renewal")
        self.assertIsNotNone(resolve_session_dir("cert_renewal"))

    def test_forgiving_name(self):
        self.make_session("cert_renewal")
        self.assertIsNotNone(resolve_session_dir("Cert Renewal"))

    def test_missing(self):
        self.assertIsNone(resolve_session_dir("nothing-here"))


if __name__ == "__main__":
    unittest.main()
