"""Text analysis over captured terminal output.

Redaction, output cleanup, and working out which program a command line
actually ran.
"""
from __future__ import annotations

import os
import re

from . import config


# Full-screen programs redraw the whole terminal; their output is noise in a
# note. With shell integration we know the exact command, so we can replace it
# with a one-line summary instead of pasting a mangled screen.
FULLSCREEN_CMDS = {
    "nano", "pico", "vi", "vim", "nvim", "emacs", "joe", "micro", "ne",
    "less", "more", "most", "man", "top", "htop", "btop", "atop",
    "mc", "ranger", "vifm", "nmtui", "tmux", "screen", "gdb", "cmatrix",
}


CMD_PREFIXES = {"sudo", "doas", "command", "time", "nohup", "env", "stdbuf"}


# Short options of those prefixes that consume the next argument.
PREFIX_VALUE_FLAGS = {"-u", "-g", "-p", "-C", "-r", "-t", "-U", "-h", "-o"}


# Shell plumbing that says nothing about how a room was solved. `cat`, `ls`,
# `id` and friends are deliberately NOT here - they are real enumeration steps.
TRIVIAL_CMDS = {
    # shell plumbing
    "echo", "printf", "cd", "pwd", "export", "source", ".", "alias", "unalias",
    "clear", "exit", "true", "false", "history", "set", "unset", "type",
    # shell keywords, which are the first word of a compound command
    "for", "while", "until", "if", "case", "select", "function", "do", "then",
    "elif", "else", "fi", "done", "esac", "time", "{", "(",
}


def base_command(cmd: str) -> str:
    """The program actually being run, looking past sudo/env-style prefixes.

    `sudo -u root nano f` is `nano`, not `root` - short options that take a
    value have to be stepped over along with the value itself.
    """
    skip_value = False
    for token in str(cmd or "").split():
        if skip_value:
            skip_value = False
            continue
        if token.startswith("-"):
            if token in PREFIX_VALUE_FLAGS:
                skip_value = True
            continue
        if "://" in token or token.lower().startswith(("http:", "https:", "ftp:")):
            return ""                             # a pasted URL is not a program
        if "=" in token:
            continue                              # FOO=bar prefix
        if token in CMD_PREFIXES:
            continue
        return os.path.basename(token.rstrip("?&;"))
    return ""


# Only high-confidence secrets. Passwords for a lab box and CTF flags are
# often the point of the recording, so they are deliberately left alone.
REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<REDACTED: private key>"),
    (re.compile(r"(?i)\b(authorization\s*:\s*)(bearer|basic)\s+[^\s'\"]+"), r"\1\2 <REDACTED>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED: aws key id>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<REDACTED: github token>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "<REDACTED: slack token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "<REDACTED: api key>"),
    (re.compile(r"(?i)\b(aws_secret_access_key|api[_-]?key|secret[_-]?key|access[_-]?token|"
                r"refresh[_-]?token|client[_-]?secret)\b(\s*[:=]\s*)['\"]?[A-Za-z0-9/+_.\-]{16,}"),
     r"\1\2<REDACTED>"),
]


_CUSTOM_CACHE: dict[tuple[str, ...], list] = {}


def custom_redactions() -> list:
    """Compile (and cache) the user's own redaction patterns."""
    key = tuple(config.settings.redact_patterns)
    compiled = _CUSTOM_CACHE.get(key)
    if compiled is None:
        compiled = []
        for pattern in key:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                continue          # validate() already reported it at load time
        _CUSTOM_CACHE[key] = compiled
    return compiled


def redact(text: str, enabled: bool = True) -> str:
    if not enabled or not text:
        return text
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    replacement = config.settings.redact_replacement
    for pattern in custom_redactions():
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------
# Output cleaning
# --------------------------------------------------------------------------


ART_CHARS = set(r"/\#_|-~+=*`^<>()[]{}.:'\"")


NOISE_LINES = {"%", "∙", "^D", ""}

# How many art-heavy lines in a row make a banner. One on its own is a table
# border or a horizontal rule and belongs in the output; replacing those
# individually gutted every `mysql`, `psql` and `column -t` table into rows of
# <SNIP> markers.
BANNER_MIN_LINES = 2


def trim_for_export(text: str, max_lines: int | None = None,
                    max_chars: int | None = None) -> str:
    """Bound a block of prose to the configured output limits.

    Command output has always been trimmed for readability; notes were not, so
    `cat big.log | sectape note` put the whole file into every export. The
    note itself is kept in full in notes.jsonl - this only bounds the
    document, and says how much it left out.
    """
    if not text:
        return ""
    max_lines = config.settings.max_output_lines if max_lines is None else max_lines
    max_chars = config.settings.max_output_chars if max_chars is None else max_chars
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        tail_n = max(1, min(20, max_lines // 3))
        head_n = max(1, max_lines - tail_n)
        lines = (lines[:head_n]
                 + [f"<SNIP: {dropped} more lines>"]
                 + lines[-tail_n:])
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n<SNIP: truncated at {max_chars} chars>"
    return out


def clean_terminal_output(text: str, max_lines: int | None = None,
                          max_chars: int | None = None) -> str:
    """Trim a rendered output block down to something readable in an export."""
    if not text:
        return ""
    max_lines = config.settings.max_output_lines if max_lines is None else max_lines
    max_chars = config.settings.max_output_chars if max_chars is None else max_chars
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    cleaned: list[str] = []
    banner: list[str] = []
    blanks = 0

    def flush_banner() -> None:
        if len(banner) >= BANNER_MIN_LINES:
            cleaned.append("<SNIP: ASCII-art banner>")
        else:
            cleaned.extend(banner)
        banner.clear()

    for line in lines:
        line = line.rstrip()
        stripped = line.strip()

        if stripped in NOISE_LINES:
            flush_banner()
            if stripped == "":
                blanks += 1
                if blanks <= 1 and cleaned:
                    cleaned.append("")
            continue
        blanks = 0

        if len(line) > 300:
            flush_banner()
            cleaned.append(line[:250] + " ... <SNIP: line over 300 chars>")
            continue

        art = sum(1 for c in stripped if c in ART_CHARS)
        if len(stripped) > 15 and art / len(stripped) > 0.6:
            banner.append(line)
            continue
        flush_banner()
        cleaned.append(line)

    flush_banner()

    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    while cleaned and cleaned[0] == "":
        cleaned.pop(0)

    if len(cleaned) > max_lines:
        # Keep a head and a tail. The split has to stay positive: a small
        # max_lines used to produce a negative head slice, which quietly kept
        # everything instead of truncating.
        # The result is never shorter than three lines: one of head, the snip
        # marker, one of tail.
        dropped = len(cleaned) - max_lines
        tail_n = max(1, min(20, max_lines // 3))
        head_n = max(1, max_lines - tail_n)
        cleaned = (cleaned[:head_n]
                   + [f"<SNIP: {dropped} more lines>"]
                   + cleaned[-tail_n:])

    out = "\n".join(cleaned)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n<SNIP: output truncated at {max_chars} chars>"
    return out.strip()
