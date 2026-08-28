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
from pathlib import Path

from . import __version__ as VERSION
from .terminal import VTScreen


# The wire format, as the shell hooks emit it. `SECTAPE` is the current
# payload tag; `THM` is the tool's previous name and is still read.
#   begin: ESC ] 7337 ; SECTAPE ; b | <b64 command> | <epoch>            BEL
#   end:   ESC ] 7337 ; SECTAPE ; e | <exit code> | <epoch> | <b64 cwd>  BEL
#   size:  ESC ] 7337 ; SECTAPE ; w | <cols> | <rows>                    BEL
# The size marker is written straight into the log file, never to the tty.
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
  # EPOCHREALTIME is printed with the locale's decimal point, so a European
  # locale would otherwise emit `1700000000,5` and lose the recording's timings.
  _sectape_now() {{ local t=${{EPOCHREALTIME:-$(date +%s)}}; print -rn -- "${{t//,/.}}" }}
  _sectape_preexec() {{
    typeset -g _SECTAPE_RUNNING=1
    printf '\033]7337;SECTAPE;b|%s|%s\007' "$(_sectape_b64 "$1")" "$(_sectape_now)"
  }}
  _sectape_precmd() {{
    local ec=$?
    [[ -z $_SECTAPE_RUNNING ]] && return
    unset _SECTAPE_RUNNING
    printf '\033]7337;SECTAPE;e|%s|%s|%s\007' "$ec" "$(_sectape_now)" "$(_sectape_b64 "$PWD")"
  }}
  if (( $+functions[add-zsh-hook] )); then
    add-zsh-hook preexec _sectape_preexec
    add-zsh-hook precmd  _sectape_precmd
  else
    preexec_functions+=(_sectape_preexec)
    precmd_functions+=(_sectape_precmd)
  fi
  # Convenience: `note "..."` inside a recording, without the sectape prefix.
  # `command -v` sees a function or an alias as well; $+commands sees only
  # external commands, so this used to replace a `note` of the user's own.
  if [[ -n $SECTAPE_BIN ]] && ! command -v note >/dev/null 2>&1; then
    note() {{ "$SECTAPE_BIN" ${{=SECTAPE_BIN_ARGS}} note "$@" }}
  fi
fi
# --- end sectape ------------------------------------------------------
"""


BASH_HOOKS = r"""
# --- sectape shell integration (v{version}) ---------------------------
if [ -n "$SECTAPE_ACTIVE" ] && [ -z "$SECTAPE_INTEGRATION_LOADED" ]; then
  SECTAPE_INTEGRATION_LOADED=1
  _sectape_b64() {{ printf '%s' "$1" | base64 | tr -d '\n'; }}
  # %N is a GNU date extension: BSD date prints a literal "N", which is not a
  # timestamp at all. EPOCHREALTIME (bash 5) is exact but locale-formatted.
  _sectape_now() {{
    local t
    if [ -n "${{EPOCHREALTIME:-}}" ]; then t=$EPOCHREALTIME; else t=$(date +%s.%N); fi
    case "$t" in *N*) t=$(date +%s) ;; esac
    printf '%s' "${{t//,/.}}"
  }}
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
    printf '\033]7337;SECTAPE;b|%s|%s\007' "$(_sectape_b64 "$(_sectape_line)")" "$(_sectape_now)"
  }}
  _sectape_precmd() {{
    local ec=$?
    [ -z "$_SECTAPE_RUNNING" ] && return
    unset _SECTAPE_RUNNING
    printf '\033]7337;SECTAPE;e|%s|%s|%s\007' "$ec" "$(_sectape_now)" "$(_sectape_b64 "$PWD")"
  }}
  # Convenience: `note "..."` inside a recording, without the sectape prefix.
  if [ -n "$SECTAPE_BIN" ] && ! command -v note >/dev/null 2>&1; then
    note() {{ "$SECTAPE_BIN" ${{SECTAPE_BIN_ARGS}} note "$@"; }}
  fi
  PROMPT_COMMAND="_sectape_precmd${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}"
  # The DEBUG trap goes last, on purpose. Anything set up after it is itself
  # seen by the trap and recorded as the session's first command.
  #
  # A trap of your own is kept and run first. bash-preexec, Atuin and several
  # shell integrations ride this trap, and simply taking it broke them for the
  # length of the recording - while PROMPT_COMMAND, right above, was being
  # carefully preserved.
  _sectape_prior_debug_trap=$(trap -p DEBUG)
  if [ -n "$_sectape_prior_debug_trap" ]; then
    _sectape_prior_debug_trap=${{_sectape_prior_debug_trap#trap -- }}
    _sectape_prior_debug_trap=${{_sectape_prior_debug_trap% DEBUG}}
    # `trap -p` prints the handler already quoted for re-use, so eval is how
    # it is meant to be replayed.
    eval "_sectape_prior_debug() {{ eval ${{_sectape_prior_debug_trap}}; }}"
    unset _sectape_prior_debug_trap
    trap '_sectape_prior_debug; _sectape_preexec' DEBUG
  else
    unset _sectape_prior_debug_trap
    trap '_sectape_preexec' DEBUG
  fi
fi
# --- end sectape ------------------------------------------------------
"""


def sh_quote(text) -> str:
    """A path as a shell single-quoted literal.

    Interpolating it bare meant a `$` in the path expanded: the wrapper then
    pointed at a directory that did not exist, and the recorded shell silently
    started with none of the user's own configuration.
    """
    return "'" + str(text).replace("'", "'\\''") + "'"


def build_zsh_wrapper(dirpath: Path) -> None:
    """A throwaway ZDOTDIR that sources the real rc files, then adds hooks."""
    dirpath.mkdir(parents=True, exist_ok=True)
    real = os.environ.get("ZDOTDIR") or str(Path.home())
    (dirpath / ".zshenv").write_text(
        "SECTAPE_REAL_ZDOTDIR=${SECTAPE_REAL_ZDOTDIR:-" + sh_quote(real) + "}\n"
        "SECTAPE_WRAP_ZDOTDIR=" + sh_quote(dirpath) + "\n"
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
