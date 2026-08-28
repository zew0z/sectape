"""Export formats.

A recording is a list of :class:`~sectape.transcript.Step`. Each writer turns
that into one file. Adding a format means adding a function and an entry in
:data:`WRITERS` - nothing else in the package knows about them.
"""
from __future__ import annotations

import html
import json
import time
from dataclasses import asdict
from dataclasses import replace as dc_replace
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
                 shell: str = "", host: str = "", notes: list[dict] | None = None):
        self.label = label
        self.steps = steps
        self.panes = panes
        self.started = started
        self.ended = ended or time.time()
        self.shell = shell
        self.host = host
        self.notes = notes or []

    def timeline(self) -> list[tuple[str, object]]:
        """Steps and notes in one chronological sequence.

        A note written between two commands belongs between them; one written
        while a long command was still running belongs just after it.
        """
        events: list[tuple[float, int, str, object]] = []
        for index, step in enumerate(self.steps):
            events.append((step.started or 0.0, index, "step", step))
        for note in self.notes:
            # Place a note after any command that was already running.
            position = len(self.steps)
            for index, step in enumerate(self.steps):
                if (step.started or 0.0) > note["at"]:
                    position = index
                    break
            events.append((note["at"], position - 0.5, "note", note))
        if not any(t for t, _, _, _ in events):
            return ([("step", s) for s in self.steps]
                    + [("note", n) for n in self.notes])
        events.sort(key=lambda item: (item[0], item[1]))
        return [(kind, payload) for _, _, kind, payload in events]

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
            "notes": self.notes,
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
    if rec.notes:
        facts.append(f"- **Notes**: {len(rec.notes)}")
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

    index = 0
    for kind, item in rec.timeline():
        if kind == "note":
            stamp = _stamp(item["at"])
            head = f"> **note**{' · ' + stamp if stamp else ''}"
            out.append(head)
            for line in str(item["text"]).split("\n"):
                out.append(f"> {line}" if line else ">")
            out.append("")
            continue

        step = item
        index += 1
        meta = [m for m in (
            _stamp(step.started),
            None if step.exit_code is None else
            ("exit 0" if step.exit_code == 0 else f"**exit {step.exit_code}**"),
            human_duration(step.duration) if step.duration else None,
            f"`{step.cwd}`" if step.cwd else None,
            f"pane {step.pane}" if step.pane and rec.panes > 1 else None,
        ) if m]
        out.append(f"### {index}. `{step.cmd}`" + (" ⚠️" if step.failed else ""))
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
    for kind, item in rec.timeline():
        if kind == "note":
            for line in str(item["text"]).split("\n"):
                out.append(f"# {line}")
            out.append("")
            continue
        step = item
        tag = f" [exit {step.exit_code}]" if step.failed else ""
        out.append(f"{prompt} {step.cmd}{tag}")
        if step.output:
            out.append(step.output)
        out.append("")
    return "\n".join(out)


HTML_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1c1c1a; --muted: #6b6b66; --line: #e2e2dd;
  --card: #ffffff; --code-bg: #f4f4f1; --accent: #2f6f4f; --bad: #a33a2a;
  --note-bg: #fdf6e3; --note-line: #d9c78a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e6e6e1; --muted: #9a9a94; --line: #2c2e33;
    --card: #1c1e22; --code-bg: #101114; --accent: #7fbf9a; --bad: #e08b7a;
    --note-bg: #221f14; --note-line: #5c5230;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.25rem 5rem; background: var(--bg); color: var(--fg);
  font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .35rem; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: .9rem; margin-bottom: 1.75rem; }
.facts { display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; padding: 0; margin: 0 0 2rem;
         list-style: none; font-size: .875rem; color: var(--muted); }
.facts b { color: var(--fg); font-weight: 600; }
.step { border: 1px solid var(--line); border-radius: 10px; background: var(--card);
        margin-bottom: 1rem; overflow: hidden; }
.step > header { padding: .7rem .9rem; border-bottom: 1px solid var(--line);
                 display: flex; gap: .75rem; align-items: baseline; flex-wrap: wrap; }
.n { color: var(--muted); font-variant-numeric: tabular-nums; font-size: .8rem; }
.cmd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9rem;
       font-weight: 600; word-break: break-word; }
.meta { margin-left: auto; color: var(--muted); font-size: .78rem;
        font-variant-numeric: tabular-nums; }
