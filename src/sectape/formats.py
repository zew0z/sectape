"""Export formats.

A recording is a list of :class:`~sectape.transcript.Step`. Each writer turns
that into one file. Adding a format means adding a function and an entry in
:data:`WRITERS` - nothing else in the package knows about them.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict
from dataclasses import replace as dc_replace
from pathlib import Path

from . import __version__ as VERSION
from . import config
from .session import pane_label
from .text import TRIVIAL_CMDS, base_command, commands_in
from .transcript import Step
from .util import (human_duration, one_line, plural, safe_filename,
                   umask_mode, write_text_atomic)

GEN_BEGIN = "<!-- sectape:begin -->"
GEN_END = "<!-- sectape:end -->"


class Recording:
    """Everything a writer needs to render one session."""

    def __init__(self, label: str, steps: list[Step], panes: int,
                 started: float | None = None, ended: float | None = None,
                 shell: str = "", host: str = "", notes: list[dict] | None = None):
        # Defensive: a recording made before labels were normalised still has
        # to produce a document with one heading in it.
        self.label = one_line(label) or "session"
        self.steps = steps
        self.panes = panes
        self.started = started
        self.ended = ended
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
    def finished(self) -> float:
        """When the recording actually stopped.

        Defaulting this to "now" made `sectape export` of a recording from
        last week report an elapsed time of several days, because the session
        start was real and the end was the moment you asked for the export.
        The last thing that happened in the session is the honest answer.
        """
        if self.ended is not None:
            return self.ended
        stamps = [s.started + (s.duration or 0.0)
                  for s in self.steps if s.started]
        stamps += [n["at"] for n in self.notes if n.get("at")]
        if stamps:
            return max(stamps)
        return self.started if self.started else time.time()

    @property
    def began(self) -> float:
        """When the content of this document starts.

        Recording again under an existing label appends to it, so the steps
        can predate the session that is being written now. Measuring from the
        later start reported two seconds of elapsed time for a document
        spanning two days.
        """
        stamps = [s.started for s in self.steps if s.started]
        stamps += [n["at"] for n in self.notes if n.get("at")]
        earliest = min(stamps) if stamps else None
        if self.started is None:
            return earliest if earliest is not None else self.finished
        if earliest is not None and earliest < self.started:
            return earliest
        return self.started

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.failed]

    @property
    def busy_time(self) -> float:
        return sum(s.duration for s in self.steps if s.duration)

    @property
    def wall_time(self) -> float:
        if self.started or any(s.started for s in self.steps):
            return max(0.0, self.finished - self.began)
        return 0.0

    @property
    def reconstructed(self) -> list[Step]:
        """Steps read back off the screen rather than from the markers."""
        return [s for s in self.steps if s.source == "heuristic"]

    def reconstruction_notice(self) -> tuple[str, str]:
        """Headline and detail for the part of this document read off a screen.

        Returned as plain prose in two pieces, so each writer can emphasise
        the headline in its own way rather than one of them printing the
        other's markup.
        """
        count = len(self.reconstructed)
        if count == len(self.steps):
            return ("Reconstructed transcript.",
                    "This session was recorded without shell integration, so "
                    "commands were read back off the screen and may carry "
                    "prompt-redraw artifacts.")
        was = "it was" if count == 1 else "they were"
        return ("Partly reconstructed.",
                f"{plural(count, 'command')} came from a pane recorded without "
                f"shell integration, so {was} read back off the screen and may "
                "carry prompt-redraw artifacts.")

    def programs(self, include_trivial: bool = False) -> list[str]:
        """Distinct programs run, in first-use order."""
        seen: list[str] = []
        for step in self.steps:
            for part in commands_in(step.cmd):
                name = base_command(part)
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
            "ended": self.finished,
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


def _backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks inside the block.

    `cat`ting a markdown file closed the ```console block on its first inner
    fence, so the rest of that command's output rendered as prose.
    """
    return "`" * max(3, _backtick_run(text) + 1)


