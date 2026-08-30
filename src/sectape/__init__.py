"""sectape - record a terminal session as commands, not bytes.

`script(1)` gives you a stream of escape codes. sectape runs your shell under
a correctly-sized pseudo-terminal, injects shell-integration markers, and
replays the capture through a VT emulator, so what you get back is a list of
the commands that ran, what each one printed, what it exited with and how long
it took.
"""

__version__ = "5.0.0"
__all__ = ["__version__"]
