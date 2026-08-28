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
FORMATS = ("markdown", "json", "text")


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

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"


settings = Settings()


def _from_file(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    general = raw.get("general", {})
    output = raw.get("output", {})
    values: dict = {}

    if "state_dir" in general:
        values["state_dir"] = _expand(general["state_dir"])
    if "prompt" in general:
        values["prompt"] = str(general["prompt"])
    for key in ("redact", "shell_integration"):
        if key in general:
            values[key] = bool(general[key])

    if "dir" in output:
        values["output_dir"] = _expand(output["dir"])
    if "format" in output:
        values["format"] = str(output["format"])
    for key in ("max_output_lines", "max_output_chars"):
        if key in output:
            values[key] = int(output[key])
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
    return values


def config_path() -> Path:
    override_path = _env(CONFIG_ENV)
    return _expand(override_path) if override_path else DEFAULT_CONFIG_PATH


def validate(s: Settings) -> Settings:
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
    for directory in (settings.state_dir, settings.sessions_dir,
                      settings.backup_dir, settings.output_dir):
        directory.mkdir(parents=True, exist_ok=True)


TEMPLATE = '''\
# sectape configuration
# Anything here can also be set with an environment variable:
# SECTAPE_STATE_DIR, SECTAPE_OUTPUT_DIR, SECTAPE_FORMAT, SECTAPE_PROMPT,
# SECTAPE_REDACT, SECTAPE_SHELL_INTEGRATION.

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

[output]
dir = "~/sectape"
# markdown | json | text
format = "markdown"
max_output_lines = 300
max_output_chars = 20000
'''