.failed .cmd { color: var(--bad); }
.failed .meta b { color: var(--bad); }
pre { margin: 0; padding: .85rem .9rem; background: var(--code-bg); overflow-x: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .84rem;
      line-height: 1.5; }
pre .p { color: var(--accent); user-select: none; }
.note { border-left: 3px solid var(--note-line); background: var(--note-bg);
        padding: .7rem .9rem; border-radius: 0 8px 8px 0; margin: 0 0 1rem; }
.note .n { display: block; margin-bottom: .2rem; text-transform: uppercase;
           letter-spacing: .06em; font-size: .68rem; }
.note p { margin: 0; white-space: pre-wrap; }
.empty { color: var(--muted); font-style: italic; }
"""


def to_html(rec: Recording) -> str:
    """A self-contained page - no external assets, readable in either theme."""
    esc = html.escape
    prompt = config.settings.prompt

    facts = [("Commands", str(len(rec.steps)))]
    if rec.failed:
        facts.append(("Failed", str(len(rec.failed))))
    if rec.panes > 1:
        facts.append(("Panes", str(rec.panes)))
    if rec.wall_time:
        facts.append(("Elapsed", human_duration(rec.wall_time)))
    if rec.notes:
        facts.append(("Notes", str(len(rec.notes))))
    if rec.shell:
        facts.append(("Shell", rec.shell))

    body = [f"<h1>{esc(rec.label)}</h1>",
            f'<p class="sub">{esc(_date(rec.started))}'
            + (f" · {esc(rec.host)}" if rec.host else "") + "</p>",
            '<ul class="facts">']
    body += [f"<li>{esc(k)} <b>{esc(v)}</b></li>" for k, v in facts]
    body.append("</ul>")

    if not rec.steps and not rec.notes:
        body.append('<p class="empty">No commands were captured in this session.</p>')

    index = 0
    for kind, item in rec.timeline():
        if kind == "note":
            stamp = _stamp(item["at"])
            body.append(
                f'<div class="note"><span class="n">note{" · " + esc(stamp) if stamp else ""}'
                f'</span><p>{esc(str(item["text"]))}</p></div>')
            continue

        step = item
        index += 1
        bits = [b for b in (
            esc(_stamp(step.started)),
            None if step.exit_code is None else
            ("exit 0" if step.exit_code == 0 else f"<b>exit {step.exit_code}</b>"),
            human_duration(step.duration) if step.duration else None,
            esc(step.cwd) if step.cwd else None,
            f"pane {esc(step.pane)}" if step.pane and rec.panes > 1 else None,
        ) if b]
        classes = "step failed" if step.failed else "step"
        body.append(f'<article class="{classes}"><header>'
                    f'<span class="n">{index}</span>'
                    f'<span class="cmd">{esc(step.cmd)}</span>'
                    f'<span class="meta">{" · ".join(bits)}</span></header>')
        rendered = f'<span class="p">{esc(prompt)}</span> {esc(step.cmd)}'
        if step.output:
            rendered += "\n" + esc(step.output)
        body.append(f"<pre>{rendered}</pre></article>")

    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n"
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(rec.label)}</title>\n<style>{HTML_CSS}</style>\n"
            "</head>\n<body>\n<main>\n" + "\n".join(body) + "\n</main>\n</body>\n</html>\n")


def filter_steps(steps: list[Step], only_failed: bool = False,
                 last: int | None = None, grep: str = "",
                 drop_output: bool = False) -> list[Step]:
    """Narrow a transcript before rendering it."""
    import re as _re

    selected = list(steps)
    if only_failed:
        selected = [s for s in selected if s.failed]
    if grep:
        try:
            pattern = _re.compile(grep, _re.I)
        except _re.error as exc:
            raise config.ConfigError(f"bad --grep pattern: {exc}") from None
        selected = [s for s in selected
                    if pattern.search(s.cmd) or pattern.search(s.output or "")]
    if last is not None:
        if last < 1:
            raise config.ConfigError("--last needs a positive count")
        selected = selected[-last:]
    if drop_output:
        selected = [dc_replace(s, output="") for s in selected]
    return selected


WRITERS = {
    "markdown": (to_markdown, ".md"),
    "json": (to_json, ".json"),
    "text": (to_text, ".txt"),
    "html": (to_html, ".html"),
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
