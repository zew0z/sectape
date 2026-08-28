import os
import shutil
import stat
import threading
import unittest
from pathlib import Path

from sectape.recorder import prepare_shell, self_invocation, write_all


class TestWriteAll(unittest.TestCase):
    """os.write may accept fewer bytes than it is given; a bare call truncates."""

    def test_writes_more_than_a_pipe_buffer(self):
        read_fd, write_fd = os.pipe()
        payload = bytes(range(256)) * 8192          # 2 MiB, far over the buffer
        received = bytearray()

        def drain():
            while len(received) < len(payload):
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                received.extend(chunk)

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            write_all(write_fd, payload)
        finally:
            os.close(write_fd)
            reader.join(timeout=30)
            os.close(read_fd)
        self.assertEqual(bytes(received), payload)

    def test_short_write_is_retried(self):
        """A fake fd that accepts one byte at a time must still get everything."""
        accepted = bytearray()
        real_write = os.write

        def stingy(fd, data):
            if fd == -424242:
                accepted.extend(bytes(data)[:1])
                return 1
            return real_write(fd, data)

        os.write = stingy
        try:
            write_all(-424242, b"abcdefgh")
        finally:
            os.write = real_write
        self.assertEqual(bytes(accepted), b"abcdefgh")

    def test_empty_payload(self):
        write_all(-1, b"")          # must not raise or spin


class TestPrepareShell(unittest.TestCase):
    def tearDown(self):
        for path in getattr(self, "_made", []):
            shutil.rmtree(path, ignore_errors=True)

    def prepare(self, no_integration=False, **env):
        saved = {k: os.environ.get(k) for k in ("SECTAPE_SHELL", "SHELL")}
        os.environ.update(env)
        try:
            shell, name, wrapper = prepare_shell(no_integration)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self._made = [wrapper] if wrapper else []
        return shell, name, wrapper

    def test_zsh_wrapper_sources_real_rc_then_adds_hooks(self):
        shell, name, wrapper = self.prepare(SECTAPE_SHELL="/bin/zsh")
        self.assertEqual(name, "zsh")
        self.assertIsNotNone(wrapper)
        zshrc = (wrapper / ".zshrc").read_text()
        self.assertIn("SECTAPE_REAL_ZDOTDIR", zshrc)
        self.assertIn("_sectape_preexec", zshrc)
        self.assertIn("source $ZDOTDIR/.zshrc", zshrc)
        for name_ in (".zshenv", ".zprofile", ".zlogin"):
            self.assertTrue((wrapper / name_).exists(), name_)

    def test_bash_wrapper(self):
        shell, name, wrapper = self.prepare(SECTAPE_SHELL="/bin/bash")
        self.assertEqual(name, "bash")
        rc = (wrapper / "bashrc").read_text()
        self.assertIn("_sectape_precmd", rc)
        self.assertIn(".bashrc", rc)

    def test_wrapper_is_owner_only(self):
        _, _, wrapper = self.prepare(SECTAPE_SHELL="/bin/zsh")
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)

    def test_no_integration_builds_nothing(self):
        _, _, wrapper = self.prepare(no_integration=True, SECTAPE_SHELL="/bin/zsh")
        self.assertIsNone(wrapper)

    def test_unsupported_shell_builds_nothing(self):
        shell, name, wrapper = self.prepare(SECTAPE_SHELL="/bin/ksh")
        self.assertEqual(name, "ksh")
        self.assertIsNone(wrapper)


class TestSelfInvocation(unittest.TestCase):
    """How the injected `note` helper calls sectape back.

    Kept as (executable, extra words) rather than one string so the shell
    function needs no eval, which would mangle arguments containing spaces.
    """

    def setUp(self):
        import sys
        import tempfile
        self.saved = list(sys.argv)
        self.addCleanup(lambda: sys.argv.__setitem__(slice(None), self.saved))
        self.dir = Path(tempfile.mkdtemp(prefix="sectape-argv-"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def invoke(self, argv0):
        import sys
        sys.argv[:] = [argv0]
        return self_invocation()

    def test_an_installed_console_script_is_called_directly(self):
        script = self.dir / "sectape"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
        binary, extra = self.invoke(str(script))
        self.assertEqual(Path(binary), script.resolve())
        self.assertEqual(extra, "")

    def test_a_versioned_script_name_still_counts(self):
        script = self.dir / "sectape3"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        binary, extra = self.invoke(str(script))
        self.assertEqual(Path(binary), script.resolve())
        self.assertEqual(extra, "")

    def test_module_execution_falls_back_to_the_interpreter(self):
        import sys
        binary, extra = self.invoke(str(self.dir / "__main__.py"))
        self.assertEqual(binary, sys.executable)
        self.assertEqual(extra, "-m sectape")

    def test_a_name_that_is_not_sectape_falls_back(self):
        import sys
        other = self.dir / "something-else"
        other.write_text("#!/bin/sh\n", encoding="utf-8")
        binary, extra = self.invoke(str(other))
        self.assertEqual(binary, sys.executable)
        self.assertEqual(extra, "-m sectape")

    def test_an_empty_argv_falls_back(self):
        import sys
        binary, extra = self.invoke("")
        self.assertEqual(binary, sys.executable)
        self.assertEqual(extra, "-m sectape")


if __name__ == "__main__":
    unittest.main()
