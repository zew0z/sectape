"""Small filesystem and formatting helpers."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


def slugify(text: str) -> str:
    """Filesystem-safe slug. Never empty, never traverses, never hidden."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(text or "")).strip("_-").lower()
    s = re.sub(r"_{2,}", "_", s)
    return s[:80] or "room"


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
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


# --------------------------------------------------------------------------
# VT emulator - replays a raw PTY capture into the text that was on screen
# --------------------------------------------------------------------------
