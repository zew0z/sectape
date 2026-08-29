"""Runtime settings.

Resolution order, lowest priority first:

1. built-in defaults
2. the config file (``~/.config/sectape/config.toml``, or ``$SECTAPE_CONFIG``)
3. environment variables (``SECTAPE_*``)
4. command-line flags

Modules read ``config.settings`` at call time rather than importing the values,
so :func:`load` and :func:`override` take effect everywhere.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

CONFIG_ENV = "SECTAPE_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sectape" / "config.toml"
FORMATS = ("markdown", "json", "text", "html")


class ConfigError(Exception):
    """Raised for an unreadable or invalid configuration."""


def _expand(value) -> Path:
    return Path(str(value)).expanduser()


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _flag(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    state_dir: Path = field(default_factory=lambda: Path.home() / ".sectape")
    output_dir: Path = field(default_factory=lambda: Path.home() / "sectape")
    format: str = "markdown"

    redact: bool = True
    redact_patterns: tuple[str, ...] = ()
    redact_replacement: str = "<REDACTED>"
    shell_integration: bool = True
    prompt: str = "$"                   # shown before each command in exports
    max_output_lines: int = 300
    max_output_chars: int = 20000

    config_path: Path | None = None

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def current_session_file(self) -> Path:
        return self.state_dir / "current.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / ".lock"


settings = Settings()


def _from_file(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    def wrong(key: str, want: str):
        raise ConfigError(f"{path}: {key} must be {want}")

    def table(name: str) -> dict:
        section = raw.get(name, {})
        if not isinstance(section, dict):
            wrong(f"[{name}]", "a section")
        return section

    general = table("general")
    output = table("output")
    redaction = table("redaction")
    values: dict = {}

    def text(section: dict, name: str, key: str):
        if not isinstance(section[name], str):
            wrong(key, "a string")
        return section[name]

    if "patterns" in redaction:
        patterns = redaction["patterns"]
        # A bare string here is iterable, so it used to be read as one regex
        # per character - every `o`, `p` and `s` in the export replaced with
        # <REDACTED>, silently.
        if not isinstance(patterns, list) or not all(
                isinstance(x, str) for x in patterns):
            wrong("redaction.patterns",
                  'a list of strings, e.g. patterns = ["CORP-[0-9]{6}"]')
        values["redact_patterns"] = tuple(patterns)
    if "replacement" in redaction:
        values["redact_replacement"] = text(redaction, "replacement",
                                            "redaction.replacement")

    if "state_dir" in general:
        values["state_dir"] = _expand(text(general, "state_dir",
                                           "general.state_dir"))
    if "prompt" in general:
        values["prompt"] = text(general, "prompt", "general.prompt")
    for key in ("redact", "shell_integration"):
        if key in general:
            if not isinstance(general[key], bool):
                wrong(f"general.{key}", "true or false")
            values[key] = general[key]

    if "dir" in output:
        values["output_dir"] = _expand(text(output, "dir", "output.dir"))
    if "format" in output:
        values["format"] = text(output, "format", "output.format")
    for key in ("max_output_lines", "max_output_chars"):
        if key in output:
            # bool is an int subclass, and `max_output_lines = true` is not a
            # limit. A non-number used to escape as a raw ValueError.
            if isinstance(output[key], bool) or not isinstance(output[key], int):
                wrong(f"output.{key}", "a whole number")
            values[key] = output[key]
    return values


def _from_env() -> dict:
    values: dict = {}
    if (v := _env("SECTAPE_STATE_DIR")):
        values["state_dir"] = _expand(v)
    if (v := _env("SECTAPE_OUTPUT_DIR")):
        values["output_dir"] = _expand(v)
    if (v := _env("SECTAPE_FORMAT")):
        values["format"] = v
    if (v := _env("SECTAPE_PROMPT")):
        values["prompt"] = v
    if (v := _env("SECTAPE_REDACT")) is not None:
        values["redact"] = _flag(v)
    if (v := _env("SECTAPE_SHELL_INTEGRATION")) is not None:
        values["shell_integration"] = _flag(v)
    if (v := _env("SECTAPE_REDACT_REPLACEMENT")):
        values["redact_replacement"] = v
    for name, key in (("SECTAPE_MAX_OUTPUT_LINES", "max_output_lines"),
                      ("SECTAPE_MAX_OUTPUT_CHARS", "max_output_chars")):
        if (v := _env(name)):
            try:
                values[key] = int(v)
            except ValueError:
                raise ConfigError(f"{name} must be a whole number, "
                                  f"not {v!r}") from None
    return values


# What each section of the config file may contain, for reporting typos.
KNOWN_KEYS = {
    "general": {"state_dir", "prompt", "redact", "shell_integration"},
    "output": {"dir", "format", "max_output_lines", "max_output_chars"},
    "redaction": {"patterns", "replacement"},
}


def unknown_keys(path: Path | None = None) -> list[str]:
    """Anything in the config file that sectape does not understand.

    A misspelled key is simply not read, so `fromat = "json"` quietly left the
    format at its default with nothing to show for it. `doctor` reports these.
    """
    chosen = Path(path) if path else config_path()
    try:
        with open(chosen, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found: list[str] = []
    for section, table in raw.items():
        if section not in KNOWN_KEYS:
            found.append(f"[{section}]")
            continue
        if isinstance(table, dict):
            found += [f"{section}.{key}" for key in table
                      if key not in KNOWN_KEYS[section]]
    return sorted(found)


def config_path() -> Path:
    override_path = _env(CONFIG_ENV)
    return _expand(override_path) if override_path else DEFAULT_CONFIG_PATH


def validate(s: Settings) -> Settings:
    import re
    for pattern in s.redact_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"bad redaction pattern {pattern!r}: {exc}") from None
    if s.format not in FORMATS:
        raise ConfigError(f"unknown output format {s.format!r}; "
                          f"choose one of {', '.join(FORMATS)}")
    if s.max_output_lines < 1 or s.max_output_chars < 1:
        raise ConfigError("output limits must be positive")
    return s


def load(path: Path | None = None, **overrides) -> Settings:
    """Resolve settings and install them as the process-wide configuration."""
    global settings
    chosen = Path(path) if path else config_path()
    values = _from_file(chosen)
    values.update(_from_env())
    values.update({k: v for k, v in overrides.items() if v is not None})
    values["config_path"] = chosen if chosen.exists() else None

    unknown = set(values) - set(Settings.__dataclass_fields__)
    if unknown:
        raise ConfigError("unknown settings: " + ", ".join(sorted(unknown)))
    settings = validate(Settings(**values))
    return settings


def override(**values) -> Settings:
    """Replace individual settings in place. Used by tests and CLI flags."""
    global settings
    settings = validate(replace(settings, **values))
    return settings


def ensure_dirs() -> None:
    # The state tree holds raw terminal logs, so keep it owner-only. The output
    # directory is for documents you will share, and is left to your umask.
    for directory in (settings.state_dir, settings.sessions_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    settings.output_dir.mkdir(parents=True, exist_ok=True)


TEMPLATE = '''\
# sectape configuration
# Anything here can also be set with an environment variable:
# SECTAPE_STATE_DIR, SECTAPE_OUTPUT_DIR, SECTAPE_FORMAT, SECTAPE_PROMPT,
# SECTAPE_REDACT, SECTAPE_SHELL_INTEGRATION, SECTAPE_REDACT_REPLACEMENT,
# SECTAPE_MAX_OUTPUT_LINES, SECTAPE_MAX_OUTPUT_CHARS. The redaction patterns
# are a list, so they live here only.

[general]
# Raw pane logs and session state.
state_dir = "~/.sectape"
# Prompt shown before each command in an export.
prompt = "$"
# Strip high-confidence secrets (private keys, bearer tokens, cloud keys) from
# exports. Passwords you type at a hidden prompt were never echoed anyway.
redact = true
# Inject preexec/precmd hooks so commands and exit codes are captured exactly.
# Turn off to fall back to reading commands off the screen.
shell_integration = true

[redaction]
# Extra regular expressions to scrub from exports, on top of the built-in
# private-key / bearer-token / cloud-credential patterns. Anything a pattern
# matches is replaced wholesale.
patterns = []
replacement = "<REDACTED>"

[output]
dir = "~/sectape"
# markdown | json | text | html
format = "markdown"
max_output_lines = 300
max_output_chars = 20000
'''
