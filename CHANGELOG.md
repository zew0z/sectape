# Changelog

## 4.1.0

### Fixed

- **Short writes truncated output.** `os.write` on a tty or pipe may accept
  fewer bytes than it is given — a large paste, or output arriving faster than
  the terminal drains it. Mirroring and logging both used a bare `os.write`,
  so in exactly those cases bytes were dropped from the log, the child's input,
  or the screen. All four call sites now loop until the buffer is empty.
- **A temporary directory leaked per recording.** The shell-integration wrapper
  was built in the forked child, which had no opportunity to remove it; the
  parent now owns it and cleans up when the session ends.
- **Pane logs were world-readable.** The state tree is created `0700` and pane
  logs `0600`. They contain everything the terminal displayed.
- **Multi-pane sessions were not chronological.** Reading pane logs end to end
  put every command from the first pane before the second's. Steps carrying a
  marker timestamp are now merged by time.
- **`list` and `status` over-counted.** They counted every marker, including
  the `exit`/`clear`/`sectape` lines the exporter drops, so the numbers
  disagreed with the export.
- **bash recorded its own setup.** The `DEBUG` trap was installed before
  `PROMPT_COMMAND` was assigned, so the trap captured that assignment as the
  session's first command.
- **bash truncated compound commands.** `BASH_COMMAND` holds only the current
  simple command, so `a; b` and loops were recorded as their first clause. The
  typed line now comes from `history 1`.
- **`export`/`show` crashed** on a session record with no `dir` key.
- **Output truncation silently did nothing** when the line limit was under 20,
  because the head slice went negative.
- `rm` now says which recording a fuzzy name matched before deleting it.

### Added

- `sectape note <text>` — timestamped annotations, written from inside the
  recording and interleaved into exports between the right commands. Reads
  stdin when piped. Left out of the transcript itself.
- `html` export format: a self-contained page, no external assets, readable in
  light or dark.
- Filters on `export` and `show`: `--only-failed`, `--last N`, `--grep RE`,
  `--no-output`.
- Custom redaction patterns via `[redaction]` in the config file.
- `sectape completion zsh|bash`.
- Commands are attributed to their pane when a session recorded more than one.

### Tests

214 tests, up from 149. bash is now covered end to end — that path had never
been executed before this release.

## 4.0.0

First public release.

sectape began as a private TryHackMe note-builder. Everything specific to that
— the browser userscript, the local webhook receiver, lesson notes, tasks and
questions — has been removed. What is left is the part that was general all
along: a terminal recorder that gives you back commands rather than bytes.

### Recording

- The recording pseudo-terminal inherits the real terminal's window size and
  follows `SIGWINCH`. Without this the recorded shell runs at 80 columns
  whatever your terminal is, and every prompt redraw lands in the wrong place.
- The terminal is restored on every exit path — termios and escape state
  (alternate screen, mouse reporting, bracketed paste, SGR, scroll region) —
  including on `SIGTERM` and `SIGHUP`.
- Commands, exit codes, working directories and timings are captured exactly,
  via `preexec`/`precmd` hooks injected through a throwaway `ZDOTDIR` or
  `--rcfile` that first sources your own configuration.
- Sessions without integration fall back to reading commands off the rendered
  screen, and exports say so rather than pretending.
- Several panes can record into one session; it ends with the last pane, and
  re-running `rec` with the same label rejoins instead of replacing.

### Replay

- A small VT emulator resolves cursor motion, erases, tabs, autowrap and the
  alternate screen, so progress bars and readline redraws collapse to their
  final on-screen state.
- Wrapped rows are rejoined, so exports contain logical lines.
- Full-screen programs (`vim`, `less`, `top`, `man`, …) are summarised in one
  line instead of pasting a mangled screen.

### Output

- `markdown`, `json` and `text` writers.
- Markdown exports regenerate only the block between the sectape markers;
  anything you add outside it survives a re-export.
- High-confidence secrets — private key blocks, `Authorization:` headers,
  AWS/GitHub/Slack/OpenAI-shaped tokens — are stripped from exports by default.

### Interface

- `rec`, `attach`, `stop`, `export`, `show`, `list`, `status`, `rm`, `config`,
  `doctor`; `--json` on `list` and `status`.
- TOML configuration with `SECTAPE_*` environment overrides and CLI flags.
- Packaged with an entry point; no runtime dependencies.

### Compatibility

Logs written by the tool under its previous name still parse; their markers
carry the old payload tag and are read unchanged.
