"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__, config
from .config import ConfigError
from .formats import WRITERS, Recording, export, filter_steps, render
from .markers import ZSH_HOOKS
from .recorder import (SUPPORTED_SHELLS, chosen_shell,
                       integration_available, record_pty)
from .session import (add_note, allocate_pane, clear_session_if_idle,
                      ensure_session_dir, live_panes, pane_label, read_notes,
                      read_session, read_session_meta, resolve_session_dir,
                      signal_panes, unregister_pane, wait_for_panes,
                      write_session_meta)
from .terminal import current_size
from .text import redact, trim_for_export
from .transcript import collect_steps, count_commands
from .util import (human_duration, pid_alive, plural, safe_filename,
                   short_path, slugify, write_json_atomic)

from .ui import Style, fit, style_for


def ui() -> Style:
    return style_for()


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def _default_label() -> str:
    return time.strftime("session-%Y%m%d-%H%M%S")


def _load_recording(session_dir: Path, label: str | None = None,
                    meta: dict | None = None) -> Recording:
    steps, panes = collect_steps(session_dir, do_redact=config.settings.redact)
    meta = meta or read_session_meta(session_dir)
    label = label or meta.get("label") or session_dir.name
    # Notes end up in the same shared document as the transcript, so they get
    # the same scrubbing and the same size limits. Only the export is
    # affected; notes.jsonl keeps what you wrote, in full.
    notes = [dict(note,
                  text=trim_for_export(redact(note["text"],
                                              config.settings.redact)))
             for note in read_notes(session_dir)]
    return Recording(
        label=label,
        steps=steps,
        panes=panes,
        started=meta.get("started"),
        shell=meta.get("shell", ""),
        host=meta.get("host", ""),
        notes=notes,
    )


def _release_pane(pane_id: str, verb: str) -> int:
    """Deregister a finished pane and, if it was the last one, end the session.

    Both `rec` and `attach` end this way. When only `rec` did, leaving the
    first tab before the attached one stranded the recording: no panes left,
    no export written, and `current.json` still on disk claiming to be live.
    """
    u = ui()
    remaining = unregister_pane(pane_id)
    if remaining:
        still = u.grey("· " + plural(remaining, "pane") + " still recording")
        print(f"\n  {u.grey(u.g('stop'))} pane {pane_label(pane_id)} {verb} {still}")
        print(f"    {u.grey('run')} {u.bold('sectape stop')} "
              f"{u.grey('when the session is finished')}")
        return 0
    print()
    session = read_session()
    if session:
        _finish(session, quiet=False)
        clear_session_if_idle()
    return 0


