"""Turning raw pane logs into a list of executed commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .markers import MARKER_RE, _b64d, capture_width
from .terminal import VTScreen, render
from .text import (FULLSCREEN_CMDS, base_command, clean_terminal_output, redact)


# Commands that only exist to drive the shell or the recorder itself.
IGNORED_CMDS = {"exit", "clear", "logout", "reset", "zsh", "bash", "sh", "fish"}
IGNORED_PROGRAMS = {"sectape"}


# Deliberately conservative: a fancy prompt ending in a chevron, or the
# classic user@host:path$ form. v2.2 matched any line containing '$' or '#',
# which turned ordinary output into fake commands.
PROMPT_PATTERNS = [
    re.compile(r"^.{0,200}?[❯➜»▶]\s+(?P<cmd>\S.*)$"),
    re.compile(r"^[\w.\-]+@[\w.\-]+:\S*\s*[#$]\s+(?P<cmd>\S.*)$"),
    re.compile(r"^\(?[\w.\-]*\)?\s*[\w.\-]+@[\w.\-]+\s*[#$]\s+(?P<cmd>\S.*)$"),
]


# A prompt with nothing typed after it. It ends the previous command's output
# and starts nothing - without this, redrawn prompts pile up inside the
# previous step's output block.
BARE_PROMPT_PATTERNS = [
    re.compile(r"^.{0,200}?[❯➜»▶]\s*$"),
    re.compile(r"^[\w.\-]+@[\w.\-]+:\S*\s*[#$]\s*$"),
]


def looks_like_prompt(line: str) -> bool:
    stripped = line.strip()
    return any(p.match(stripped) for p in BARE_PROMPT_PATTERNS)


@dataclass
class Step:
    cmd: str
    output: str = ""
    exit_code: int | None = None
    cwd: str = ""
    started: float | None = None
    duration: float | None = None
    source: str = "heuristic"          # "marker" | "heuristic"
    pane: str = ""                     # which pane log it came from

    @property
    def failed(self) -> bool:
        return self.exit_code is not None and self.exit_code != 0


def _is_ignored(cmd: str) -> bool:
    c = cmd.strip()
    if not c:
        return True
    if c in IGNORED_CMDS:
        return True
    return base_command(c) in IGNORED_PROGRAMS


def read_raw_log(path) -> str:
    """Read a pane log verbatim.

    newline="" is essential: universal-newline translation would swallow every
    carriage return, and CR is what tells the emulator to return to column 0.
    Without it, multi-line output renders as a staircase.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            return f.read()
    except Exception:
        return ""


def parse_marked_transcript(raw: str) -> list[Step]:
    """Exact extraction using the shell-integration markers."""
    marks = list(MARKER_RE.finditer(raw))
    if not marks:
        return []

    steps: list[Step] = []
    pending: Step | None = None
    out_start = 0
    width = VTScreen.DEFAULT_WIDTH

    for m in marks:
        payload = m.group(1)
        parts = payload.split("|")
        kind = parts[0] if parts else ""

        if kind == "w":
            try:
                width = int(parts[1])
            except (IndexError, ValueError):
                pass
        elif kind == "b":
            cmd = _b64d(parts[1]) if len(parts) > 1 else ""
            try:
                started = float(parts[2]) if len(parts) > 2 else None
            except ValueError:
                started = None
            pending = Step(cmd=cmd.strip(), started=started, source="marker")
            out_start = m.end()
        elif kind == "e" and pending is not None:
            try:
                pending.exit_code = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                pending.exit_code = None
            try:
                ended = float(parts[2]) if len(parts) > 2 else None
            except ValueError:
                ended = None
            if ended is not None and pending.started is not None:
                pending.duration = max(0.0, ended - pending.started)
            pending.cwd = _b64d(parts[3]) if len(parts) > 3 else ""
            pending.output = clean_terminal_output(render(raw[out_start:m.start()], width))
            steps.append(pending)
            pending = None

    if pending is not None:                    # session ended mid-command
        pending.output = clean_terminal_output(render(raw[out_start:], width))
        steps.append(pending)

    return [s for s in steps if not _is_ignored(s.cmd)]


