"""The shell-integration wrappers and the hooks they install.

These are shipped as shell source, so the only honest way to test them is to
run a real shell over them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sectape import __version__
from sectape.markers import (BASH_HOOKS, ZSH_HOOKS, build_bash_wrapper,
                             build_zsh_wrapper, sh_quote)


def marker_fields(output: str) -> list[str]:
    """The MARK|... line a probe printed, split into its fields.

    The shell's own integration marker can land on the same line without a
    newline between them, so the marker is found anywhere in the output.
    """
    for line in output.split("\n"):
        index = line.find("MARK|")
        if index != -1:
            return line[index:].split("|")
    return []

ZSH = shutil.which("zsh")
BASH = shutil.which("bash")

# Directory names that used to break the generated wrapper, which interpolated
# the path into shell source unquoted.
AWKWARD = ["plain", "with space", "od$d ir", "quo'te dir", "brace}dir",
           "back`tick`dir", 'dq"dir', "semi;dir", "star*dir"]


class TestShellQuoting(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(sh_quote("/home/u"), "'/home/u'")

    def test_a_single_quote_is_escaped(self):
        self.assertEqual(sh_quote("it's"), "'it'\\''s'")

    def test_metacharacters_are_literal(self):
        for text in ("a$b", "a`b`", "a}b", 'a"b', "a;b", "a*b", "a b"):
            self.assertEqual(sh_quote(text), "'" + text + "'")

    @unittest.skipUnless(ZSH or BASH, "no shell available")
    def test_a_real_shell_reads_it_back_unchanged(self):
        shell = ZSH or BASH
        for text in AWKWARD:
            out = subprocess.run([shell, "-c", f"printf '%s' {sh_quote(text)}"],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout, text, text)


@unittest.skipUnless(ZSH, "zsh not available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestZshWrapper(unittest.TestCase):
    """The wrapper must source the user's own rc files before adding hooks."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="sectape-zdot-"))
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.saved = os.environ.get("ZDOTDIR")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            os.environ.pop("ZDOTDIR", None)
        else:
            os.environ["ZDOTDIR"] = self.saved

    def probe(self, name: str) -> tuple[str, str]:
        """Build a wrapper for a ZDOTDIR called `name` and start a zsh in it."""
        real = self.base / name
        real.mkdir(parents=True, exist_ok=True)
        (real / ".zshrc").write_text("export REAL_RC=yes\n", encoding="utf-8")
        wrapper = self.base / "wrapper"
        shutil.rmtree(wrapper, ignore_errors=True)
        os.environ["ZDOTDIR"] = str(real)
        build_zsh_wrapper(wrapper)
        result = subprocess.run(
            [ZSH, "-i", "-c", 'print -r -- "MARK|${REAL_RC:-NO}|$SECTAPE_REAL_ZDOTDIR"'],
            env=dict(os.environ, ZDOTDIR=str(wrapper), SECTAPE_ACTIVE="1"),
            capture_output=True, text=True, timeout=60)
        fields = marker_fields(result.stdout)
        self.assertTrue(fields, f"no marker in output: {result.stdout!r} "
                                f"{result.stderr!r}")
        return fields[1], "|".join(fields[2:])

    def test_the_users_rc_is_sourced_whatever_the_directory_is_called(self):
        # A `$` in the path expanded, so the wrapper pointed at a directory
        # that did not exist and the recorded shell started with none of the
        # user's own configuration.
        for name in AWKWARD:
            sourced, resolved = self.probe(name)
            self.assertEqual(sourced, "yes", f"{name}: .zshrc was not sourced")
            self.assertEqual(resolved, str(self.base / name), name)

    def test_the_hooks_are_installed(self):
        real = self.base / "hooks"
        real.mkdir(parents=True, exist_ok=True)
        (real / ".zshrc").write_text("", encoding="utf-8")
        wrapper = self.base / "wrapper-hooks"
        os.environ["ZDOTDIR"] = str(real)
        build_zsh_wrapper(wrapper)
        result = subprocess.run(
            [ZSH, "-i", "-c",
             'print -r -- "MARK|${SECTAPE_INTEGRATION_LOADED:-NO}|$(_sectape_now)"'],
            env=dict(os.environ, ZDOTDIR=str(wrapper), SECTAPE_ACTIVE="1"),
            capture_output=True, text=True, timeout=60)
        fields = marker_fields(result.stdout)
        self.assertTrue(fields, result.stdout + result.stderr)
        loaded, now = fields[1], fields[2]
        self.assertEqual(loaded, "1")
        self.assertGreater(float(now), 1_600_000_000, "timestamp is not an epoch")
        self.assertNotIn(",", now, "a locale comma reached the marker")