def cmd_rec(args) -> int:
    config.ensure_dirs()
    u = ui()

    if os.environ.get("SECTAPE_ACTIVE"):
        print(f"  {u.red(u.g('warn'))} this shell is already being recorded.")
        print(f"    open a new tab and run {u.bold('sectape attach')}, "
              f"or {u.bold('exit')} first.")
        return 1

    label = " ".join(args.label).strip() if args.label else _default_label()
    slug = slugify(label)
    session_dir = ensure_session_dir(config.settings.sessions_dir / slug)

    existing = read_session()
    live = len(live_panes(existing or {}))

    rejoining = bool(existing and existing.get("slug") == slug)
    if rejoining and live:
        print(f"  {u.yellow(u.g('reel'))} rejoining {u.bold(str(existing.get('label')))} "
              f"{u.grey('(' + plural(live, 'pane') + ' already recording)')}")
    elif existing and not rejoining:
        if live:
            # They have to actually stop. Left running, their recorders would
            # deregister themselves out of the *new* session's pane registry
            # when they eventually exit, since there is only one of those.
            print(f"  {u.yellow(u.g('warn'))} {u.bold(str(existing.get('label')))} "
                  f"still has {plural(live, 'live pane')}; stopping it first.")
            signal_panes(existing)
            left = wait_for_panes()
            if left:
                print(f"    {u.yellow(u.g('warn'))} "
                      f"{plural(left, 'pane')} did not stop.")
        if config.settings.current_session_file.exists():
            _finish(read_session() or existing, quiet=False)

    if not rejoining:
        write_json_atomic(config.settings.current_session_file, {
            "label": label,
            "slug": slug,
            "dir": str(session_dir),
            "started": time.time(),
            "shell": os.environ.get("SHELL", ""),
            "host": os.uname().nodename,
            "version": __version__,
            "panes": {},
        })

    earlier = len(list(session_dir.glob("pane_*.raw")))
    if earlier and not rejoining:
        # Recording under a label that already has logs appends to it: the
        # export will hold both, and the older panes will be numbered as if
        # they had been open alongside this one.
        print(f"  {u.yellow(u.g('warn'))} {u.bold(label)} already holds "
              f"{plural(earlier, 'recorded pane')}; this one is added to it "
              f"and the export will contain both.")
        print(f"    {u.grey('start a separate recording with a new label, or')} "
              f"{u.bold('sectape rm ' + slug)} {u.grey('first')}")

    write_session_meta(session_dir, read_session() or {})
    pane_id, pane_log = allocate_pane(session_dir)

    rows, cols, _, _ = current_size()
    no_integration = args.no_integration or not config.settings.shell_integration
    integration = "on" if integration_available(no_integration) else "off"
    lines = ["", *u.deck("rec", label)]
    lines.append(u.field("pane", f"{pane_label(pane_id)}  "
                                 f"{u.dim(u.g('dot'))} {cols}×{rows} "
                                 f"{u.dim(u.g('dot'))} integration {integration}"))
    lines.append(u.field("tape", u.grey(short_path(pane_log))))
    lines.append("")
    lines.append(u.hint(('note "…"', "annotate"),
                        ("sectape attach", "another pane"),
                        ("exit", "finish")))
    lines.append("")
    banner = "\n".join(lines)

    record_pty(pane_log, banner, no_integration=no_integration)

    return _release_pane(pane_id, "stopped")


def cmd_attach(args) -> int:
    config.ensure_dirs()
    u = ui()

    if os.environ.get("SECTAPE_ACTIVE"):
        print(f"  {u.red(u.g('warn'))} this shell is already being recorded.")
        return 1

    session = read_session()
    if not session:
        print("No active session. Start one with: sectape rec [label]")
        return 1

    session_dir = ensure_session_dir(session.get("dir") or
                                     (config.settings.sessions_dir
                                      / session.get("slug", "")))
    pane_id, pane_log = allocate_pane(session_dir)

    banner = "\n".join([
        "", *u.deck("rec", str(session.get("label"))),
        u.field("pane", f"{pane_label(pane_id)}  {u.grey('attached')}"),
        u.field("tape", u.grey(short_path(pane_log))),
        "",
        u.hint(('note "…"', "annotate"), ("exit", "leave this pane")),
        "",
    ])
    record_pty(pane_log, banner,
               no_integration=args.no_integration or not config.settings.shell_integration)

    return _release_pane(pane_id, "detached")


def _finish(session: dict, quiet: bool, fmt: str | None = None) -> Path | None:
    u = Style(colour=False) if quiet else ui()
    label = session.get("label", "session")
    session_dir = Path(session.get("dir") or
                       (config.settings.sessions_dir / session.get("slug", "")))
    rec = _load_recording(session_dir, label, session)
    if not rec.steps and not rec.notes:
        if not quiet:
            for line in u.deck("stop", label):
                print(line)
            print(u.counter(u.grey("nothing captured")))
            print()
        return None
    path = export(rec, fmt)
    if not quiet:
        reconstructed = len(rec.reconstructed)
        parts = [plural(len(rec.steps), "command")]
        if rec.failed:
            parts.append(u.red(f"{len(rec.failed)} failed"))
        if rec.notes:
            parts.append(plural(len(rec.notes), "note"))
        if rec.panes > 1:
            parts.append(plural(rec.panes, "pane"))
        if rec.wall_time:
            parts.append(human_duration(rec.wall_time))
        if reconstructed:
            parts.append(u.yellow("reconstructed" if reconstructed == len(rec.steps)
                                  else f"{reconstructed} reconstructed"))
        for line in u.deck("stop", label):
            print(line)
        print(u.counter(*parts))
        print(f"    {u.green(u.g('arrow'))} {short_path(path)}")
        print()
    return path