def _inline_code(text: str) -> str:
    """Span-level code that survives backticks in the command line."""
    tick = "`" * max(1, _backtick_run(text) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


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
        f"date: {_date(rec.began)}",
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
    # The machine matters when you record on more than one; the HTML page and
    # the JSON both carried it and the default format did not.
    if rec.host:
        facts.append(f"- **Host**: `{rec.host}`")
    if rec.shell:
        facts.append(f"- **Shell**: `{rec.shell}`")
    if rec.notes:
        facts.append(f"- **Notes**: {len(rec.notes)}")
    programs = rec.programs()
    if programs:
        facts.append("- **Programs**: " + ", ".join(f"`{p}`" for p in programs))
    # The summary is derived from the steps, so it lives inside the block that
    # gets regenerated. Left outside it, a re-export kept the first run's
    # numbers forever - a header claiming one command above a body listing
    # four.
    out += [GEN_BEGIN, ""] + facts + [""]

    # Notes alone are a recording worth keeping: the other three writers
    # already rendered them, markdown announced "Notes: 1" and dropped them.
    if not rec.steps and not rec.notes:
        out += ["> No commands were captured in this session.", "", GEN_END, ""]
        return "\n".join(out)

    # The warning used to appear only when *every* step was reconstructed, so a
    # session mixing an integrated pane with one recorded without integration
    # said nothing at all about the half that was read off the screen.
    if rec.reconstructed:
        headline, detail = rec.reconstruction_notice()
        out += [f"> **{headline}** {detail}", ""]

    index = 0
    current_pane = None
    for kind, item in rec.timeline():
        if kind == "step" and rec.panes > 1 and item.pane != current_pane:
            current_pane = item.pane
            out += [f"**— pane {pane_label(current_pane)} —**", ""]
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
            _inline_code(step.cwd) if step.cwd else None,
            f"pane {pane_label(step.pane)}" if step.pane and rec.panes > 1 else None,
        ) if m]
        out.append(f"### {index}. {_inline_code(step.cmd)}"
                   + (" ⚠️" if step.failed else ""))
        out.append("")
        if meta:
            out += [" · ".join(meta), ""]
        fence = _fence(step.cmd + "\n" + (step.output or ""))
        out.append(fence + "console")
        out.append(f"{prompt} {step.cmd}")
        if step.output:
            out.append(step.output)
        out += [fence, ""]

    out += [GEN_END, ""]
    return "\n".join(out)


def to_json(rec: Recording) -> str:
    return json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n"


def to_text(rec: Recording) -> str:
    prompt = config.settings.prompt
    out = [f"# {rec.label}  ({plural(len(rec.steps), 'command')}, "
           f"{len(rec.failed)} failed)", ""]
    current_pane = None
    for kind, item in rec.timeline():
        # Panes interleave by time, and without a marker two tabs read as one
        # shell running everything in sequence - so a `tail -f` in one tab
        # looked as though it had exited before the next command started.
        if kind == "step" and rec.panes > 1 and item.pane != current_pane:
            current_pane = item.pane
            out += [f"# --- pane {pane_label(current_pane)} ---", ""]
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
  --bg:#faf9f7; --panel:#fff; --fg:#1b1b19; --muted:#6d6c66; --faint:#93928b;
  --line:#e5e3dd; --rail:#dcd9d1; --code-bg:#f5f4f0; --code-fg:#26251f;
  --accent:#2f6f4f; --bad:#b0402c; --bad-bg:#fbf0ed; --bad-line:#e8c4ba;
  --tag:#f6efd8; --tag-line:#dcc98c; --tag-fg:#5a4a1c;
  --radius:10px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#141519; --panel:#1b1d22; --fg:#e7e6e0; --muted:#9c9b94; --faint:#74736d;
    --line:#2b2d33; --rail:#33353c; --code-bg:#0f1013; --code-fg:#d8d7d0;
    --accent:#7fbf9a; --bad:#e2836c; --bad-bg:#241a17; --bad-line:#4a2f27;
    --tag:#221e13; --tag-line:#584c26; --tag-fg:#d8c78d;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; padding:2rem 1.25rem 6rem; background:var(--bg); color:var(--fg);
  font:15px/1.65 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif;
}
main{max-width:62rem;margin:0 auto}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}

