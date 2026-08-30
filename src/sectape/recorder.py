"""The PTY recorder.

Runs an interactive shell under a pseudo-terminal, mirrors it to the real
terminal, and appends everything to a pane log. The terminal is restored on
every exit path, including signals.
"""
from __future__ import annotations

import errno
import os
import select
import shutil
import signal
import sys
import tempfile
import termios
import tty
from pathlib import Path

from .markers import build_bash_wrapper, build_zsh_wrapper
from .terminal import TERMINAL_RESET, get_winsize, set_winsize


def write_all(fd: int, data: bytes) -> None:
    """Write every byte.

    os.write on a tty or pipe may accept fewer bytes than it was given - a
    large paste, or output arriving faster than the terminal drains it. A bare
    os.write silently truncates in exactly those cases.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 0.1)
            continue
        if written <= 0:
            break
        view = view[written:]


# The only shells whose hooks sectape knows how to write.
SUPPORTED_SHELLS = ("zsh", "bash")


def chosen_shell() -> tuple[str, str]:
    """The shell a recording will run, and its base name."""
    shell = os.environ.get("SECTAPE_SHELL") or os.environ.get("SHELL") or "/bin/zsh"
    return shell, os.path.basename(shell)


def integration_available(no_integration: bool = False) -> bool:
    """Whether the shell-integration hooks will really be installed.

    The banner used to answer this from the command-line flag alone, so a fish
    or sh user - and anyone with `shell_integration = false` in their config -
    was told "integration on" and then handed a transcript read back off the
    screen.
    """
    if no_integration:
        return False
    shell, name = chosen_shell()
    if name not in SUPPORTED_SHELLS:
        return False
    # A `SHELL` naming a binary that is not there still looks like zsh. The
    # recording falls back to /bin/sh, which has no hooks, so promising
    # integration on the strength of the name alone was wrong twice over.
    return shutil.which(shell) is not None


def prepare_shell(no_integration: bool) -> tuple[str, str, Path | None]:
    """Pick the shell and build its integration wrapper.

    Called before the fork so the parent owns the temporary directory and can
    remove it when the session ends; building it in the child leaked one
    directory per recording.
    """
    shell, name = chosen_shell()
    if not integration_available(no_integration):
        return shell, name, None

    wrapper = Path(tempfile.mkdtemp(prefix="sectape-shell-"))
    os.chmod(wrapper, 0o700)
    if name == "zsh":
        build_zsh_wrapper(wrapper)
    else:
        build_bash_wrapper(wrapper / "bashrc")
    return shell, name, wrapper


def self_invocation() -> tuple[str, str]:
    """How to re-run this sectape: (executable, extra argument words).

    Kept as two pieces so the injected shell function can call it without an
    eval, which would mangle arguments containing spaces or quotes.
    """
    script = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if script and script.is_file() and script.name.startswith("sectape"):
        return str(script), ""
    return sys.executable, "-m sectape"


def _spawn_shell(shell: str, name: str, wrapper: Path | None) -> None:
    """Runs in the forked child. Never returns."""
    os.environ["SECTAPE_ACTIVE"] = "1"
    binary, extra = self_invocation()
    os.environ["SECTAPE_BIN"] = binary
    os.environ["SECTAPE_BIN_ARGS"] = extra
    os.environ.pop("SECTAPE_INTEGRATION_LOADED", None)

    try:
        if wrapper is not None and name == "zsh":
            os.environ["SECTAPE_REAL_ZDOTDIR"] = os.environ.get("ZDOTDIR") or str(Path.home())
            os.environ["ZDOTDIR"] = str(wrapper)
            os.execvp(shell, [shell, "-i"])
        elif wrapper is not None and name == "bash":
            os.execvp(shell, [shell, "--rcfile", str(wrapper / "bashrc"), "-i"])
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
    # 0o600: the log holds everything the terminal displayed.
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    shell, shell_name, wrapper = prepare_shell(no_integration)

    pid, master_fd = pty.fork()
    if pid == 0:
        _spawn_shell(shell, shell_name, wrapper)
        os._exit(127)                                   # unreachable

    rows, cols, _, _ = get_winsize(stdin_fd)
    set_winsize(master_fd, rows, cols)

    def note_size(rows: int, cols: int) -> None:
        # Written straight to the log so the replay knows the wrap column;
        # it never reaches the user's terminal.
        try:
            write_all(log_fd, f"\x1b]7337;SECTAPE;w|{cols}|{rows}\x07".encode())
        except OSError:
            pass

    note_size(rows, cols)

    # The size marker is written by the loop, not by the handler. A signal can
    # arrive in the middle of a partial write to the log, and a marker spliced
    # into the middle of an escape sequence corrupts the replay. Resizing the
    # pty and waking the child stay here, because those have to be prompt; the
    # marker only has to land before the redraw it describes, which it does.
    pending_size: list[tuple[int, int]] = []

    def on_winch(signum, frame):
        size = get_winsize(stdin_fd)
        set_winsize(master_fd, *size)
        pending_size.append((size[0], size[1]))
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
    # Mirroring stops if the terminal goes away; logging carries on. On Linux
    # a write to a pty whose other end has closed raises EIO, which used to
    # abandon the rest of the shell's output rather than log it.
    mirroring = True
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

            while pending_size:                 # before anything this wake logs
                note_size(*pending_size.pop(0))

            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if data:
                    try:
                        write_all(master_fd, data)
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
                    if mirroring:
                        try:
                            write_all(sys.stdout.fileno(), data)
                        except OSError:
                            mirroring = False
                    try:
                        write_all(log_fd, data)
                    except OSError:
                        pass
    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGHUP, prev_hup)
        while pending_size:
            note_size(*pending_size.pop(0))
        # Drain whatever the shell printed on its way out.
        try:
            os.set_blocking(master_fd, False)
            while True:
                chunk = os.read(master_fd, 65536)
                if not chunk:
                    break
                if mirroring:
                    try:
                        write_all(sys.stdout.fileno(), chunk)
                    except OSError:
                        mirroring = False
                write_all(log_fd, chunk)
        except Exception:
            pass
        try:
            os.close(log_fd)
        except OSError:
            pass
        if wrapper is not None:
            shutil.rmtree(wrapper, ignore_errors=True)
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
        try:
            # TCSADRAIN waits for output to drain, which a terminal that has
            # gone away will never do. Failing to restore one that no longer
            # exists is not worth an exception.
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        except OSError:
            pass
        try:
            sys.stdout.write(TERMINAL_RESET)
            sys.stdout.flush()
        except Exception:
            pass

    return os.waitstatus_to_exitcode(status) if status else 0