def _confirm(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def cmd_stop(args) -> int:
    config.ensure_dirs()
    session = read_session()
    if not session:
        print("No active session. Use `sectape export <session>` for an old one.")
        return 1

    inside = bool(os.environ.get("SECTAPE_ACTIVE"))
    live = live_panes(session)

    # Called from inside a recorded shell: the recorders own the export, so
    # signal them and let them unwind - that also closes the shell we are in.
    if inside and live:
        signal_panes(session)
        print(f"stopping {plural(len(live), 'pane')}…")
        return 0

    if live:
        count = len(live)
        # --force means "yes, stop them too, don't ask". It used to mean
        # "delete the session and leave the recorders running", which left the
        # user in a shell still logging to a session that no longer existed.
        stop_them = args.force or _confirm(
            f"{plural(count, 'pane')} still recording. Stop them too?", default=False)
        if stop_them:
            signal_panes(session)
            left = wait_for_panes()
            if left:
                print(f"{plural(left, 'pane')} did not stop; exporting anyway.")
            session = read_session() or session
        else:
            _finish(session, quiet=False, fmt=args.format)
            print(f"  {plural(count, 'pane')} still recording, "
                  "so the session stays open.")
            return 0

    _finish(session, quiet=False, fmt=args.format)
    if config.settings.current_session_file.exists():
        config.settings.current_session_file.unlink()
    return 0


# --------------------------------------------------------------------------
# reading back
# --------------------------------------------------------------------------

def session_name(session_dir: Path) -> str:
    """What to call a recording on screen.

    The directory is a slug, and for a label with no ASCII in it that slug is
    a digest - unreadable on its own. The recording knows its own name.
    """
    return read_session_meta(session_dir).get("label") or session_dir.name


def sessions_ordered() -> list[Path]:
    """Recordings newest first - the order `list` prints and indexes."""
    root = config.settings.sessions_dir
    if not root.exists():
        return []
    return sorted((d for d in root.iterdir()
                   if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: d.stat().st_mtime, reverse=True)


def resolve_target(name: str) -> Path | None:
    """Accept the number `list` printed, or a session name."""
    name = (name or "").strip()
    if not name:
        return None
    if name.isdigit():
        ordered = sessions_ordered()
        index = int(name)
        if 1 <= index <= len(ordered):
            return ordered[index - 1]
        return None
    return resolve_session_dir(name)


def _resolve(name: str) -> Path | None:
    if name:
        return resolve_target(name)
    session = read_session() or {}
    location = session.get("dir")
    if location:
        return Path(location)
    if session.get("slug"):
        return config.settings.sessions_dir / session["slug"]
    return None


def _apply_filters(rec: Recording, args) -> Recording:
    rec.steps = filter_steps(
        rec.steps,
        only_failed=getattr(args, "only_failed", False),
        last=getattr(args, "last", None),
        grep=getattr(args, "grep", "") or "",
        drop_output=getattr(args, "no_output", False),
    )
    return rec


def _filter_suffix(args) -> str:
    """How a filtered export differs from the whole recording, in a few words."""
    bits = []
    if getattr(args, "only_failed", False):
        bits.append("failed")
    if getattr(args, "last", None):
        bits.append(f"last {args.last}")
    if getattr(args, "grep", ""):
        bits.append("filtered")
    if getattr(args, "no_output", False):
        bits.append("commands")
    return ", ".join(bits)


def cmd_export(args) -> int:
    config.ensure_dirs()
    name = " ".join(args.session).strip() if args.session else ""
    session_dir = _resolve(name)
    if session_dir is None or not session_dir.exists():
        print(f"No recording found for {name or 'the active session'!r}.")
        return 1
    rec = _apply_filters(_load_recording(session_dir), args)
    dest = Path(args.output).expanduser() if args.output else None
    if dest is not None and dest.is_dir():
        print(f"error: {dest} is a directory; -o wants a file path.",
              file=sys.stderr)
        return 1
    narrowed = _filter_suffix(args)
    if dest is None and narrowed:
        # A filtered export is a subset, and writing it to the recording's own
        # file replaced the complete document with it - four commands down to
        # one, with no warning. Give the subset its own name; -o still puts it
        # wherever you say.
        fmt = args.format or config.settings.format
        suffix = WRITERS[fmt][1] if fmt in WRITERS else ".md"
        dest = (config.settings.output_dir
                / f"{safe_filename(rec.label)} ({narrowed}){suffix}")
    path = export(rec, args.format, dest)
    print(path)
    return 0


def cmd_show(args) -> int:
    config.ensure_dirs()
    name = " ".join(args.session).strip() if args.session else ""
    session_dir = _resolve(name)
    if session_dir is None or not session_dir.exists():
        print(f"No recording found for {name or 'the active session'!r}.")
        return 1
    rec = _apply_filters(_load_recording(session_dir), args)
    text, _ = render(rec, args.format or "text")
    sys.stdout.write(text)
    return 0


def cmd_note(args) -> int:
    config.ensure_dirs()
    text = " ".join(args.text).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        print("Nothing to note. Pass some text, or pipe it in.")
        return 1

    session = read_session()
    if not session:
        print("No active session; start one with `sectape rec`.")
        return 1
    path = add_note(text)
    if path is None:
        print("Could not write the note.")
        return 1
    u = ui()
    first = text.split("\n")[0]
    print(f"  {u.green(u.g('note'))} {u.grey('noted')} "
          f"{first[:72]}{'…' if len(first) > 72 else ''}")
    return 0


def cmd_list(args) -> int:
    config.ensure_dirs()
    dirs = sessions_ordered()
    if args.json:
        print(json.dumps([{
            "index": i,
            "session": d.name,
            "label": session_name(d),
            "panes": len(list(d.glob("pane_*.raw"))),
            "commands": count_commands(d),
            "notes": len(read_notes(d)),
            "modified": d.stat().st_mtime,
        } for i, d in enumerate(dirs, 1)], indent=2))
        return 0

    u = ui()
    print()
    if not dirs:
        print("\n".join(u.deck("stop", "no recordings")))
        print(f"\n    {u.grey('start one with')} {u.bold('sectape rec')}\n")
        return 0

    active = read_session() or {}
    active_dir = active.get("dir")
    print("\n".join(u.deck("pause", f"recordings {u.grey(f'({len(dirs)})')}")))
    # One template for the header and every row, so the columns cannot drift
    # apart - and `fit` measures screen columns, so a name in Japanese or with
    # an emoji in it lines up like any other.
    row = "  {mark} {index}  {name}{cmds}{notes}{panes}   {when}"
    print(u.grey(row.format(mark=" ", index=" #", name=fit("session", 33),
                            cmds="cmds".rjust(4), notes="notes".rjust(7),
                            panes="panes".rjust(7), when="last activity")))
    for i, d in enumerate(dirs, 1):
        count = count_commands(d)
        notes = len(read_notes(d))
        panes = len(list(d.glob("pane_*.raw")))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime))
        running = str(d) == str(active_dir)
        print(row.format(
            mark=u.red(u.g("rec")) if running else " ",
            index=u.bold(str(i).rjust(2)),
            name=fit(session_name(d), 33),
            cmds=("?" if count is None else str(count)).rjust(4),
            notes=str(notes).rjust(7),
            panes=str(panes).rjust(7),
            when=u.grey(when)))
    print(f"\n    {u.grey('show one with')} {u.bold('sectape show 1')} "
          f"{u.grey('or by name')}\n")
    return 0