/* deck ------------------------------------------------------------- */
.deck{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;margin-bottom:1.5rem}
.reel{
  width:38px;height:38px;border-radius:50%;flex:none;
  border:3px solid var(--rail);position:relative;background:var(--panel);
}
.reel::after{
  content:"";position:absolute;inset:11px;border-radius:50%;
  background:var(--accent);opacity:.85;
}
.titles{flex:1 1 16rem;min-width:0}
h1{font-size:1.5rem;line-height:1.2;margin:0;letter-spacing:-.015em;word-break:break-word}
.sub{margin:.2rem 0 0;color:var(--muted);font-size:.85rem}
.controls{display:flex;gap:.4rem;flex-wrap:wrap}
.controls button{
  font:inherit;font-size:.78rem;padding:.3rem .7rem;border-radius:999px;
  border:1px solid var(--line);background:var(--panel);color:var(--muted);
  cursor:pointer;transition:background .12s,color .12s,border-color .12s;
}
.controls button:hover{color:var(--fg);border-color:var(--rail)}
.controls button[aria-pressed="true"]{
  background:var(--fg);border-color:var(--fg);color:var(--bg);
}

/* stats ------------------------------------------------------------ */
.stats{
  display:flex;flex-wrap:wrap;list-style:none;padding:0;margin:0 0 2rem;
  background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);overflow:hidden;
}
.stats li{
  flex:1 1 auto;min-width:7rem;padding:.7rem .9rem;
  border-right:1px solid var(--line);border-bottom:1px solid var(--line);
}
.stats li:last-child{border-right:0}
.stats .k{display:block;font-size:.7rem;letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
.stats .v{font-size:1.05rem;font-weight:600;font-variant-numeric:tabular-nums}
.stats .s{display:block;font-size:.72rem;color:var(--faint);margin-top:.05rem}
.stats li.bad .v{color:var(--bad)}

/* tape ------------------------------------------------------------- */
.tape{list-style:none;margin:0;padding:0 0 0 1.6rem;border-left:2px solid var(--rail)}
.entry{position:relative;margin:0 0 1.1rem}
.entry::before{
  content:"";position:absolute;left:-1.95rem;top:1.05rem;width:9px;height:9px;
  border-radius:50%;background:var(--rail);border:2px solid var(--bg);
}
.step.failed::before{background:var(--bad)}
.card{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);overflow:hidden}
.step.failed .card{border-color:var(--bad-line)}
.card>header{
  display:flex;gap:.75rem;align-items:baseline;flex-wrap:wrap;
  padding:.65rem .85rem;border-bottom:1px solid var(--line);
}
.step.failed .card>header{background:var(--bad-bg);border-bottom-color:var(--bad-line)}
.num{color:var(--faint);font-size:.78rem;font-variant-numeric:tabular-nums;flex:none}
.cmd{font-size:.9rem;font-weight:600;word-break:break-word;min-width:0}
.step.failed .cmd{color:var(--bad)}
.meta{
  margin-left:auto;color:var(--muted);font-size:.75rem;
  font-variant-numeric:tabular-nums;display:flex;gap:.5rem;flex-wrap:wrap;
}
.meta .sep{color:var(--faint)}
.meta b{color:var(--bad);font-weight:600}
pre{
  margin:0;padding:.8rem .85rem;background:var(--code-bg);color:var(--code-fg);
  font-size:.82rem;line-height:1.55;overflow-x:auto;
}
pre .p{color:var(--accent);user-select:none}
body.wrap pre{white-space:pre-wrap;word-break:break-word;overflow-x:visible}

/* notes ------------------------------------------------------------ */
.note::before{background:var(--tag-line)}
.note .card{background:var(--tag);border-color:var(--tag-line)}
.note .body{padding:.7rem .85rem;color:var(--tag-fg);white-space:pre-wrap}
.note .k{
  display:block;font-size:.66rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--tag-line);margin-bottom:.25rem;
}

