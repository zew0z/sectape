"""The PTY recorder.

Runs an interactive shell under a pseudo-terminal, mirrors it to the real
terminal, and appends everything to a pane log. The terminal is restored on
every exit path, including signals.
"""
from __future__ import annotations

import errno
import os
import select
import signal
import sys
import tempfile
import termios
import tty
from pathlib import Path

from .markers import build_bash_wrapper, build_zsh_wrapper
from .terminal import TERMINAL_RESET, get_winsize, set_winsize


def _spawn_shell(no_integration: bool) -> None:
    """Runs in the forked child. Never returns."""
    shell = os.environ.get("SECTAPE_SHELL") or os.environ.get("SHELL") or "/bin/zsh"
    name = os.path.basename(shell)
    os.environ["SECTAPE_ACTIVE"] = "1"
    os.environ.pop("SECTAPE_INTEGRATION_LOADED", None)

    try:
        if not no_integration and name == "zsh":
            wrap = Path(tempfile.mkdtemp(prefix="sectape-zdotdir-"))
            build_zsh_wrapper(wrap)
            os.environ["SECTAPE_REAL_ZDOTDIR"] = os.environ.get("ZDOTDIR") or str(Path.home())
            os.environ["ZDOTDIR"] = str(wrap)
            os.execvp(shell, [shell, "-i"])
        elif not no_integration and name == "bash":
            rc = Path(tempfile.mkdtemp(prefix="sectape-bashrc-")) / "bashrc"
            build_bash_wrapper(rc)
            os.execvp(shell, [shell, "--rcfile", str(rc), "-i"])
        else:
            os.execvp(shell, [shell, "-i"])
    except Exception:
        try:
            os.execvp("/bin/sh", ["/bin/sh", "-i"])
        except Exception:
            os._exit(127)


def record_pty(log_path: Path, banner: str, no_integration: bool = False) -> int:
    """Run an interactive shell under a PTY, mirroring and logging its output.

    Returns the child's exit status. The caller's terminal is restored even if
    the child dies badly or we are signalled.
    """
    import pty  # imported late: only needed when actually recording

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("[-] sectape needs an interactive terminal (stdin/stdout must be a tty).",
              file=sys.stderr)
        return 1

    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)
    print(banner, flush=True)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab", buffering=0)

    pid, master_fd = pty.fork()
    if pid == 0:
        _spawn_shell(no_integration)
        os._exit(127)                                   # unreachable

    rows, cols, _, _ = get_winsize(stdin_fd)
    set_winsize(master_fd, rows, cols)

    log_fd = log_file.fileno()

    def note_size(rows: int, cols: int) -> None:
        # Written straight to the log so the replay knows the wrap column;
        # it never reaches the user's terminal.
        try:
            os.write(log_fd, f"\x1b]7337;SECTAPE;w|{cols}|{rows}\x07".encode())
        except OSError:
            pass

    note_size(rows, cols)

    def on_winch(signum, frame):
        size = get_winsize(stdin_fd)
        set_winsize(master_fd, *size)
        note_size(size[0], size[1])
        try:
            os.kill(pid, signal.SIGWINCH)
        except OSError:
            pass

    stopping = {"now": False}

    def on_terminate(signum, frame):
        # Being killed must still restore the terminal, so unwind the loop
        # instead of dying inside it.
        stopping["now"] = True
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass

    prev_winch = signal.signal(signal.SIGWINCH, on_winch)
    prev_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_IGN)        # Ctrl-C belongs to the child
    prev_term = signal.signal(signal.SIGTERM, on_terminate)
    prev_hup = signal.signal(signal.SIGHUP, on_terminate)

    status = 0
    try:
        tty.setraw(stdin_fd)
        running = True
        while running and not stopping["now"]:
            try:
                readable, _, _ = select.select([stdin_fd, master_fd], [], [])
            except (InterruptedError, OSError) as exc:
                if getattr(exc, "errno", None) == errno.EINTR:
                    continue
                break

            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if data:
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        running = False
                else:
                    running = False

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                except OSError as exc:
                    # macOS returns b'', Linux raises EIO, when the child exits.
                    if getattr(exc, "errno", None) not in (errno.EIO, errno.EBADF):
                        raise
                    data = b""
                if not data:
                    running = False
                else:
                    os.write(sys.stdout.fileno(), data)
                    try:
                        log_file.write(data)
                    except Exception:
                        pass
    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGHUP, prev_hup)
        # Drain whatever the shell printed on its way out.
        try:
            os.set_blocking(master_fd, False)
            while True:
                chunk = os.read(master_fd, 65536)
                if not chunk:
                    break
                os.write(sys.stdout.fileno(), chunk)
                log_file.write(chunk)
        except Exception:
            pass
        try:
            log_file.close()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
            except ChildProcessError:
                status = 0
                break
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        try:
            sys.stdout.write(TERMINAL_RESET)
            sys.stdout.flush()
        except Exception:
            pass

    return os.waitstatus_to_exitcode(status) if status else 0


# --------------------------------------------------------------------------
# Webhook receiver (the browser userscript posts room data here)
# --------------------------------------------------------------------------
