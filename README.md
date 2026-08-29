# sectape

Record a terminal session as **commands**, not bytes.

`script(1)` hands you a stream of escape codes. `asciinema` gives you a replay.
Neither gives you the thing you usually want afterwards: *which commands ran,
what each one printed, what it exited with, and how long it took.*

```console
$ sectape rec "cert renewal"

  ⏺ REC  cert renewal
  ────────────────────────────────────────────────────────────────
    pane      1  · 173×48 · integration on
    tape      ~/.sectape/sessions/cert_renewal/pane_01.raw

    note "…" annotate   ·   sectape attach another pane   ·   exit finish

$ ... work normally ...
$ exit

  ⏹ STOP  cert renewal
  ────────────────────────────────────────────────────────────────
    14 commands · 2 failed · 12m 4s
    → ~/sectape/cert renewal.md
```

The export:

```markdown
### 7. `certbot renew --dry-run` ⚠️

14:22:07 · **exit 1** · 4.2s · `/etc/letsencrypt`

```console
$ certbot renew --dry-run
Failed to renew certificate example.com with error: ...
```
```

## Install

Not published to PyPI yet, so install it from a checkout:

```bash
git clone https://github.com/zew0z/sectape
pipx install ./sectape
```

Or, to keep working on it in place:

```bash
pipx install --editable ./sectape
```

Requires Python 3.11+ on macOS or Linux. No dependencies.

## Use

```
sectape rec [label]        record a session; exit the shell to finish
sectape attach             record another tab or tmux pane into the same session
sectape note <text>        annotate the running session
sectape stop               export now and end the session
sectape list               what has been recorded
sectape show [session]     print a transcript to stdout
sectape export [session]   write it to a file
sectape status             what is recording right now
sectape rm <session>       delete raw logs
sectape config init        write a config file you can edit
sectape completion zsh     emit a completion script
sectape doctor             check the install
```

`export` and `show` take `-f markdown|json|text|html`, `-o PATH`, and filters:
`--only-failed`, `--last N`, `--grep RE`, `--no-output`.

A filtered `export` is a subset, so it gets its own file — `lab (failed).md`,
`lab (last 20).md` — rather than overwriting the recording's complete
document. `-o` still puts it exactly where you say. Notes are narrowed with
the commands: a filtered document keeps the notes written while those commands
were running, not every note in the session.

## Notes while you work

A transcript tells you what you ran. It does not tell you *why*. From inside a
recording:

```console
$ sectape note "load is climbing - checking the worker pool"
  ❯ noted load is climbing - checking the worker pool
```

Notes are timestamped and land between the right commands in the export:

```markdown
> **note** · 19:18:47
> load climbing — checking the worker pool

### 2. `grep -c . /etc/hosts`
```

`sectape note` is itself left out of the transcript. Notes are redacted and
held to `max_output_lines` / `max_output_chars` in the export, like command
output; `notes.jsonl` keeps what you wrote in full.

Handy aliases:

```bash
alias rec='sectape rec'
alias recjoin='sectape attach'
```

## How it works

1. `rec` forks a pseudo-terminal, **sizes it to your real terminal**, and execs
   your shell through a throwaway `ZDOTDIR` (or `--rcfile`) that sources your
   own rc files and then adds `preexec`/`precmd` hooks.
2. Those hooks emit a private `OSC 7337` sequence carrying the exact command
   line, its exit code, working directory and timestamps. Terminals ignore OSC
   codes they don't know, so you never see them.
3. Everything the shell writes is mirrored to your terminal and appended to a
   pane log.
4. On exit the log is replayed through a small VT emulator — cursor motion,
   erases, autowrap, the alternate screen — and cut into commands at the
   markers.

A pane recorded without integration — `--no-integration`, or a shell other than
zsh/bash — falls back to reading commands off the rendered screen, and the
export says so.

An `ssh` session *inside* a recording is different: the outer shell still has
its hooks, so the pane is read from the markers as usual and everything you did
on the remote box appears as the output of the `ssh` command. The remote shell
has no hooks of its own, so its commands are not separate steps.

zsh uses `preexec`/`precmd`. bash has no preexec hook, so it rides the `DEBUG`
trap (a trap and a `PROMPT_COMMAND` of your own are kept and run first) and reads the typed line from `history 1` — `BASH_COMMAND` alone holds
only the current *simple* command, which would record `a; b` and loops as their
first clause. If you have disabled bash history, command text falls back to
`BASH_COMMAND` and compound lines are truncated.

### Things it gets right that a naive `script` wrapper doesn't

- The recorded shell inherits your real window size and follows `SIGWINCH`, so
  nothing wraps at a phantom 80 columns.
- Your terminal is restored on **every** exit path — termios *and* escape state
  (alternate screen, mouse reporting, bracketed paste, SGR, scroll region) —
  including on `SIGTERM`/`SIGHUP`.
- Progress bars, `\r` redraws and readline autosuggestions resolve to their
  final on-screen state instead of appearing as garbage.
- Full-screen programs (`vim`, `less`, `top`, `man`, …) are recorded as a
  one-line summary rather than a mangled screen dump.
- Multiple panes can record into one session; the session ends with the last
  one, not the first.

## Output

Four formats, selected with `-f` or `output.format` in the config:

| Format | What it's for |
|---|---|
| `markdown` | A readable document. The summary and transcript between `<!-- sectape:begin -->` and `<!-- sectape:end -->` are regenerated, and the YAML frontmatter is refreshed; prose you write around the block is preserved. |
| `json` | Structured steps — command, output, exit code, cwd, duration — for feeding somewhere else. |
| `text` | Plain prompt-and-output, good for piping. |
| `html` | A self-contained page — no external assets, readable in light or dark, fine to hand to someone. |

## Configuration

`sectape config init` writes `~/.config/sectape/config.toml`:

```toml
[general]
state_dir = "~/.sectape"
prompt = "$"
redact = true
shell_integration = true

[redaction]
# On top of the built-in patterns. Whatever these match is replaced wholesale.
patterns = ["CORP-[0-9]{6}", "internal\\.example\\.com"]
replacement = "<REDACTED>"

[output]
dir = "~/sectape"
format = "markdown"
max_output_lines = 300
max_output_chars = 20000
```

Every value has a `SECTAPE_*` environment variable equivalent, and `--state-dir`
/ `--output-dir` / `--no-redact` override both.

## Privacy

**The raw pane log records everything your terminal displayed.** That is the
point of the tool, but it means the logs under `state_dir` deserve the same
care as your shell history — more, since they include command *output*.

- Passwords typed at a hidden prompt (`sudo`, `ssh`, `passwd`) are never echoed,
  so they are not in the log.
- Anything you `cat`, `echo` or paste **is**.
- `redact = true` (the default) strips high-confidence secrets — private key
  blocks, `Authorization:` headers, AWS/GitHub/Slack/OpenAI-shaped tokens —
  from **exports**, transcript and notes alike. It does not rewrite the raw
  logs or `notes.jsonl`. Add your own patterns under `[redaction]`.
- The state directory, the recordings in it, the pane logs and your notes
  are all owner-only (`0700`/`0600`). Exports follow your umask, since they
  are the documents you hand to someone else.
- `sectape rm <session> --yes` deletes a recording's raw logs.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

596 tests, no dependencies. The end-to-end ones drive the real CLI through a
pseudo-terminal and assert that the recorded shell sees the right `$COLUMNS`,
follows a resize, restores the terminal on `SIGTERM`, and produces exports with
exact commands and exit codes, under both zsh and bash. CI runs them on macOS
and Linux across Python 3.11–3.13.

## Licence

MIT.
