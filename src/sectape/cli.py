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
from .recorder import record_pty
from .session import (add_note, clear_session_if_idle, new_pane_id,
                      prune_dead_panes, read_notes, read_session,
                      register_pane, resolve_session_dir, unregister_pane)
from .terminal import current_size
from .transcript import collect_steps, count_commands
from .util import human_duration, pid_alive, slugify, write_json_atomic

GREEN, CYAN, YELLOW, RED, DIM, OFF = (
    "\033[1;32m", "\033[1;36m", "\033[1;33m", "\033[1;31m", "\033[2m", "\033[0m")


def _colour(enabled: bool):
    if enabled:
        return GREEN, CYAN, YELLOW, RED, DIM, OFF
    return ("",) * 6


def _use_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def _default_label() -> str:
    return time.strftime("session-%Y%m%d-%H%M%S")


def _load_recording(session_dir: Path, label: str, meta: dict | None = None) -> Recording:
    steps, panes = collect_steps(session_dir, do_redact=config.settings.redact)
    meta = meta or {}
    return Recording(
        label=label,
        steps=steps,
        panes=panes,
        started=meta.get("started"),
        shell=meta.get("shell", ""),
        host=meta.get("host", ""),
        notes=read_notes(session_dir),
    )


def cmd_rec(args) -> int:
    config.ensure_dirs()
    g, c, y, r, d, off = _colour(_use_colour())

    if os.environ.get("SECTAPE_ACTIVE"):
        print(f"{r}This shell is already being recorded.{off}")
        print("Open a new tab and run `sectape attach`, or `exit` first.")
        return 1

    label = " ".join(args.label).strip() if args.label else _default_label()
    slug = slugify(label)
    session_dir = config.settings.sessions_dir / slug
    session_dir.mkdir(parents=True, exist_ok=True)

    existing = read_session()
    live = len([p for p in ((existing or {}).get("panes") or {}).values()
                if pid_alive(p.get("pid", -1))])

    rejoining = bool(existing and existing.get("slug") == slug)
    if rejoining and live:
        print(f"{y}Rejoining the open session '{existing.get('label')}' "
              f"({live} pane(s) already recording).{off}")
    elif existing and not rejoining:
        if live:
            print(f"{y}Session '{existing.get('label')}' still has {live} live "
                  f"pane(s); finishing it first.{off}")
        _finish(existing, quiet=False)

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

    pane_id = new_pane_id()
    pane_log = session_dir / f"pane_{pane_id}.raw"
    register_pane(pane_id, pane_log)

    rows, cols, _, _ = current_size()
    integration = "off" if args.no_integration else "on"
    banner = (
        f"\n{g}● sectape {__version__} recording{off}  {d}pane #{pane_id}{off}\n"
        f"  label       {c}{label}{off}\n"
        f"  log         {pane_log}\n"
        f"  terminal    {cols}x{rows}   shell integration: {integration}\n"
        f"  {d}another tab? `sectape attach`   finish with `exit`{off}\n"
    )

    record_pty(pane_log, banner,
               no_integration=args.no_integration or not config.settings.shell_integration)

    remaining = unregister_pane(pane_id)
    print(f"\n{g}■ pane #{pane_id} stopped.{off}")
    if remaining:
        print(f"  {remaining} pane(s) still recording - run `sectape stop` when done.")
        return 0

    session = read_session()
    if session:
        _finish(session, quiet=False)
        clear_session_if_idle()
    return 0


def cmd_attach(args) -> int:
    config.ensure_dirs()
    g, c, y, r, d, off = _colour(_use_colour())

    if os.environ.get("SECTAPE_ACTIVE"):
        print(f"{r}This shell is already being recorded.{off}")
        return 1

    session = read_session()
    if not session:
        print("No active session. Start one with: sectape rec [label]")
        return 1

    session_dir = Path(session.get("dir") or
                       (config.settings.sessions_dir / session.get("slug", "")))
    session_dir.mkdir(parents=True, exist_ok=True)
    pane_id = new_pane_id()
    pane_log = session_dir / f"pane_{pane_id}.raw"
    register_pane(pane_id, pane_log)

    banner = (f"\n{g}● attached to '{session.get('label')}'{off}  {d}pane #{pane_id}{off}\n"
              f"  {d}`exit` leaves this pane{off}\n")
    record_pty(pane_log, banner,
               no_integration=args.no_integration or not config.settings.shell_integration)

    remaining = unregister_pane(pane_id)
    print(f"\n■ pane #{pane_id} detached. {remaining} pane(s) still recording.")
    return 0


def _finish(session: dict, quiet: bool, fmt: str | None = None) -> Path | None:
    g, c, y, r, d, off = _colour(_use_colour() and not quiet)
    label = session.get("label", "session")
    session_dir = Path(session.get("dir") or
                       (config.settings.sessions_dir / session.get("slug", "")))
    rec = _load_recording(session_dir, label, session)
    if not rec.steps and not quiet:
        print(f"{y}No commands were captured; nothing exported.{off}")
        return None
    path = export(rec, fmt)
    if not quiet:
        marked = sum(1 for s in rec.steps if s.source == "marker")
        how = "shell integration" if marked else "prompt heuristics"
        print(f"{g}✓ {path}{off}")
        print(f"  {len(rec.steps)} commands from {rec.panes} pane(s) via {how}"
              + (f", {len(rec.failed)} failed" if rec.failed else ""))
    return path


