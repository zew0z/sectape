"""Shared test scaffolding."""
from __future__ import annotations

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sectape import config

ESC = "\x1b"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def marker(kind: str, *parts: str) -> str:
    return f"{ESC}]7337;SECTAPE;{kind}|{'|'.join(parts)}\x07"


def begin(cmd: str, when: float = 1000.0) -> str:
    return marker("b", b64(cmd), str(when))


def end(code: int = 0, when: float = 1001.5, cwd: str = "/home/user") -> str:
    return marker("e", str(code), str(when), b64(cwd))


def size(cols: int, rows: int = 40) -> str:
    return marker("w", str(cols), str(rows))


def legacy_marker(kind: str, *parts: str) -> str:
    """A marker written by the tool under its previous name."""
    return f"{ESC}]7337;THM;{kind}|{'|'.join(parts)}\x07"


class TempConfig(unittest.TestCase):
    """Base class giving each test an isolated state and output directory."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sectape-test-"))
        # Also belt-and-braces for a direct `python tests/test_config.py`,
        # where conftest.py does not run.
        self._saved_env = {k: os.environ.pop(k) for k in list(os.environ)
                           if k.startswith("SECTAPE_")}
        self._saved = config.settings
        config.settings = config.Settings(
            state_dir=self.root / "state",
            output_dir=self.root / "out",
        )
        config.ensure_dirs()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.environ.update(self._saved_env)
        config.settings = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def sessions(self) -> Path:
        return config.settings.sessions_dir

    def make_session(self, name: str, *raws: str) -> Path:
        """Create a session directory containing one pane log per raw string."""
        session_dir = self.sessions / name
        session_dir.mkdir(parents=True, exist_ok=True)
        for i, raw in enumerate(raws or ("",), 1):
            (session_dir / f"pane_{i}.raw").write_text(raw, encoding="utf-8")
        return session_dir
