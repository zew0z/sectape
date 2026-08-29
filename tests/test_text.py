import unittest

from sectape import config
from sectape.text import (base_command, clean_terminal_output, commands_in,
                          redact)
from tests.helpers import TempConfig


class TestBaseCommand(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(base_command("nmap -sV 10.10.1.1"), "nmap")

    def test_sudo_and_env_prefixes(self):
        self.assertEqual(base_command("sudo nmap -sS x"), "nmap")
        self.assertEqual(base_command("sudo -u root nano f"), "nano")
        self.assertEqual(base_command("FOO=bar time curl x"), "curl")

    def test_absolute_path(self):
        self.assertEqual(base_command("/usr/bin/gobuster dir"), "gobuster")

    def test_script_invocations_kept(self):
        self.assertEqual(base_command("./deploy.sh"), "deploy.sh")
        self.assertEqual(base_command("python3 exploit.py"), "python3")

    def test_pasted_url_is_not_a_program(self):
        self.assertEqual(base_command("http://10.1.1.1/uploads/shell.php"), "")
        self.assertEqual(base_command("https://x.test/a?b=c"), "")

    def test_url_as_argument_is_fine(self):
        self.assertEqual(base_command("curl http://10.1.1.1/x"), "curl")

    def test_empty(self):
        self.assertEqual(base_command(""), "")
        self.assertEqual(base_command("   "), "")


class TestRedaction(unittest.TestCase):
    def test_private_key_block(self):
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
        self.assertNotIn("AAAA", redact(text))

    def test_bearer_token(self):
        self.assertIn("<REDACTED>", redact("Authorization: Bearer abc.def.ghi"))

    def test_closing_quote_survives(self):
        out = redact("echo 'Authorization: Bearer abc.def.ghi'")
        self.assertTrue(out.endswith("'"), out)

    def test_cloud_and_vcs_tokens(self):
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redact("id AKIAIOSFODNN7EXAMPLE"))
        self.assertNotIn("ghp_", redact("ghp_" + "a" * 30))
        self.assertNotIn("xoxb-", redact("xoxb-" + "1" * 20))

    def test_ordinary_content_untouched(self):
        for keep in ("hydra -l bob -P rockyou.txt ssh://10.10.1.1",
                     "password: hunter2", "root:toor", "git commit -m 'key fix'"):
            self.assertEqual(redact(keep), keep)

    def test_disabled(self):
        s = "Authorization: Bearer abc.def.ghi"
        self.assertEqual(redact(s, enabled=False), s)