def parse_heuristic_transcript(raw: str) -> list[Step]:
    """Fallback for captures with no markers (old logs, ssh, other shells)."""
    text = render(raw, capture_width(raw))
    steps: list[Step] = []
    current: Step | None = None
    buf: list[str] = []

    def close():
        nonlocal current, buf
        if current is not None:
            current.output = clean_terminal_output("\n".join(buf))
            steps.append(current)
        current, buf = None, []

    for line in text.split("\n"):
        matched = None
        for pat in PROMPT_PATTERNS:
            m = pat.match(line.strip())
            if m:
                matched = m.group("cmd").strip()
                break

        if matched is not None:
            close()
            if not _is_ignored(matched):
                current = Step(cmd=matched, source="heuristic")
        elif looks_like_prompt(line):
            close()
        elif current is not None:
            buf.append(line)

    close()
    return steps


def summarise_interactive(steps: list[Step]) -> list[Step]:
    """Replace redrawn full-screen output with a one-liner."""
    for step in steps:
        base = base_command(step.cmd)
        if base in FULLSCREEN_CMDS:
            step.output = f"<interactive {base} session - screen output not recorded>"
    return steps


def parse_transcript(raw: str) -> list[Step]:
    steps = parse_marked_transcript(raw)
    if not steps:
        steps = parse_heuristic_transcript(raw)
    return summarise_interactive(steps)


def dedupe_steps(steps: list[Step]) -> list[Step]:
    out: list[Step] = []
    for s in steps:
        if not s.cmd.strip():
            continue
        if out and out[-1].cmd == s.cmd and out[-1].output == s.output:
            continue
        out.append(s)
    return out


MAX_SCAN_BYTES = 8 * 1024 * 1024


def count_commands(session_dir: Path) -> int | None:
    """Cheap command count for listings - no VT replay on integrated logs.

    Returns None when a log is too large to scan, so callers can say so
    instead of freezing on a `cat`-a-huge-file session.
    """
    total = 0
    for pf in sorted(session_dir.glob("pane_*.raw")):
        try:
            if pf.stat().st_size > MAX_SCAN_BYTES:
                return None
        except OSError:
            continue
        raw = read_raw_log(pf)
        if not raw:
            continue
        # Count the commands that would actually be exported: marker payloads
        # minus the shell/recorder noise the parser drops.
        marked = 0
        found_marker = False
        for m in MARKER_RE.finditer(raw):
            payload = m.group(1)
            if not payload.startswith("b|"):
                continue
            found_marker = True
            parts = payload.split("|")
            if len(parts) > 1 and not _is_ignored(_b64d(parts[1])):
                marked += 1
        total += marked if found_marker else len(parse_heuristic_transcript(raw))
    return total


def sort_by_time(steps: list[Step]) -> list[Step]:
    """Interleave panes chronologically.

    Reading pane logs one after another put every command from pane 1 before
    pane 2's, which is wrong whenever you work in two tabs at once. Steps that
    carry a marker timestamp are ordered by it; unmarked ones keep their
    position relative to their neighbours.
    """
    if not any(s.started for s in steps):
        return steps
    decorated = []
    last_seen = 0.0
    for index, step in enumerate(steps):
        if step.started:
            last_seen = step.started
        decorated.append((last_seen, index, step))
    decorated.sort(key=lambda item: (item[0], item[1]))
    return [step for _, _, step in decorated]


def collect_steps(session_dir: Path, do_redact: bool = True) -> tuple[list[Step], int]:
    """Read every pane log for a session, oldest first, and extract the steps."""
    raws: list[tuple[str, str]] = []
    if session_dir.exists():
        for pf in sorted(session_dir.glob("pane_*.raw"), key=lambda p: p.stat().st_mtime):
            raw = read_raw_log(pf)
            if raw:
                raws.append((pf.stem.replace("pane_", ""), raw))

    steps: list[Step] = []
    for pane_name, raw in raws:
        for step in parse_transcript(raw):
            step.pane = pane_name
            steps.append(step)

    steps = sort_by_time(dedupe_steps(steps))
    if do_redact:
        for s in steps:
            s.cmd = redact(s.cmd)
            s.output = redact(s.output)
    return steps, len(raws)


# --------------------------------------------------------------------------
# Session state (shared by every pane, so it needs a lock)
# --------------------------------------------------------------------------