def snapshot(session: dict | None) -> dict:
    snap = {"version": __version__, "active": bool(session),
            "state_dir": str(config.settings.state_dir),
            "output_dir": str(config.settings.output_dir),
            "format": config.settings.format}
    if not session:
        return snap
    panes = {k: v for k, v in (session.get("panes") or {}).items()
             if pid_alive(v.get("pid", -1))}
    session_dir = Path(session.get("dir") or
                       (config.settings.sessions_dir / session.get("slug", "")))
    snap.update({
        "label": session.get("label"),
        "slug": session.get("slug"),
        "started": session.get("started"),
        "panes": [{"id": k, "pid": v.get("pid"), "log": v.get("log")}
                  for k, v in sorted(panes.items())],
        "commands": count_commands(session_dir),
    })
    return snap


def cmd_status(args) -> int:
    config.ensure_dirs()
    session = read_session()
    if args.json:
        print(json.dumps(snapshot(session), indent=2))
        return 0
    u = ui()
    print()
    if not session:
        for line in u.deck("stop", "idle"):
            print(line)
        print(u.field("output", f"{short_path(config.settings.output_dir)} "
                                f"{u.grey('(' + config.settings.format + ')')}"))
        print(u.field("config", short_path(config.settings.config_path)
                      if config.settings.config_path else u.grey("defaults")))
        print(f"\n    {u.grey('start one with')} {u.bold('sectape rec')}\n")
        return 0

    panes = live_panes(session)
    print("\n".join(u.deck("rec" if panes else "pause", str(session.get("label")))))
    session_dir = Path(session.get("dir") or
                       (config.settings.sessions_dir / session.get("slug", "")))
    count = count_commands(session_dir)
    running = (human_duration(time.time() - session["started"])
               if session.get("started") else "")
    print(u.counter("? commands" if count is None else plural(count, "command"),
                    plural(len(panes), "live pane"), running))
    for pane_id, pane in sorted(panes.items()):
        log = Path(pane.get("log", ""))
        size = log.stat().st_size / 1024 if log.exists() else 0
        pid = pane.get("pid")
        print(f"      {u.dim(u.g('bar'))} pane {pane_label(pane_id)}  "
              f"{u.grey('pid ' + str(pid))}  {u.grey(f'{size:.1f} KiB')}")
    print(u.field("output", f"{short_path(config.settings.output_dir)} "
                            f"{u.grey('(' + config.settings.format + ')')}"))
    print()
    return 0


