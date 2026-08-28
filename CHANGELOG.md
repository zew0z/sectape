# Changelog

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
