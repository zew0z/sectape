"""Shared session state.

Several panes record into one session at once, so the registry of live panes is
read-modify-written under a file lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
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


def ensure_session_dir(path) -> Path:
    """Create a session directory, owner-only.

    It sits inside the state tree, which is 0700, but the session directories
    themselves were left to the umask - so the tree said `drwx------` and the
    recordings inside it `drwxr-xr-x`. Anything holding raw terminal output
    gets the same treatment as its parent.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


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
# Session lifecycle
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


def allocate_pane(session_dir: Path, log_name: str = "pane_{n:02d}.raw") -> tuple[str, Path]:
    """Claim the next pane number for a session.

    Panes are numbered 1, 2, 3 within a session rather than by a timestamp, so
    exports and the terminal can say "pane 2" and mean it. Allocation happens
    under the session lock, so two tabs starting at once cannot collide.
    """
    session_dir = ensure_session_dir(session_dir)

    def _claim(data):
        prune_dead_panes(data)
        used = {int(d) for d in
                (e.stem.replace("pane_", "") for e in session_dir.glob("pane_*.raw"))
                if d.isdigit()}
        used |= {int(k) for k in (data.get("panes") or {}) if str(k).isdigit()}
        number = 1
        while number in used:
            number += 1
        pane_id = f"{number:02d}"
        path = session_dir / log_name.format(n=number)
        data.setdefault("panes", {})[pane_id] = {
            "pid": os.getpid(),
            "log": str(path),
            "started": time.time(),
        }
        return pane_id, path

    return mutate_session(_claim, create=True)


def pane_label(pane_id: str) -> str:
    """`07` -> `7`, for display."""
    text = str(pane_id or "").lstrip("0")
    return text or str(pane_id or "")


def live_panes(session: dict | None = None) -> dict:
    session = session if session is not None else (read_session() or {})
    return {k: v for k, v in (session.get("panes") or {}).items()
            if pid_alive(v.get("pid", -1))}


def signal_panes(session: dict | None = None, sig=signal.SIGTERM) -> int:
    """Ask every live recorder to unwind. Returns how many were signalled."""
    sent = 0
    for pane in live_panes(session).values():
        pid = pane.get("pid")
        # Never hand a non-positive id to os.kill: 0 is our whole process
        # group and -1 is every process we are allowed to signal.
        if not pid_alive(pid) or int(pid) == os.getpid():
            continue
        try:
            os.kill(int(pid), sig)
            sent += 1
        except (OSError, TypeError, ValueError):
            continue
    return sent


def wait_for_panes(timeout: float = 8.0) -> int:
    """Wait for signalled recorders to exit; returns how many are still alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = len(live_panes())
        if not remaining:
            return 0
        time.sleep(0.15)
    return len(live_panes())


def resolve_session_dir(name: str) -> Path | None:
    """Find a recording by name. Never escapes the sessions directory.

    The name arrives straight from the command line, and the first candidate
    tried was the raw string: `sectape rm ../../work --yes` resolved to - and
    deleted - a directory that was never a recording. An absolute path did the
    same. A recording is always a direct child of the sessions directory, so
    anything else is refused.
    """
    try:
        root = config.settings.sessions_dir.resolve()
    except OSError:
        return None

    def child(candidate: str) -> Path | None:
        if not candidate:
            return None
        try:
            found = (root / candidate).resolve()
        except OSError:
            return None
        return found if found.parent == root and found.is_dir() else None

    for candidate in (name, slugify(name), squash(name)):
        found = child(candidate)
        if found is not None:
            return found
    try:
        entries = sorted(config.settings.sessions_dir.iterdir())
    except OSError:
        return None
    matches = [d for d in entries if d.is_dir() and squash(d.name) == squash(name)]
    return matches[0] if matches else None


# --------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------

NOTES_FILE = "notes.jsonl"
META_FILE = "meta.json"


def write_session_meta(session_dir: Path, meta: dict) -> None:
    """Remember a session's own details next to its logs.

    Without this an old recording only knows its slug, so exporting it later
    titled the document `cert_renewal` instead of `cert renewal`.
    """
    session_dir = ensure_session_dir(session_dir)
    keep = {k: meta.get(k) for k in ("label", "slug", "started", "shell", "host")
            if meta.get(k) is not None}
    write_json_atomic(session_dir / META_FILE, keep)


def read_session_meta(session_dir: Path) -> dict:
    return load_json(Path(session_dir) / META_FILE) or {}



def add_note(text: str, session_dir: Path | None = None,
             when: float | None = None) -> Path | None:
    """Append a timestamped note to the session, for exports to interleave."""
    text = str(text or "").strip()
    if not text:
        return None
    if session_dir is None:
        session = read_session()
        if not session:
            return None
        session_dir = Path(session.get("dir") or
                           (config.settings.sessions_dir / session.get("slug", "")))
    session_dir = ensure_session_dir(session_dir)
    path = session_dir / NOTES_FILE
    record = {"at": when if when is not None else time.time(), "text": text}
    with session_lock():
        # 0600 like the pane logs: a note holds whatever you wrote in it, and
        # the plain `open` left it readable by everyone.
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(handle, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_notes(session_dir: Path) -> list[dict]:
    """Every note recorded for a session, oldest first."""
    path = Path(session_dir) / NOTES_FILE
    notes: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("text"):
                    notes.append({"at": float(record.get("at") or 0.0),
                                  "text": str(record["text"])})
    except OSError:
        return []
    notes.sort(key=lambda n: n["at"])
    return notes
