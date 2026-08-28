"""Export formats.

A recording is a list of :class:`~sectape.transcript.Step`. Each writer turns
that into one file. Adding a format means adding a function and an entry in
:data:`WRITERS` - nothing else in the package knows about them.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from . import config
from .text import TRIVIAL_CMDS, base_command
from .transcript import Step
from .util import human_duration, safe_filename, write_text_atomic

GEN_BEGIN = "<!-- sectape:begin -->"
GEN_END = "<!-- sectape:end -->"


class Recording:
    """Everything a writer needs to render one session."""

    def __init__(self, label: str, steps: list[Step], panes: int,
                 started: float | None = None, ended: float | None = None,
                 shell: str = "", host: str = ""):
        self.label = label
        self.steps = steps
        self.panes = panes
        self.started = started
        self.ended = ended or time.time()
        self.shell = shell
        self.host = host

    # -- summary ----------------------------------------------------------
    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.failed]

    @property
    def busy_time(self) -> float:
        return sum(s.duration for s in self.steps if s.duration)

    @property
    def wall_time(self) -> float:
        if self.started:
            return max(0.0, self.ended - self.started)
        stamps = [s.started for s in self.steps if s.started]
        return max(stamps) - min(stamps) if len(stamps) > 1 else 0.0

    def programs(self, include_trivial: bool = False) -> list[str]:
        """Distinct programs run, in first-use order."""
        seen: list[str] = []
        for step in self.steps:
            name = base_command(step.cmd)
            if not name or name in seen:
                continue
            if not include_trivial and name in TRIVIAL_CMDS:
                continue
            seen.append(name)
        return seen

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "started": self.started,
            "ended": self.ended,
            "panes": self.panes,
            "shell": self.shell,
            "host": self.host,
            "commands": len(self.steps),
            "failed": len(self.failed),
            "programs": self.programs(),
            "steps": [asdict(s) for s in self.steps],
        }


def _stamp(epoch: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch)) if epoch else ""


def _date(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch or time.time()))


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------

def to_markdown(rec: Recording) -> str:
    prompt = config.settings.prompt
    out = [
        "---",
        "type: terminal-capture",
        f"label: {json.dumps(rec.label)}",
        f"date: {_date(rec.started)}",
        f"commands: {len(rec.steps)}",
        f"failed: {len(rec.failed)}",
        "---",
        "",
        f"# {rec.label}",
        "",
    ]

    facts = [f"- **Commands**: {len(rec.steps)}"]
    if rec.failed:
        facts.append(f"- **Non-zero exits**: {len(rec.failed)}")
    if rec.panes > 1:
        facts.append(f"- **Panes**: {rec.panes}")
    if rec.wall_time:
        facts.append(f"- **Elapsed**: {human_duration(rec.wall_time)}")
    if rec.busy_time:
        facts.append(f"- **Time in commands**: {human_duration(rec.busy_time)}")
    if rec.shell:
        facts.append(f"- **Shell**: `{rec.shell}`")
    programs = rec.programs()
    if programs:
        facts.append("- **Programs**: " + ", ".join(f"`{p}`" for p in programs))
    out += facts + ["", GEN_BEGIN, ""]

    if not rec.steps:
        out += ["> No commands were captured in this session.", "", GEN_END, ""]
        return "\n".join(out)

    if all(s.source == "heuristic" for s in rec.steps):
        out += ["> **Reconstructed transcript.** This session was recorded without "
                "shell integration, so commands were read back off the screen and "
                "may carry prompt-redraw artifacts.", ""]

    for i, step in enumerate(rec.steps, 1):
        meta = [m for m in (
            _stamp(step.started),
            None if step.exit_code is None else
            ("exit 0" if step.exit_code == 0 else f"**exit {step.exit_code}**"),
            human_duration(step.duration) if step.duration else None,
            f"`{step.cwd}`" if step.cwd else None,
        ) if m]
        out.append(f"### {i}. `{step.cmd}`" + (" ⚠️" if step.failed else ""))
        out.append("")
        if meta:
            out += [" · ".join(meta), ""]
        out.append("```console")
        out.append(f"{prompt} {step.cmd}")
        if step.output:
            out.append(step.output)
        out += ["```", ""]

    out += [GEN_END, ""]
    return "\n".join(out)


def to_json(rec: Recording) -> str:
    return json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n"


def to_text(rec: Recording) -> str:
    prompt = config.settings.prompt
    out = [f"# {rec.label}  ({len(rec.steps)} commands, "
           f"{len(rec.failed)} failed)", ""]
    for step in rec.steps:
        tag = f" [exit {step.exit_code}]" if step.failed else ""
        out.append(f"{prompt} {step.cmd}{tag}")
        if step.output:
            out.append(step.output)
        out.append("")
    return "\n".join(out)


WRITERS = {
    "markdown": (to_markdown, ".md"),
    "json": (to_json, ".json"),
    "text": (to_text, ".txt"),
}


def render(rec: Recording, fmt: str | None = None) -> tuple[str, str]:
    """Render a recording; returns (text, file extension)."""
    fmt = fmt or config.settings.format
    try:
        writer, suffix = WRITERS[fmt]
    except KeyError:
        raise config.ConfigError(
            f"unknown format {fmt!r}; choose one of {', '.join(WRITERS)}") from None
    return writer(rec), suffix


# --------------------------------------------------------------------------
# writing to disk, without clobbering edits
# --------------------------------------------------------------------------

def merge(new_text: str, path: Path) -> str:
    """Keep anything a human added outside the generated block."""
    if not path.exists():
        return new_text
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        return new_text
    if GEN_BEGIN not in existing or GEN_END not in existing:
        return new_text
    if GEN_BEGIN not in new_text or GEN_END not in new_text:
        return new_text
    head, rest = existing.split(GEN_BEGIN, 1)
    _, tail = rest.split(GEN_END, 1)
    inner = new_text.split(GEN_BEGIN, 1)[1].split(GEN_END, 1)[0]
    return head + GEN_BEGIN + inner + GEN_END + tail


def export(rec: Recording, fmt: str | None = None,
           destination: Path | None = None) -> Path:
    """Render a recording and write it to the output directory."""
    text, suffix = render(rec, fmt)
    if destination is not None:
        path = Path(destination)
    else:
        out_dir = config.settings.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{safe_filename(rec.label)}{suffix}"
    write_text_atomic(path, merge(text, path))
    return path