def cmd_stop(args) -> int:
    config.ensure_dirs()
    session = read_session()
    if not session:
        print("No active session. Use `sectape export <session>` for an old one.")
        return 1

    _finish(session, quiet=False, fmt=args.format)
    live = [p for p in (session.get("panes") or {}).values()
            if pid_alive(p.get("pid", -1))]
    if live and not args.force:
        print(f"  {len(live)} pane(s) still recording, so the session stays open.")
        return 0
    if config.settings.current_session_file.exists():
        config.settings.current_session_file.unlink()
    return 0


# --------------------------------------------------------------------------
# reading back
# --------------------------------------------------------------------------

def _resolve(name: str) -> Path | None:
    if name:
        return resolve_session_dir(name)
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


def cmd_export(args) -> int:
    config.ensure_dirs()
    name = " ".join(args.session).strip() if args.session else ""
    session_dir = _resolve(name)
    if session_dir is None or not session_dir.exists():
        print(f"No recording found for {name or 'the active session'!r}.")
        return 1
    label = name or session_dir.name
    rec = _apply_filters(_load_recording(session_dir, label), args)
    dest = Path(args.output).expanduser() if args.output else None
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
    rec = _apply_filters(_load_recording(session_dir, name or session_dir.name), args)
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
    g, c, y, r, d, off = _colour(_use_colour())
    first = text.split("\n")[0]
    print(f"{g}noted{off} {first[:70]}{'…' if len(first) > 70 else ''}")
    return 0


def cmd_list(args) -> int:
    config.ensure_dirs()
    root = config.settings.sessions_dir
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    if args.json:
        print(json.dumps([{
            "session": d.name,
            "panes": len(list(d.glob("pane_*.raw"))),
            "commands": count_commands(d),
            "modified": d.stat().st_mtime,
        } for d in dirs], indent=2))
        return 0
    if not dirs:
        print("No recordings yet. Start one with: sectape rec")
        return 0
    print(f"{'Session':<36} {'Panes':>5} {'Cmds':>5}  Last activity")
    print("-" * 74)
    for d in dirs:
        count = count_commands(d)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime))
        print(f"{d.name[:36]:<36} {len(list(d.glob('pane_*.raw'))):>5} "
              f"{'?' if count is None else count:>5}  {when}")
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
    g, c, y, r, d, off = _colour(_use_colour())
    print(f"{c}sectape {__version__}{off}")
    print(f"  state    {config.settings.state_dir}")
    print(f"  output   {config.settings.output_dir}  ({config.settings.format})")
    print(f"  config   {config.settings.config_path or '(defaults)'}")
    if not session:
        print(f"  session  {y}none active{off}")
        return 0
    panes = {k: v for k, v in (session.get("panes") or {}).items()
             if pid_alive(v.get("pid", -1))}
    print(f"  session  {g}{session.get('label')}{off}")
    if session.get("started"):
        print(f"  running  {human_duration(time.time() - session['started'])}")
    session_dir = Path(session.get("dir"))
    count = count_commands(session_dir)
    print(f"  captured {'?' if count is None else count} commands, {len(panes)} live pane(s)")
    for pid_key, pane in sorted(panes.items()):
        log = Path(pane.get("log", ""))
        size = log.stat().st_size / 1024 if log.exists() else 0
        print(f"           #{pid_key}  pid {pane.get('pid')}  {size:.1f} KiB")
    return 0


def cmd_rm(args) -> int:
    config.ensure_dirs()
    name = " ".join(args.session).strip()
    session_dir = resolve_session_dir(name)
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
    g, c, y, r, d, off = _colour(_use_colour())
    ok = True

    def check(label, passed, detail="", warn_only=False):
        nonlocal ok
        mark = f"{g}✓{off}" if passed else (f"{y}!{off}" if warn_only else f"{r}✗{off}")
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))
        if not passed and not warn_only:
            ok = False

    print(f"{c}sectape {__version__} - doctor{off}\n")
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

    shell = os.environ.get("SHELL", "")
    check("shell supports integration",
          os.path.basename(shell) in ("zsh", "bash"), shell or "(SHELL unset)",
          warn_only=True)
    check("integration hooks render", "_sectape_preexec" in ZSH_HOOKS.format(version=__version__))
    check("output format valid", config.settings.format in WRITERS, config.settings.format)

    session = read_session()
    if session:
        stale = [k for k, v in (session.get("panes") or {}).items()
                 if not pid_alive(v.get("pid", -1))]
        check("no stale panes", not stale,
              f"{len(stale)} dead pane(s) will be pruned" if stale else "", warn_only=True)

    print()
    print(f"{g}All good.{off}" if ok else f"{y}Some checks failed (see above).{off}")
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
                      help="end even if panes are still recording")
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
        config.load(Path(args.config).expanduser() if args.config else None)
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
        return args.func(args) or 0
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130