@unittest.skipUnless(BASH, "bash not available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestBashWrapper(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="sectape-bashrc-"))
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))

    def test_the_users_bashrc_is_sourced_and_hooks_installed(self):
        home = self.base / "home"
        home.mkdir(parents=True, exist_ok=True)
        (home / ".bashrc").write_text("export REAL_RC=yes\n", encoding="utf-8")
        rcfile = self.base / "wrapper" / "bashrc"
        build_bash_wrapper(rcfile)
        result = subprocess.run(
            [BASH, "--rcfile", str(rcfile), "-i", "-c",
             'printf "MARK|%s|%s\\n" "${REAL_RC:-NO}" "$(_sectape_now)"'],
            env=dict(os.environ, HOME=str(home), SECTAPE_ACTIVE="1"),
            capture_output=True, text=True, timeout=60)
        fields = marker_fields(result.stdout)
        self.assertTrue(fields, result.stdout + result.stderr)
        sourced, now = fields[1], fields[2]
        self.assertEqual(sourced, "yes")
        # `date +%s.%N` is a GNU extension; BSD date prints a literal N.
        self.assertNotIn("N", now, "the timestamp is not a number")
        self.assertNotIn(",", now, "a locale comma reached the marker")
        self.assertGreater(float(now), 1_600_000_000)


