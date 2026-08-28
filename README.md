# sectape

Record a terminal session as **commands**, not bytes.

`script(1)` hands you a stream of escape codes. `asciinema` gives you a replay.
Neither gives you the thing you usually want afterwards: *which commands ran,
what each one printed, what it exited with, and how long it took.*

```console
$ sectape rec "cert renewal"
● sectape 4.0.0 recording  pane #48213
  label       cert renewal
  log         ~/.sectape/sessions/cert_renewal/pane_48213.raw
  terminal    173x48   shell integration: on
  another tab? `sectape attach`   finish with `exit`

$ ... work normally ...
$ exit

■ pane #48213 stopped.
✓ ~/sectape/cert renewal.md
  14 commands from 1 pane(s) via shell integration, 2 failed
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

```bash
pipx install sectape
```

Requires Python 3.11+ on macOS or Linux. No dependencies.

## Use

```
sectape rec [label]        record a session; exit the shell to finish
sectape attach             record another tab or tmux pane into the same session
sectape stop               export now and end the session
sectape list               what has been recorded
sectape show [session]     print a transcript to stdout
sectape export [session]   write it to a file  (-f markdown|json|text, -o PATH)
sectape status             what is recording right now
sectape rm <session>       delete raw logs
sectape config init        write a config file you can edit
sectape doctor             check the install
```

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

Sessions recorded without integration (`--no-integration`, a shell other than
zsh/bash, an ssh session inside the recording) fall back to reading commands
off the rendered screen, and the export says so.

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

Three formats, selected with `-f` or `output.format` in the config:

| Format | What it's for |
|---|---|
| `markdown` | A readable document. Content between `<!-- sectape:begin -->` and `<!-- sectape:end -->` is regenerated; anything you write outside it is preserved. |
| `json` | Structured steps — command, output, exit code, cwd, duration — for feeding somewhere else. |
| `text` | Plain prompt-and-output, good for piping. |

## Configuration

`sectape config init` writes `~/.config/sectape/config.toml`:

```toml
[general]
state_dir = "~/.sectape"
prompt = "$"
redact = true
shell_integration = true

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
  from **exports**. It does not rewrite the raw logs.
- `sectape rm <session> --yes` deletes a recording's raw logs.

## Licence

MIT.
