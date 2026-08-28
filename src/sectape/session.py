"""Shared session state.

Several panes record into one session at once, so the registry of live panes is
read-modify-written under a file lock.
"""
from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .util import load_json, pid_alive, slugify, squash, write_json_atomic


@contextmanager
def session_lock():
    config.ensure_dirs()
    fd = os.open(str(config.settings.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_session() -> dict | None:
    return load_json(config.settings.current_session_file)


def mutate_session(fn, create: bool = False):
    """Read-modify-write current_session.json under the lock.

    Never resurrects a session file that has already been finished - a pane
    exiting after `sectape stop` must not recreate an empty session.
    """
    with session_lock():
        exists = config.settings.current_session_file.exists()
        data = load_json(config.settings.current_session_file) or {}
        result = fn(data)
        if exists or create:
            write_json_atomic(config.settings.current_session_file, data)
        return result


def prune_dead_panes(data: dict) -> dict:
    panes = data.get("panes") or {}
    data["panes"] = {k: v for k, v in panes.items() if pid_alive(v.get("pid", -1))}
    return data["panes"]


def register_pane(pane_id: str, log_path: Path) -> None:
    def _fn(data):
        prune_dead_panes(data)
        data.setdefault("panes", {})[pane_id] = {
            "pid": os.getpid(),
            "log": str(log_path),
            "started": time.time(),
        }
    mutate_session(_fn)


def unregister_pane(pane_id: str) -> int:
    """Remove this pane; returns how many live panes remain."""
    def _fn(data):
        panes = data.setdefault("panes", {})
        panes.pop(pane_id, None)
        prune_dead_panes(data)
        return len(data["panes"])
    try:
        return mutate_session(_fn)
    except Exception:
        return 0


# --------------------------------------------------------------------------
# PTY recording
# --------------------------------------------------------------------------


def clear_session_if_idle() -> bool:
    def _fn(data):
        if not data:
            return False
        prune_dead_panes(data)
        return not data.get("panes")
    try:
        idle = mutate_session(_fn)
    except Exception:
        idle = False
    if idle and config.settings.current_session_file.exists():
        config.settings.current_session_file.unlink()
    return bool(idle)


def new_pane_id() -> str:
    return f"{int(time.time() * 1000) % 100000:05d}"


def resolve_session_dir(name: str) -> Path | None:
    for candidate in (name, slugify(name), squash(name)):
        p = config.settings.sessions_dir / candidate
        if p.is_dir():
            return p
    matches = [d for d in config.settings.sessions_dir.iterdir()
               if d.is_dir() and squash(d.name) == squash(name)]
    return matches[0] if matches else None
