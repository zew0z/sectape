"""Shell-integration markers.

A private OSC 7337 sequence carries the exact command line, its exit code,
working directory and timestamps out of the recorded shell. Terminals ignore
OSC codes they do not recognise, so the markers are invisible while you work
and are stripped by the VT replayer afterwards.

Logs written by the tool's earlier name still use the ``THM`` payload tag and
are read unchanged.
"""
from __future__ import annotations

import base64
import os
import re
import tempfile
from pathlib import Path

from . import __version__ as VERSION
from .terminal import VTScreen


MARKER_RE = re.compile(r"\x1b\]7337;(?:THM|SECTAPE);([^\x07\x1b]*)(?:\x07|\x1b\\)")


def capture_width(raw: str, default: int = VTScreen.DEFAULT_WIDTH) -> int:
    """Terminal width the capture was recorded at, from its size marker.

    Pre-3.0 logs carry no marker; those were always recorded at 80 columns
    because the old recorder never sized its pty.
    """
    for m in MARKER_RE.finditer(raw):
        parts = m.group(1).split("|")
        if parts and parts[0] == "w" and len(parts) > 1:
            try:
                return int(parts[1])
            except ValueError:
                continue
    return default


def _b64d(s: str) -> str:
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


ZSH_HOOKS = r"""
# --- sectape shell integration (v{version}) ---------------------------
if [[ -n $SECTAPE_ACTIVE && -z $SECTAPE_INTEGRATION_LOADED ]]; then
  SECTAPE_INTEGRATION_LOADED=1
  zmodload zsh/datetime 2>/dev/null
  autoload -Uz add-zsh-hook 2>/dev/null
  _sectape_b64() {{ print -rn -- "$1" | base64 | tr -d '\n' }}
  _sectape_preexec() {{
    typeset -g _SECTAPE_RUNNING=1
    printf '\033]7337;SECTAPE;b|%s|%s\007' "$(_sectape_b64 "$1")" "${{EPOCHREALTIME:-$(date +%s)}}"
  }}
  _sectape_precmd() {{
    local ec=$?
    [[ -z $_SECTAPE_RUNNING ]] && return
    unset _SECTAPE_RUNNING
    printf '\033]7337;SECTAPE;e|%s|%s|%s\007' "$ec" "${{EPOCHREALTIME:-$(date +%s)}}" "$(_sectape_b64 "$PWD")"
  }}
  if (( $+functions[add-zsh-hook] )); then
    add-zsh-hook preexec _sectape_preexec
    add-zsh-hook precmd  _sectape_precmd
  else
    preexec_functions+=(_sectape_preexec)
    precmd_functions+=(_sectape_precmd)
  fi
fi
# --- end sectape ------------------------------------------------------
"""


BASH_HOOKS = r"""
# --- sectape shell integration (v{version}) ---------------------------
if [ -n "$SECTAPE_ACTIVE" ] && [ -z "$SECTAPE_INTEGRATION_LOADED" ]; then
  SECTAPE_INTEGRATION_LOADED=1
  _sectape_b64() {{ printf '%s' "$1" | base64 | tr -d '\n'; }}
  # bash has no preexec hook, so this rides the DEBUG trap. BASH_COMMAND holds
  # only the current simple command, so `a; b` and loops would be recorded as
  # their first clause; the history entry has the line as it was typed.
  _sectape_line() {{
    local entry
    entry=$(HISTTIMEFORMAT= history 1 2>/dev/null)
    if [[ $entry =~ ^[[:space:]]*[0-9]+[[:space:]]+(.*)$ ]]; then
      printf '%s' "${{BASH_REMATCH[1]}}"
    else
      printf '%s' "$BASH_COMMAND"
    fi
  }}
  _sectape_preexec() {{
    [ -n "$_SECTAPE_RUNNING" ] && return
    # The trap also fires for the prompt hook itself; that is not a command
    # the user ran.
    case "$BASH_COMMAND" in
      _sectape_precmd*|_sectape_preexec*|"$PROMPT_COMMAND") return ;;
    esac
    _SECTAPE_RUNNING=1
    printf '\033]7337;SECTAPE;b|%s|%s\007' "$(_sectape_b64 "$(_sectape_line)")" "$(date +%s.%N)"
  }}
  _sectape_precmd() {{
    local ec=$?
    [ -z "$_SECTAPE_RUNNING" ] && return
    unset _SECTAPE_RUNNING
    printf '\033]7337;SECTAPE;e|%s|%s|%s\007' "$ec" "$(date +%s.%N)" "$(_sectape_b64 "$PWD")"
  }}
  # PROMPT_COMMAND first: installing the trap before this line made the trap
  # capture the assignment itself as the session's first command.
  PROMPT_COMMAND="_sectape_precmd${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}"
  trap '_sectape_preexec' DEBUG
fi
# --- end sectape ------------------------------------------------------
"""


def build_zsh_wrapper(dirpath: Path) -> None:
    """A throwaway ZDOTDIR that sources the real rc files, then adds hooks."""
    dirpath.mkdir(parents=True, exist_ok=True)
    real = os.environ.get("ZDOTDIR") or str(Path.home())
    (dirpath / ".zshenv").write_text(
        "SECTAPE_REAL_ZDOTDIR=${SECTAPE_REAL_ZDOTDIR:-" + f"{real}" + "}\n"
        "SECTAPE_WRAP_ZDOTDIR=" + str(dirpath) + "\n"
        "() {\n"
        "  local ZDOTDIR=$SECTAPE_REAL_ZDOTDIR\n"
        "  [[ -f $ZDOTDIR/.zshenv ]] && source $ZDOTDIR/.zshenv\n"
        "  SECTAPE_REAL_ZDOTDIR=$ZDOTDIR\n"
        "}\n"
        "ZDOTDIR=$SECTAPE_WRAP_ZDOTDIR\n",
        encoding="utf-8",
    )
    for name in (".zprofile", ".zlogin"):
        (dirpath / name).write_text(
            "[[ -f $SECTAPE_REAL_ZDOTDIR/" + name + " ]] && source $SECTAPE_REAL_ZDOTDIR/" + name + "\n",
            encoding="utf-8",
        )
    (dirpath / ".zshrc").write_text(
        "ZDOTDIR=$SECTAPE_REAL_ZDOTDIR\n"
        "[[ -f $ZDOTDIR/.zshrc ]] && source $ZDOTDIR/.zshrc\n"
        + ZSH_HOOKS.format(version=VERSION),
        encoding="utf-8",
    )


def build_bash_wrapper(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"\n'
        + BASH_HOOKS.format(version=VERSION),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Redaction - only high-confidence secrets. Lab passwords and CTF flags are
# the point of these notes, so they are deliberately left alone.
# --------------------------------------------------------------------------
