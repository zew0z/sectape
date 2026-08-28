"""Test package.

Every ``SECTAPE_*`` variable is an input to the configuration, so a developer
who exports ``SECTAPE_OUTPUT_DIR`` in their own shell - which is exactly what
a user of this tool does - used to get spurious failures from the tests that
assert on resolved settings. Clearing them here covers both runners: pytest
and ``python -m unittest discover``, which never loads a conftest. Tests that
want a variable set it themselves.
"""
from __future__ import annotations

import os


def _clear_ambient_settings() -> None:
    for name in [key for key in os.environ if key.startswith("SECTAPE_")]:
        del os.environ[name]


_clear_ambient_settings()
