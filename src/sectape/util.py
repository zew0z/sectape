"""Small filesystem and formatting helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def slugify(text: str) -> str:
    """Filesystem-safe slug. Never empty, never traverses, never hidden.

    A label with no ASCII in it - a session named in Japanese, Korean, Cyrillic
    or emoji - leaves nothing behind. That used to become the single fixed name
    `room`, so every such recording shared one directory and one export. A
    digest of the label keeps them apart; the readable name is kept in the
    session's own meta.json and used for the export filename.
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(text or "")).strip("_-").lower()
    slug = re.sub(r"_{2,}", "_", slug)[:80]
    if slug:
        return slug
    if not str(text or "").strip():
        return "session"
    digest = hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:10]
    return f"session-{digest}"


def one_line(text) -> str:
    """Collapse any run of whitespace to a single space.

    A label is a heading, a filename and a row in a listing, and none of those
    survive a newline in the middle of them - `sectape rec "$(some-command)"`
    is all it takes.
    """
    return " ".join(str(text or "").split())


def safe_filename(text: str, fallback: str = "untitled") -> str:
    """A single path component safe to join onto a vault directory."""
    s = str(text or "").replace("\n", " ").strip()
    s = re.sub(r"[/\\\x00-\x1f]", "-", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" .")
    if s in ("", ".", ".."):
        s = fallback
    return s[:120]


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def load_json(path) -> dict | None:
    """A JSON *object* from `path`, or None if it is missing or unusable.

    The type check earns its keep: `[1, 2, 3]` is valid JSON, so a state file
    holding a list got this far and then met a caller doing `.get`. Anything
    that is not an object is treated as no state at all.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def umask_mode(base: int = 0o666) -> int:
    """The permissions a plain `open()` would have given a new file."""
    current = os.umask(0)
    os.umask(current)
    return base & ~current


def _write_atomic(path: Path, data: str, suffix: str,
                  mode: int | None = None) -> None:
    """Write `data` to `path` via a temporary file in the same directory.

    Every failure is re-raised against the file the caller actually asked
    for. Reporting the scratch file instead told the user their export had
    failed at some `.tmp-3f9a1c.md` they had never heard of.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=suffix)
    except OSError as exc:
        raise OSError(exc.errno, exc.strerror, str(path)) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        if mode is not None:
            # mkstemp always creates 0600. That is right for state, but an
            # export is a document you hand to someone.
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise OSError(exc.errno, exc.strerror, str(path)) from None
        raise


def write_json_atomic(path: Path, data) -> None:
    _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False), ".json")


def write_text_atomic(path: Path, text: str, mode: int | None = None) -> None:
    _write_atomic(path, text, Path(path).suffix or ".txt", mode)


def pid_alive(pid) -> bool:
    """Whether a process id belongs to a process we can see.

    Only a positive id is a process. `os.kill(0, 0)` addresses our own process
    group and `os.kill(-1, 0)` addresses every process we may signal - neither
    raises, so both used to answer "alive", and -1 is the default this is
    called with for a pane record that has lost its pid.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except OSError:
        return False
    return True


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """`1 note` / `2 notes`."""
    word = singular if abs(count) == 1 else (plural_form or singular + "s")
    return f"{count} {word}"


def short_path(path) -> str:
    """Display form of a path, with $HOME collapsed to ~."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text
