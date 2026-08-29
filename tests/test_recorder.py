import os
import shutil
import stat
import threading
import unittest
from pathlib import Path

from sectape.recorder import (chosen_shell, integration_available,
                              prepare_shell, self_invocation, write_all)


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


ZSH = shutil.which("zsh")
BASH = shutil.which("bash")


class TestPrepareShell(unittest.TestCase):
    """Hooks are only built for a shell that is really installed, so these
    use whatever is on this machine rather than a hard-coded path."""

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

    @unittest.skipUnless(ZSH, "zsh not installed")
    def test_zsh_wrapper_sources_real_rc_then_adds_hooks(self):
        shell, name, wrapper = self.prepare(SECTAPE_SHELL=ZSH)
        self.assertEqual(name, "zsh")
        self.assertIsNotNone(wrapper)
        zshrc = (wrapper / ".zshrc").read_text()
        self.assertIn("SECTAPE_REAL_ZDOTDIR", zshrc)
        self.assertIn("_sectape_preexec", zshrc)
        self.assertIn("source $ZDOTDIR/.zshrc", zshrc)
        for name_ in (".zshenv", ".zprofile", ".zlogin"):
            self.assertTrue((wrapper / name_).exists(), name_)

    @unittest.skipUnless(BASH, "bash not installed")
    def test_bash_wrapper(self):
        shell, name, wrapper = self.prepare(SECTAPE_SHELL=BASH)
        self.assertEqual(name, "bash")
        rc = (wrapper / "bashrc").read_text()
        self.assertIn("_sectape_precmd", rc)
        self.assertIn(".bashrc", rc)

    @unittest.skipUnless(ZSH or BASH, "no supported shell installed")
    def test_wrapper_is_owner_only(self):
        _, _, wrapper = self.prepare(SECTAPE_SHELL=ZSH or BASH)
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)

    @unittest.skipUnless(ZSH or BASH, "no supported shell installed")
    def test_no_integration_builds_nothing(self):
        # A real shell, so this tests the flag rather than a missing binary.
        _, _, wrapper = self.prepare(no_integration=True,
                                     SECTAPE_SHELL=ZSH or BASH)
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


class TestIntegrationAvailability(unittest.TestCase):
    """Whether hooks are really installed, which the banner reports.

    It used to be answered from the command-line flag alone, so a fish or sh
    user was told "integration on" and then handed a transcript read back off
    the screen.
    """

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("SECTAPE_SHELL", "SHELL")}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def set_shell(self, path):
        os.environ["SECTAPE_SHELL"] = path

    def test_supported_shells(self):
        # Real paths only: a shell that is not installed cannot offer hooks,
        # however zsh-like its name looks.
        import shutil as _shutil
        found = [p for p in (_shutil.which("zsh"), _shutil.which("bash")) if p]
        if not found:
            self.skipTest("neither zsh nor bash is installed")
        for shell in found:
            self.set_shell(shell)
            self.assertTrue(integration_available(), shell)

    def test_unsupported_shells(self):
        for shell in ("/bin/fish", "/bin/sh", "/usr/bin/nu", "/bin/ksh", "/bin/tcsh"):
            self.set_shell(shell)
            self.assertFalse(integration_available(), shell)

    def test_the_flag_turns_it_off_for_a_supported_shell(self):
        self.set_shell("/bin/zsh")
        self.assertFalse(integration_available(no_integration=True))

    def test_it_agrees_with_what_prepare_shell_does(self):
        # The banner and the wrapper must never disagree.
        for shell in ("/bin/zsh", "/bin/bash", "/bin/fish", "/bin/sh"):
            for flag in (False, True):
                self.set_shell(shell)
                _, _, wrapper = prepare_shell(flag)
                try:
                    self.assertEqual(bool(wrapper), integration_available(flag),
                                     f"{shell} no_integration={flag}")
                finally:
                    if wrapper:
                        shutil.rmtree(wrapper, ignore_errors=True)

    def test_a_shell_that_is_not_installed_offers_no_integration(self):
        # The name still looks like zsh, but the recording falls back to
        # /bin/sh, which has no hooks - so the banner must not promise them.
        for shell in ("/nonexistent/zsh", "/nowhere/bash", "/nope/zsh"):
            self.set_shell(shell)
            self.assertFalse(integration_available(), shell)

    def test_a_shell_found_on_the_path_still_counts(self):
        import shutil as _shutil
        if not _shutil.which("zsh"):
            self.skipTest("zsh not on PATH")
        self.set_shell("zsh")
        self.assertTrue(integration_available())

    def test_no_wrapper_is_built_for_a_missing_shell(self):
        # And so no temporary directory is left behind for one either.
        self.set_shell("/nonexistent/zsh")
        _, _, wrapper = prepare_shell(False)
        self.assertIsNone(wrapper)

    def test_the_shell_is_chosen_from_the_environment(self):
        os.environ.pop("SECTAPE_SHELL", None)
        os.environ["SHELL"] = "/bin/bash"
        self.assertEqual(chosen_shell(), ("/bin/bash", "bash"))
        os.environ["SECTAPE_SHELL"] = "/bin/zsh"
        self.assertEqual(chosen_shell(), ("/bin/zsh", "zsh"))

    def test_it_falls_back_when_nothing_is_set(self):
        for key in ("SECTAPE_SHELL", "SHELL"):
            os.environ.pop(key, None)
        self.assertEqual(chosen_shell()[1], "zsh")


if __name__ == "__main__":
    unittest.main()