def cmd_rm(args) -> int:
    config.ensure_dirs()
    name = " ".join(args.session).strip()
    session_dir = resolve_target(name)
    if session_dir is None:
        print(f"No recording matches {name!r}.")
        return 1
    if session_dir.name != name:
        print(f"{name!r} matched recording {session_dir.name!r}.")
    active = read_session()
    if active and Path(active.get("dir", "")) == session_dir:
        print("That session is still active; run `sectape stop` first.")
        return 1
    if not args.yes:
        print(f"Would delete {session_dir} "
              f"({len(list(session_dir.glob('pane_*.raw')))} pane logs).")
        print("Re-run with --yes to confirm.")
        return 0
    # This deletes a tree, so confirm one more time that it is a recording and
    # not something the resolver was talked into.
    if session_dir.resolve().parent != config.settings.sessions_dir.resolve():
        print(f"error: {session_dir} is not inside "
              f"{config.settings.sessions_dir}.", file=sys.stderr)
        return 1
    shutil.rmtree(session_dir)
    print(f"Deleted {session_dir}")
    return 0


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def cmd_config(args) -> int:
    path = config.config_path()
    if args.action == "path":
        print(path)
        return 0
    if args.action == "show":
        s = config.settings
        for key in ("state_dir", "output_dir", "format", "prompt", "redact",
                    "shell_integration", "max_output_lines", "max_output_chars"):
            print(f"{key:20} {getattr(s, key)}")
        print(f"{'config_path':20} {s.config_path or '(defaults)'}")
        typos = config.unknown_keys(s.config_path) if s.config_path else []
        if typos:
            print(f"\nignored, not recognised: {', '.join(typos)}",
                  file=sys.stderr)
        return 0
    # init
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.TEMPLATE, encoding="utf-8")
    print(f"Wrote {path}")
    return 0


ZSH_COMPLETION = """#compdef sectape
# sectape zsh completion - install with:
#   sectape completion zsh > "${fpath[1]}/_sectape"
_sectape_sessions() {
  local dir="${SECTAPE_STATE_DIR:-$HOME/.sectape}/sessions"
  [[ -d $dir ]] && _values 'recording' ${(f)"$(ls -1 $dir 2>/dev/null)"}
}
_sectape() {
  local -a commands
  commands=(
    'rec:record a session' 'attach:record another pane'
    'stop:export and end the session' 'note:annotate the running session'
    'export:write a recording to a file' 'show:print a recording'
    'list:list recordings' 'status:show the active session'
    'rm:delete a recording' 'config:inspect or create the config'
    'doctor:check the install' 'completion:emit a completion script'
  )
  _arguments -C '1:command:->cmd' '*::arg:->args'
  case $state in
    cmd) _describe -t commands 'sectape command' commands ;;
    args)
      case $words[1] in
        export|show|rm) _sectape_sessions ;;
        *) _default ;;
      esac ;;
  esac
}
_sectape "$@"
"""