@unittest.skipUnless(ZSH, "zsh not available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestTheNoteHelperDoesNotClobber(unittest.TestCase):
    """`note` is a convenience, not a claim on the name.

    zsh's `$+commands` sees only external commands, so a `note` function or
    alias of the user's own was silently replaced inside every recording.
    bash's `command -v` had this right already.
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="sectape-note-"))
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.saved = os.environ.get("ZDOTDIR")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            os.environ.pop("ZDOTDIR", None)
        else:
            os.environ["ZDOTDIR"] = self.saved

    def run_note(self, rc_body: str, label: str) -> str:
        real = self.base / label
        real.mkdir(parents=True, exist_ok=True)
        (real / ".zshrc").write_text(rc_body, encoding="utf-8")
        wrapper = self.base / ("wrap-" + label)
        shutil.rmtree(wrapper, ignore_errors=True)
        os.environ["ZDOTDIR"] = str(real)
        build_zsh_wrapper(wrapper)
        result = subprocess.run(
            [ZSH, "-i", "-c", "note hello"],
            env=dict(os.environ, ZDOTDIR=str(wrapper), SECTAPE_ACTIVE="1",
                     SECTAPE_BIN="/bin/echo", SECTAPE_BIN_ARGS=""),
            capture_output=True, text=True, timeout=60)
        return result.stdout.strip().split("\n")[-1]

    def test_a_users_own_function_wins(self):
        self.assertEqual(self.run_note('note() { echo "MINE"; }\n', "fn"), "MINE")

    def test_a_users_own_alias_wins(self):
        self.assertEqual(self.run_note('alias note="echo MINE"\n', "al"),
                         "MINE hello")

    def test_the_helper_is_defined_when_the_name_is_free(self):
        self.assertEqual(self.run_note("\n", "free"), "note hello")


@unittest.skipUnless(BASH, "bash not available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestTheBashNoteHelperDoesNotClobber(unittest.TestCase):
    def test_a_users_own_function_wins(self):
        base = Path(tempfile.mkdtemp(prefix="sectape-bnote-"))
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        (home / ".bashrc").write_text('note() { echo "MINE"; }\n', encoding="utf-8")
        rcfile = base / "wrap" / "bashrc"
        build_bash_wrapper(rcfile)
        result = subprocess.run(
            [BASH, "--rcfile", str(rcfile), "-i", "-c", "note hello"],
            env=dict(os.environ, HOME=str(home), SECTAPE_ACTIVE="1",
                     SECTAPE_BIN="/bin/echo", SECTAPE_BIN_ARGS=""),
            capture_output=True, text=True, timeout=60)
        self.assertIn("MINE", result.stdout)


@unittest.skipUnless(BASH, "bash not available")
@unittest.skipIf(sys.platform == "win32", "POSIX only")
class TestAnExistingDebugTrapIsKept(unittest.TestCase):
    """bash has no preexec hook, so several tools ride the DEBUG trap.

    bash-preexec, Atuin and various shell integrations install one. Taking it
    outright broke them for the length of the recording - while PROMPT_COMMAND,
    set two lines earlier, was being carefully preserved.
    """

    def probe(self, rc_body: str) -> tuple[int, bool]:
        base = Path(tempfile.mkdtemp(prefix="sectape-trap-"))
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        home = base / "home"
        home.mkdir(parents=True)
        (home / ".bashrc").write_text(rc_body, encoding="utf-8")
        rcfile = base / "wrap" / "bashrc"
        build_bash_wrapper(rcfile)
        marks = base / "marks.txt"
        marks.write_text("", encoding="utf-8")
        result = subprocess.run(
            [BASH, "--rcfile", str(rcfile), "-i", "-c", "echo SENTINEL_CMD"],
            env=dict(os.environ, HOME=str(home), SECTAPE_ACTIVE="1",
                     MARKFILE=str(marks)),
            capture_output=True, text=True, timeout=60)
        fired = sum(1 for line in marks.read_text().split("\n")
                    if "SENTINEL_CMD" in line)
        return fired, "]7337;SECTAPE;b|" in result.stdout

    def test_a_simple_prior_trap_still_runs(self):
        fired, marked = self.probe(
            'my_pre() {{ echo "MY:$BASH_COMMAND" >> "$MARKFILE"; }}\n'
            'trap "my_pre" DEBUG\n'.replace("{{", "{").replace("}}", "}"))
        self.assertEqual(fired, 1, "the user's DEBUG trap was replaced")
        self.assertTrue(marked, "sectape stopped emitting its own marker")

    def test_a_compound_prior_trap_still_runs(self):
        fired, marked = self.probe(
            'a() { echo "A:$BASH_COMMAND" >> "$MARKFILE"; }\n'
            'b() { :; }\n'
            "trap 'a; b' DEBUG\n")
        self.assertEqual(fired, 1, "a multi-command trap was mishandled")
        self.assertTrue(marked)

    def test_sectape_still_works_with_no_prior_trap(self):
        fired, marked = self.probe("\n")
        self.assertEqual(fired, 0)
        self.assertTrue(marked)


class TestHookSource(unittest.TestCase):
    def test_both_hook_sets_format(self):
        for hooks in (ZSH_HOOKS, BASH_HOOKS):
            text = hooks.format(version=__version__)
            self.assertIn("_sectape_preexec", text)
            self.assertIn("_sectape_precmd", text)
            self.assertIn("_sectape_now", text)
            self.assertNotIn("{{", text, "an escaped brace survived formatting")

    def test_neither_shell_claims_the_note_name_blindly(self):
        for hooks in (ZSH_HOOKS, BASH_HOOKS):
            text = hooks.format(version=__version__)
            self.assertIn("command -v note", text,
                          "the helper must look for a function or alias too")

    def test_bash_keeps_a_prior_debug_trap(self):
        text = BASH_HOOKS.format(version=__version__)
        self.assertIn("trap -p DEBUG", text,
                      "the hooks must look for a trap already installed")

    def test_bash_hooks_do_not_rely_on_gnu_date_alone(self):
        text = BASH_HOOKS.format(version=__version__)
        self.assertIn("EPOCHREALTIME", text)
        self.assertIn("*N*", text, "no fallback for a date without %N")


if __name__ == "__main__":
    unittest.main()