class TestCleaning(TempConfig):
    def test_a_lone_art_line_is_kept(self):
        # A single art-heavy line is a table border or a horizontal rule, not
        # a banner. Snipping those individually gutted every `mysql` table.
        table = ("+----+-------+\n| id | name  |\n+----+-------+\n"
                 "| 1  | alice |\n+----+-------+")
        self.assertEqual(clean_terminal_output(table), table)

    def test_a_lone_horizontal_rule_is_kept(self):
        out = clean_terminal_output("Results\n" + "=" * 30 + "\nport 80 open")
        self.assertIn("=" * 30, out)
        self.assertNotIn("SNIP", out)

    def test_two_art_lines_in_a_row_are_a_banner(self):
        out = clean_terminal_output("hi\n" + "\n".join(["/\\/\\/\\/\\/\\/\\/\\/\\/\\"] * 2) + "\nbye")
        self.assertEqual(out.split("\n"), ["hi", "<SNIP: ASCII-art banner>", "bye"])

    def test_banner_snip_keeps_surrounding_order(self):
        out = clean_terminal_output(
            "before\n" + "\n".join(["|||||||||||||||||||"] * 3) + "\nafter")
        self.assertEqual(out.split("\n"),
                         ["before", "<SNIP: ASCII-art banner>", "after"])

    def test_ascii_banner_snipped(self):
        banner = "\n".join(["/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\"] * 5)
        out = clean_terminal_output(banner + "\nreal output")
        self.assertIn("<SNIP: ASCII-art banner>", out)
        self.assertIn("real output", out)
        self.assertEqual(out.count("<SNIP: ASCII-art banner>"), 1)

    def test_long_line_snipped(self):
        out = clean_terminal_output("x" * 500)
        self.assertIn("<SNIP: line over 300 chars>", out)
        self.assertLess(len(out), 350)

    def test_many_lines_snipped_but_head_and_tail_kept(self):
        out = clean_terminal_output("\n".join(f"line{i}" for i in range(1000)))
        self.assertIn("line0", out)
        self.assertIn("line999", out)
        self.assertIn("more lines", out)

    def test_limits_come_from_configuration(self):
        config.override(max_output_lines=5)
        out = clean_terminal_output("\n".join(f"line{i}" for i in range(200)))
        self.assertLessEqual(len(out.split("\n")), 6)
        self.assertIn("line0", out)
        self.assertIn("line199", out)
        self.assertIn("195 more lines", out)

    def test_small_limits_still_truncate(self):
        # A head slice of max_lines-20 goes negative below 20 and used to keep
        # the whole block.
        for limit in (1, 2, 5, 19, 20, 21):
            config.override(max_output_lines=limit)
            out = clean_terminal_output("\n".join(f"l{i}" for i in range(500)))
            # floor of 3: one head line, the snip marker, one tail line
            self.assertLessEqual(len(out.split("\n")), max(3, limit + 1),
                                 f"limit={limit}")

    def test_char_limit_enforced(self):
        config.override(max_output_chars=100)
        out = clean_terminal_output("\n".join("x" * 40 for _ in range(50)))
        self.assertLess(len(out), 200)
        self.assertIn("truncated", out)

    def test_blank_runs_collapsed(self):
        self.assertEqual(clean_terminal_output("a\n\n\n\n\nb"), "a\n\nb")

    def test_empty_input(self):
        self.assertEqual(clean_terminal_output(""), "")


class TestRedactionOfCurrentTokenFormats(TempConfig):
    """The built-in patterns are meant to be high confidence, not exhaustive.

    Several of these are the format their issuer now steers people towards,
    while the pattern only knew the one it replaced.
    """

    SECRETS = {
        "github fine-grained pat": "github_pat_11ABCDEFG0" + "abcdefghij" * 6,
        "github classic pat": "ghp_" + "A" * 36,
        "aws long-lived key": "AKIAIOSFODNN7EXAMPLE",
        "aws temporary key": "ASIAIOSFODNN7EXAMPLE",
        "google api key": "AIzaSy" + "G" * 33,
        "stripe secret key": "sk_live_" + "H" * 32,
        "stripe restricted key": "rk_live_" + "H" * 32,
        "stripe test key": "sk_test_" + "H" * 32,
        "npm token": "npm_" + "I" * 36,
        "pypi token": "pypi-AgEIcHlwaS5vcmc" + "J" * 40,
        "openai key": "sk-" + "D" * 40,
        "slack bot token": "xoxb-123456789012-abcdefghijkl",
    }

    ORDINARY = {
        "a plain url": "curl https://example.com/api",
        "a url with a port": "curl https://example.com:8080/health",
        "an ssh remote": "git clone git@github.com:zew0z/sectape.git",
        "a url with only a user": "psql postgres://readonly@db.internal/app",
        "the prefix in prose": "AIzaSy is the prefix google uses",
        "an env var name": "npm_config_registry is an env var",
        "an scp path": "scp file user@host:/tmp/x",
        "a short hex string": "commit 4f2a9c1",
        "a normal sentence": "the deploy key rotated at 09:15",
    }

    def test_every_shape_is_redacted(self):
        for name, secret in self.SECRETS.items():
            self.assertNotIn(secret, redact(f"echo {secret}"), name)

    def test_the_replacement_says_what_it_was(self):
        self.assertIn("github token", redact(self.SECRETS["github fine-grained pat"]))
        self.assertIn("aws key id", redact(self.SECRETS["aws temporary key"]))

    def test_ordinary_output_is_left_alone(self):
        for name, text in self.ORDINARY.items():
            self.assertEqual(redact(text), text, name)

    def test_a_password_in_a_url_goes_but_the_host_stays(self):
        out = redact("curl https://user:hunter2@internal.example.com/api")
        self.assertNotIn("hunter2", out)
        self.assertIn("internal.example.com", out)
        self.assertIn("user", out)

    def test_redaction_can_be_turned_off(self):
        secret = self.SECRETS["aws temporary key"]
        self.assertEqual(redact(secret, enabled=False), secret)

    def test_a_secret_inside_a_longer_line_is_still_caught(self):
        line = f"  export TOKEN={self.SECRETS['npm token']}  # for CI"
        out = redact(line)
        self.assertNotIn(self.SECRETS["npm token"], out)
        self.assertIn("# for CI", out)


class TestCommandsInALine(unittest.TestCase):
    """One typed line can run several programs."""

    def test_a_single_command(self):
        self.assertEqual(commands_in("ls -la"), ["ls -la"])

    def test_a_pipeline(self):
        self.assertEqual(commands_in("cat f | grep x"), ["cat f", "grep x"])

    def test_a_pipeline_without_spaces(self):
        self.assertEqual(commands_in("cat f|grep x"), ["cat f", "grep x"])

    def test_every_separator(self):
        for line, count in (("a && b", 2), ("a || b", 2), ("a ; b", 2),
                            ("a | b | c", 3), ("sleep 1 &", 1)):
            self.assertEqual(len(commands_in(line)), count, line)

    def test_a_pipe_inside_quotes_is_an_argument(self):
        self.assertEqual(commands_in("grep 'a|b' notes.txt"),
                         ["grep a|b notes.txt"])

    def test_an_option_value_containing_a_pipe(self):
        self.assertEqual(len(commands_in("awk -F'|' '{print $2}' data.csv")), 1)

    def test_a_line_that_will_not_tokenise_is_left_whole(self):
        line = 'echo "unbalanced'
        self.assertEqual(commands_in(line), [line])

    def test_a_redirection_is_not_a_separator(self):
        self.assertEqual(commands_in("sudo tee /etc/hosts < in.txt"),
                         ["sudo tee /etc/hosts < in.txt"])

    def test_empty_input(self):
        self.assertEqual(commands_in(""), [""])


if __name__ == "__main__":
    unittest.main()