BASH_COMPLETION = """# sectape bash completion - install with:
#   sectape completion bash > /etc/bash_completion.d/sectape
_sectape() {
  local cur prev commands dir
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  commands="rec attach stop note export show list status rm config doctor completion"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
    return
  fi
  case "${COMP_WORDS[1]}" in
    export|show|rm)
      dir="${SECTAPE_STATE_DIR:-$HOME/.sectape}/sessions"
      COMPREPLY=( $(compgen -W "$(ls -1 "$dir" 2>/dev/null)" -- "$cur") ) ;;
    config)
      COMPREPLY=( $(compgen -W "init show path" -- "$cur") ) ;;
    *)
      COMPREPLY=( $(compgen -f -- "$cur") ) ;;
  esac
}
complete -F _sectape sectape
"""


def cmd_completion(args) -> int:
    print(ZSH_COMPLETION if args.shell == "zsh" else BASH_COMPLETION, end="")
    return 0


def cmd_doctor(args) -> int:
    config.ensure_dirs()
    u = ui()
    ok = True

    def check(label, passed, detail="", warn_only=False):
        nonlocal ok
        if passed:
            mark = u.green(u.g("tick"))
        else:
            mark = u.yellow("!") if warn_only else u.red(u.g("cross"))
        print(f"    {mark} {label.ljust(30)}{u.grey(detail)}")
        if not passed and not warn_only:
            ok = False

    print()
    print("\n".join(u.deck("pause", f"doctor {u.grey('sectape ' + __version__)}")))
    check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    check("platform supported", sys.platform != "win32", sys.platform)
    check("stdin is a tty", sys.stdin.isatty(),
          "" if sys.stdin.isatty() else "(only matters when recording)", warn_only=True)
    if sys.stdin.isatty():
        rows, cols, _, _ = current_size()
        check("terminal size readable", rows > 0 and cols > 0, f"{cols}x{rows}")

    for label, path in (("state dir", config.settings.state_dir),
                        ("output dir", config.settings.output_dir)):
        check(f"{label} writable", os.access(path, os.W_OK), str(path))

    shell, name = chosen_shell()
    check("shell supports integration", name in SUPPORTED_SHELLS,
          shell if os.environ.get("SHELL") else "(SHELL unset)", warn_only=True)
    check("integration hooks render", "_sectape_preexec" in ZSH_HOOKS.format(version=__version__))
    check("output format valid", config.settings.format in WRITERS, config.settings.format)
    if config.settings.config_path:
        typos = config.unknown_keys(config.settings.config_path)
        check("config keys recognised", not typos,
              ", ".join(typos) if typos else "", warn_only=True)

    session = read_session()
    if session:
        stale = [k for k, v in (session.get("panes") or {}).items()
                 if not pid_alive(v.get("pid", -1))]
        check("no stale panes", not stale,
              f"{plural(len(stale), 'dead pane')} will be pruned" if stale else "",
              warn_only=True)

    print()
    print(f"    {u.green('all good.')}" if ok
          else f"    {u.yellow('some checks failed (see above).')}")
    print()
    return 0 if ok else 1


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def _add_filters(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument("--only-failed", action="store_true",
                       help="keep only commands that exited non-zero")
    group.add_argument("--last", type=int, metavar="N",
                       help="keep only the last N commands")
    group.add_argument("--grep", metavar="RE",
                       help="keep commands whose text or output matches")
    group.add_argument("--no-output", action="store_true",
                       help="list the commands without their output")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sectape",
        description="Record a terminal session as commands, not bytes.")
    p.add_argument("--version", action="version", version=f"sectape {__version__}")
    p.add_argument("--config", metavar="PATH", help="configuration file to use")
    p.add_argument("--state-dir", metavar="DIR", help="override the state directory")
    p.add_argument("--output-dir", metavar="DIR", help="override the output directory")
    p.add_argument("--no-redact", action="store_true",
                   help="keep private keys and API tokens verbatim in exports")
    sub = p.add_subparsers(dest="command")

    rec = sub.add_parser("rec", aliases=["start"], help="record a session")
    rec.add_argument("label", nargs="*", help="name for this recording")
    rec.add_argument("--no-integration", action="store_true",
                     help="skip shell hooks and read commands off the screen")
    rec.set_defaults(func=cmd_rec)

    att = sub.add_parser("attach", aliases=["join"], help="record another tab or pane")
    att.add_argument("--no-integration", action="store_true")
    att.set_defaults(func=cmd_attach)

    stop = sub.add_parser("stop", aliases=["finish"], help="export and end the session")
    stop.add_argument("-f", "--format", choices=sorted(WRITERS))
    stop.add_argument("--force", action="store_true",
                      help="stop any panes still recording, without asking")
    stop.set_defaults(func=cmd_stop)

    exp = sub.add_parser("export", help="write a recording to a file")
    exp.add_argument("session", nargs="*", help="session name (default: active)")
    exp.add_argument("-f", "--format", choices=sorted(WRITERS))
    exp.add_argument("-o", "--output", metavar="PATH", help="write here instead")
    _add_filters(exp)
    exp.set_defaults(func=cmd_export)

    show = sub.add_parser("show", aliases=["cat"], help="print a recording to stdout")
    show.add_argument("session", nargs="*")
    show.add_argument("-f", "--format", choices=sorted(WRITERS))
    _add_filters(show)
    show.set_defaults(func=cmd_show)

    note = sub.add_parser("note", help="annotate the running session")
    note.add_argument("text", nargs="*", help="the note (or pipe it on stdin)")
    note.set_defaults(func=cmd_note)

    lst = sub.add_parser("list", aliases=["ls"], help="list recordings")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    sta = sub.add_parser("status", help="show the active session")
    sta.add_argument("--json", action="store_true")
    sta.set_defaults(func=cmd_status)

    rm = sub.add_parser("rm", help="delete a recording's raw logs")
    rm.add_argument("session", nargs="+")
    rm.add_argument("--yes", action="store_true", help="actually delete")
    rm.set_defaults(func=cmd_rm)

    cfg = sub.add_parser("config", help="inspect or create the config file")
    cfg.add_argument("action", choices=["init", "show", "path"], nargs="?", default="show")
    cfg.add_argument("--force", action="store_true")
    cfg.set_defaults(func=cmd_config)

    comp = sub.add_parser("completion", help="emit a shell completion script")
    comp.add_argument("shell", choices=["zsh", "bash"])
    comp.set_defaults(func=cmd_completion)

    sub.add_parser("doctor", help="check the install").set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    try:
        chosen = Path(args.config).expanduser() if args.config else None
        # A missing *default* config is normal; a missing one you named on the
        # command line is a typo, and silently falling back to the defaults
        # hid it.
        if chosen is not None and not chosen.exists():
            raise ConfigError(f"{chosen}: no such configuration file")
        config.load(chosen)
        overrides = {}
        if args.state_dir:
            overrides["state_dir"] = Path(args.state_dir).expanduser()
        if args.output_dir:
            overrides["output_dir"] = Path(args.output_dir).expanduser()
        if args.no_redact:
            overrides["redact"] = False
        if getattr(args, "format", None):
            overrides["format"] = args.format
        if overrides:
            config.override(**overrides)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if not hasattr(args, "no_integration"):
        args.no_integration = False
    try:
        result = args.func(args) or 0
        # Flush inside the guard. Left to interpreter shutdown, a failing
        # flush escapes as "Exception ignored" and exit code 120 instead of
        # reaching the handlers below.
        sys.stdout.flush()
        return result
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # `sectape show | head`, or quitting `less` early. The reader going
        # away is not an error, and complaining about it is noise. stdout is
        # pointed at devnull so the interpreter's own shutdown flush cannot
        # raise again and print "Exception ignored".
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 128 + 13                 # what a SIGPIPE death would report
    except OSError as exc:
        # A full disk, a read-only output directory, a path that is really a
        # directory: ordinary conditions that used to end in a traceback.
        detail = getattr(exc, "strerror", None) or str(exc)
        where = getattr(exc, "filename", None)
        print(f"error: {detail}" + (f": {where}" if where else ""),
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130