/* pane breaks ------------------------------------------------------ */
.pane-break{
  position:relative;margin:1.6rem 0 1.1rem;color:var(--faint);
  font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;
}
.pane-break::before{
  content:"";position:absolute;left:-1.95rem;top:.55rem;width:9px;height:9px;
  border-radius:2px;background:var(--rail);border:2px solid var(--bg);
}

body.failed-only .step:not(.failed),
body.failed-only .note,
body.failed-only .pane-break{display:none}
.empty{color:var(--muted);font-style:italic}
footer{margin-top:2.5rem;color:var(--faint);font-size:.75rem}
"""

HTML_JS = """
(function () {
  var body = document.body;
  function store(key, on) { try { localStorage.setItem(key, on ? "1" : "0"); } catch (e) {} }
  function load(key) { try { return localStorage.getItem(key) === "1"; } catch (e) { return false; } }
  document.querySelectorAll("[data-toggle]").forEach(function (button) {
    var name = button.getAttribute("data-toggle");
    var key = "sectape:" + name;
    function apply(on) {
      body.classList.toggle(name === "wrap" ? "wrap" : "failed-only", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    }
    apply(load(key));
    button.addEventListener("click", function () {
      var on = button.getAttribute("aria-pressed") !== "true";
      apply(on);
      store(key, on);
    });
  });
})();
"""


def to_html(rec: Recording) -> str:
    """A self-contained page: no external assets, readable in either theme."""
    esc = html.escape
    prompt = config.settings.prompt

    # Kept to five tiles so they stay on one row at ordinary widths.
    stats = [("commands", str(len(rec.steps)), "", False)]
    if rec.failed:
        stats.append(("failed", str(len(rec.failed)), "", True))
    if rec.notes:
        stats.append(("notes", str(len(rec.notes)), "", False))
    if rec.panes > 1:
        stats.append(("panes", str(rec.panes), "", False))
    if rec.wall_time:
        stats.append(("elapsed", human_duration(rec.wall_time),
                      f"{human_duration(rec.busy_time)} in commands"
                      if rec.busy_time else "", False))

    sub = " · ".join(x for x in (_date(rec.began), rec.host, rec.shell) if x)

    body = ['<header class="deck">', '<div class="reel"></div>',
            f'<div class="titles"><h1>{esc(rec.label)}</h1>'
            f'<p class="sub">{esc(sub)}</p></div>',
            '<div class="controls">'
            '<button type="button" data-toggle="wrap" aria-pressed="false">wrap</button>'
            '<button type="button" data-toggle="failed" aria-pressed="false">'
            'failed only</button></div>',
            "</header>", '<ul class="stats">']
    for key, value, sub, bad in stats:
        css = ' class="bad"' if bad else ""
        extra = f'<span class="s">{esc(sub)}</span>' if sub else ""
        body.append(f'<li{css}>'
                    f'<span class="k">{esc(key)}</span>'
                    f'<span class="v">{esc(value)}</span>{extra}</li>')
    body.append("</ul>")

    if not rec.steps and not rec.notes:
        body.append('<p class="empty">No commands were captured in this session.</p>')
        return _html_page(rec.label, body)

    if rec.reconstructed:
        headline, detail = rec.reconstruction_notice()
        body.append(f'<p class="empty"><strong>{esc(headline)}</strong> '
                    f'{esc(detail)}</p>')

    body.append('<ol class="tape">')
    index = 0
    current_pane = None
    for kind, item in rec.timeline():
        if kind == "note":
            stamp = _stamp(item["at"])
            body.append(
                '<li class="entry note"><div class="card">'
                f'<div class="body"><span class="k">note'
                f'{" · " + esc(stamp) if stamp else ""}</span>'
                f'{esc(str(item["text"]))}</div></div></li>')
            continue

        step = item
        if rec.panes > 1 and step.pane != current_pane:
            current_pane = step.pane
            body.append(f'<li class="pane-break">pane {esc(pane_label(current_pane))}</li>')
        index += 1
        bits = []
        if step.started:
            bits.append(esc(_stamp(step.started)))
        if step.exit_code is not None:
            bits.append("exit 0" if step.exit_code == 0
                        else f"<b>exit {step.exit_code}</b>")
        if step.duration:
            bits.append(esc(human_duration(step.duration)))
        if step.cwd:
            bits.append(esc(step.cwd))
        # The pane breaks are hidden in the failed-only view, which is exactly
        # when you are comparing what went wrong across tabs - so each step
        # carries its own pane, as the markdown export already did.
        if step.pane and rec.panes > 1:
            bits.append(esc(f"pane {pane_label(step.pane)}"))
        meta = '<span class="sep">·</span>'.join(f"<span>{b}</span>" for b in bits)

        rendered = f'<span class="p">{esc(prompt)}</span> {esc(step.cmd)}'
        if step.output:
            rendered += "\n" + esc(step.output)
        body.append(
            f'<li class="entry step{" failed" if step.failed else ""}">'
            f'<div class="card"><header>'
            f'<span class="num">{index}</span>'
            f'<span class="cmd mono">{esc(step.cmd)}</span>'
            f'<span class="meta">{meta}</span></header>'
            f"<pre>{rendered}</pre></div></li>")
    body.append("</ol>")
    body.append(f'<footer>recorded with sectape{esc(" " + VERSION) if VERSION else ""}'
                "</footer>")
    return _html_page(rec.label, body)


def _html_page(title: str, body: list[str]) -> str:
    return ('<!doctype html>\n<html lang="en">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{html.escape(title)}</title>\n"
            f"<style>{HTML_CSS}</style>\n</head>\n<body>\n<main>\n"
            + "\n".join(body)
            + f"\n</main>\n<script>{HTML_JS}</script>\n</body>\n</html>\n")


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

FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
# A summary line as *sectape* wrote it before the summary moved inside the
# block. Deliberately narrow, so a reader's own bullet list is never eaten.
STALE_FACT_RE = re.compile(
    r"^- \*\*(?:Commands|Non-zero exits|Panes|Notes)\*\*: \d+$"
    r"|^- \*\*(?:Elapsed|Time in commands)\*\*: \S.*$"
    r"|^- \*\*(?:Shell|Programs)\*\*: `.+$")


def _refresh_head(head: str, new_text: str) -> str:
    """Bring the part before the block up to date without losing prose."""
    old_front = FRONTMATTER_RE.match(head)
    new_front = FRONTMATTER_RE.match(new_text)
    if old_front and new_front:
        head = new_front.group(0) + head[old_front.end():]
    lines = head.split("\n")

    def drop_blanks():
        while lines and not lines[-1].strip():
            lines.pop()

    # Documents written before the summary moved inside the block still carry
    # it here, where it would now sit stale above a fresh copy.
    drop_blanks()
    removed = False
    while lines and STALE_FACT_RE.match(lines[-1]):
        lines.pop()
        drop_blanks()
        removed = True
    if not removed:
        return head              # nothing of ours to clean up; leave it be
    return "\n".join(lines) + "\n\n"


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
    # The *last* end marker is the real one. A recording that `cat`s an older
    # export captures the marker as ordinary output, and splitting on the
    # first one truncated the document at that command, silently dropping
    # every step after it.
    head, rest = existing.split(GEN_BEGIN, 1)
    _, tail = rest.rsplit(GEN_END, 1)
    inner = new_text.split(GEN_BEGIN, 1)[1].rsplit(GEN_END, 1)[0]
    head = _refresh_head(head, new_text)
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
    # An export is meant to be read by someone else, so it follows the umask
    # like any other document - mkstemp's 0600 left it unshareable. A file
    # that already exists keeps whatever permissions it was given.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = umask_mode()
    write_text_atomic(path, merge(text, path), mode)
    return path
