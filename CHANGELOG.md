# Changelog

## Unreleased

### Fixed

- **`resolve_session_dir` did not keep the promise in its own docstring.** The
  direct lookup refuses anything that is not a direct child of the sessions
  directory, but the name-matching fallback beneath it returned the entry as
  it found it, unchecked - so a symlink inside the sessions directory resolved
  to a target outside it and was handed back as a recording. Nothing was
  deleted, because `sectape rm` re-checks containment before it removes
  anything; that second check was simply carrying the whole weight rather than
  being the belt-and-braces it reads as. The fallback now applies the same
  check, and a symlink pointing *within* the tree still resolves.

- **`sectape rm --yes` could delete a recording that was still running.** The
  guard that refuses to do that compared the directory stored in
  `current.json`, which is unresolved, against the resolved path the resolver
  returns. Those disagree as text the moment any component of the path is a
  symlink - and on macOS `/tmp` is one, so a state directory under `/tmp`,
  which scripts and CI pick constantly, made them differ for every session.
  The message never appeared, the pane logs of a live recording were removed
  while it was still being written to, and the export at the end found
  nothing. Both sides are now resolved before they are compared. The same
  comparison decided the `REC` marker in `sectape list`, which had never
  appeared for those sessions either.

- **Closing the terminal mid-recording threw the export away.** When the
  window is shut, or an ssh connection drops, the pty the recorder was
  mirroring to goes with it. The teardown then printed its summary to a
  terminal that no longer existed, which raises `EIO` - and that escaped from
  the middle of the teardown, *before* the export ran. The pane log survived
  on disk, but nothing reached the output directory and `current.json` still
  claimed the session was live. Telling you what happened is worth less than
  finishing the recording, so a report that cannot be delivered is now
  dropped and the export goes ahead.

- **A command whose end marker never arrived was dropped from the export.**
  When the shell does not reach its prompt hook, no `e|` marker is written.
  The parser kept such a command if the log ended there, but overwrote it if
  another command followed - so the command text, its output and its timing
  all vanished, and `sectape list` reported a count the export did not match.
  It is now closed out at the next marker with an unknown exit code, which is
  the honest answer: it ran, and how it ended was never recorded.

## 5.0.0

### Compatibility

Behaviour an existing setup may notice. Each is described in full below.

- A `--config` path that does not exist is now an error rather than silently
  ignored, and config values are checked against their type: `redact = "no"`
  used to mean *true*, and now says so instead.
- `export` with a filter writes `<label> (failed).md` rather than overwriting
  `<label>.md`. `-o` is unaffected.
- Exports are created with your umask instead of always `0600`; the state tree
  is unchanged and stays owner-only.
- `stop --force` stops the panes that are still recording instead of leaving
  them running.
- Notes are held to `max_output_lines` / `max_output_chars` in exports, as
  command output already was. `notes.jsonl` is unchanged.
- `list --json` gained a `label` field alongside `session`.

### Fixed

- **One hand-edited line in `notes.jsonl` made a whole recording
  unexportable.** `read_notes` skips a blank line, a line that is not JSON, a
  record that is not an object and a record with no text - and then raised on
  an `at` it could not turn into a number, which came out of `sectape show` and
  `sectape export` as a traceback and took every good note in the file with it.
  An unreadable timestamp now falls back to `0.0`, which is what a missing one
  has always used, so the note keeps its text and loses only its place.

- **A private key could reach the export with only its header removed.**
  Command output was trimmed to `max_output_lines` first and redacted second.
  The private-key pattern is the one that has to match both ends of itself, so
  when the trim dropped the middle of a long log it took the `-----END-----`
  line with it, and the redaction that followed had nothing left to match: the
  `-----BEGIN RSA PRIVATE KEY-----` header and nineteen lines of key material
  went into the document verbatim. The same held for a key on a single line
  over 300 characters, which is cut to 250. Output is now redacted before it is
  trimmed, everywhere - notes already were. `--no-redact` is unchanged.

- **`sectape rm` could delete a directory outside the session tree.** A name
  from the command line was tried as a raw path first, so
  `sectape rm ../../work --yes` resolved to, and then removed, something that
  was never a recording; an absolute path did the same. A recording is now
  required to be a direct child of the sessions directory, and the delete
  itself re-checks that before removing anything.
- **A `$` in `ZDOTDIR` or `$HOME` silently stripped your shell configuration.**
  The throwaway wrapper interpolated the real directory into shell source
  unquoted, so a path containing `$`, a backtick, a brace or a quote resolved
  to something else - and the recorded shell started with none of your own rc
  files, prompt or aliases. Paths are now quoted.
- **`sectape stop --force` orphaned the recorders.** It deleted the session and
  left them running, so you were still in a shell logging to a recording that
  no longer existed, and that work never reached the export. `--force` now
  means what its help says: stop the panes too, without asking.
- **`sectape show | head` complained.** Closing the pipe early - `head`,
  `grep -m1`, quitting `less` - surfaced as a traceback, and once that was
  tidied, as `error: Broken pipe`. The reader going away is not an error: it
  is now silent, with the exit status a program killed by SIGPIPE would
  report. Output is also flushed while the error handlers are still in scope,
  so a failing flush cannot escape as "Exception ignored" and exit code 120.
- **A pane record with no usable pid was immortal, and could have signalled
  every process you own.** `pid_alive` is called with a default of `-1` for a
  pane that lost its pid, and `os.kill(-1, 0)` does not raise - it addresses
  every process the caller may signal - so such a pane counted as alive
  forever and the session could never go idle. Worse, `signal_panes` would
  then have passed `-1` to `os.kill` with SIGTERM. Only a positive id is now
  treated as a process, and the signal path checks again before it fires.
- **Every recording named in another script shared one directory.** With no
  ASCII left to slug, the fallback was the single fixed name `room`, so
  `日本語のセッション`, `별개의 세션` and `Привет мир` were all the same session
  and their commands merged into one export. Distinct labels now get distinct
  directories; the readable name still names the export.
- **A damaged `current.json` crashed the CLI.** `[1, 2, 3]` is valid JSON, so a
  state file holding a list (or a bare string, or a number) got past the
  loader and met a caller doing `.get` on it. Only a JSON object counts as
  state now; anything else reads as no session at all, which is what a
  malformed file already did.
- **Resizing the terminal mid-recording scrambled everything after it.** The
  replay used the first size marker for the whole capture, so once the window
  changed the wrap column was stale and a `\r` returned to the wrong row:
  `xxxxxxxx...OVERWRITTENxxxx` where the screen had shown `OVERWRITTEN...`.
  The replayer now follows every size marker, including one written while a
  command was still running. (`VTScreen.resize` existed for this and had never
  been wired up.)
- **A recording killed inside `vim` or `less` lost the whole transcript.** If
  the capture ended with the alternate screen still up - the recorder killed,
  or `sectape stop` run from another tab - the replay returned the redrawn
  full-screen buffer and discarded the real session, which is exactly
  backwards: the screen is the noise, the transcript is the point. The stashed
  transcript is now what gets exported.
- **Recording again under an existing label was silent, and the export said
  the wrong thing about it.** The earlier panes are kept and renumbered as
  though they had been open alongside the new one, so two separate days came
  out as "pane 1" and "pane 2" with an elapsed time of two seconds for a
  document spanning a day. `rec` now says it is appending and how to avoid it,
  and elapsed time and the document date are measured over the content that is
  actually in the export.
- **A virtualenv prompt made the fallback reader return nothing.** Without
  shell integration commands are read off the screen, and the patterns did not
  allow for the parenthesised name a virtualenv, conda env or toolbox puts in
  front of the prompt - so `(venv) user@host:~$` matched nothing and the whole
  session exported empty. Prompts with no path in them are recognised now too.
- **The state tree was not as private as documented.** The tree itself is
  0700, but the recording directories inside it were left to the umask
  (`drwxr-xr-x`) and `notes.jsonl` was created world-readable (`-rw-r--r--`)
  while the pane logs beside it were 0600. Everything holding raw terminal
  output or your own notes is owner-only now.
- **A narrow tmux pane was replayed at the wrong width.** The replay floored
  the terminal width at twenty columns, so a split pane narrower than that had
  its wrap column shifted and a `\r` returned to the wrong row - the same
  failure as an unfollowed resize. Only a width of zero now falls back to the
  default. Replays at twenty columns and wider are byte-for-byte unchanged.
- **The injected `note` helper replaced one of your own, under zsh.** The
  check used `$+commands`, which sees only external commands, so a `note`
  function or alias you had defined was silently shadowed for the length of
  every recording. bash, which used `command -v`, was already correct; zsh now
  does the same.
- **The bash hooks took over a `DEBUG` trap you already had.** bash has no
  preexec hook, so shell integrations ride that trap - bash-preexec, Atuin and
  others - and installing ours over it broke them for the length of every
  recording. `PROMPT_COMMAND`, set two lines earlier, was already being
  preserved; the trap now is too, and runs first.
- **A partly reconstructed transcript did not say so.** The warning that
  commands were read back off the screen appeared only when *every* step was,
  so a session mixing an integrated pane with one recorded without integration
  said nothing at all about the half that was less reliable. The notice now
  appears whenever any of it was, and says how much.
- **A `SHELL` naming a binary that is not installed still promised
  integration.** The check looked at the name, so `/nonexistent/zsh` counted
  as zsh; the recording falls back to `/bin/sh`, which has no hooks, and the
  banner said "integration on" anyway. The shell has to actually be there now,
  and no wrapper directory is built for one that is not.
- **`doctor` claimed integration the recording would not install.** It
  compared the shell's name, so `/nonexistent/zsh` passed the check while the
  recorder correctly refused it - and telling you whether things will work is
  the whole point of the command. It now asks the recorder the same question
  and says why the answer is no: not a supported shell, not installed, or
  turned off in the configuration.
- **The recording banner claimed shell integration it did not have.** It
  answered from the `--no-integration` flag alone, so `integration on` was
  printed for fish, sh or any shell whose hooks sectape cannot write - and for
  `shell_integration = false` in the config or the environment. You were
  promised exact command capture and handed a transcript read off the screen.
  The banner and `doctor` now ask the same question the recorder does.
- **A label containing a newline broke the document it named.** A label is a
  markdown heading, a filename and a row in `list` all at once, and none of
  those survive a line break in the middle - `sectape rec "$(some-command)"`
  is all it takes. Labels are collapsed to a single line. The YAML frontmatter
  was already quoted correctly and is unchanged.
- **Starting a different recording could lose the new one's export.** A
  recorder keeps working after it deregisters its pane - exporting, tidying -
  and `sectape rec other-label` waits only for the pane registry to empty, so
  the two overlap. In that window the old recorder finished and cleared
  *whatever session was current*, which by then was the new one: its state
  file was deleted, the pane allocation that followed rebuilt it from nothing,
  and the new recording lost its label and its directory and exported nothing
  at all. A recorder now finishes and clears only the session it was
  recording.
- **Tidying up raced with itself.** Removing the session file checked that it
  existed and then removed it, so two `sectape stop`s - or a stop and the last
  pane finishing - could each see it and the loser met a `No such file or
  directory`. `status` had the same shape when a pane log was removed while it
  was being measured. Both now treat "someone else already did it" as the
  success it is.
- **Writing a note on the same line as a command lost the command.** A line
  was judged by its first word alone, so `note "done" && systemctl restart
  app` counted as the `note` helper and was dropped whole - the restart with
  it. Every command on the line is considered now, so a line is only skipped
  when all of it is plumbing. `clear; exit` is correctly skipped for the same
  reason, where before it was kept as a step.
- **An export could destroy a file it had never written.** `merge` keeps what
  you add around the generated block, but a file with no block at all was
  replaced wholesale - so hand-written notes that happened to share a
  session's name were silently lost. A markdown export now writes beside such
  a file, as `notes (1).md`, and settles there on the next export because by
  then that file is its own. An explicit `-o` still writes exactly where you
  say, and the formats with no generated block are unchanged, since there is
  no way to tell our JSON from anyone else's.
- **The window-size marker was written from a signal handler.** Resizing the
  terminal wrote straight into the log from inside the `SIGWINCH` handler,
  which can interrupt a partial write and splice the marker into the middle
  of an escape sequence - corrupting that part of the replay. The resize
  itself still happens in the handler, because the shell has to hear about it
  promptly; only the marker is left for the loop, which writes it before
  anything else that wake records, so it still precedes the redraw it
  describes.
- **`attach` never finished a session.** Only `rec` closed a recording, so
  leaving the first tab before the attached one left no panes, no export and
  a `current.json` still claiming to be live — the whole recording was lost.
  Both commands now share one release path, and the last pane out finishes
  the session, as the README always said it did.
- **A command run twice was exported once.** Deduplication collapsed any two
  consecutive identical steps. That artifact belongs to the heuristic reader,
  which reads a redrawn prompt twice; a pair of markers is proof the command
  really ran, so `id` typed twice is now two steps. `list` and the export
  agree again.
- **Timestamps were lost on BSD and in non-English locales.** `date +%s.%N` is
  a GNU extension — BSD `date` prints a literal `N` — and bash formats
  `EPOCHREALTIME` with the locale's decimal point. Either produced a string
  `float()` refused, costing the recording every duration and all cross-pane
  ordering. The hooks now emit a portable timestamp and the parser tolerates
  both forms.
- **Elapsed time was wrong for anything but a fresh recording.** The end of a
  session defaulted to the moment of the export, so exporting last week's
  recording reported an elapsed time of days. It is now taken from the last
  thing that actually happened in the session.
- **Table borders were destroyed as ASCII art.** A single art-heavy line was
  replaced with a `<SNIP>` marker, gutting every `mysql`, `psql` and
  `column -t` table. A banner now needs at least two such lines in a row.
- **`cat`ting a markdown file broke the export.** Output containing a ```` ```
  ```` fence closed the generated `console` block early; the fence is now
  sized to the content. A command containing backticks likewise broke its
  own heading.
- **Merging lost steps after an embedded end marker.** A recording that `cat`s
  an older sectape export captures `<!-- sectape:end -->` as ordinary output,
  and merging split on that copy — silently truncating the document there.
- **Double-width characters were counted as one column.** CJK and emoji are
  two columns wide and combining marks are none, so any prompt containing one
  put every following cursor move in the wrong place. The replayer now tracks
  real screen columns. Raw C1 control bytes no longer reach the export either.
- **Filesystem errors ended in a traceback.** A read-only output directory, a
  full disk, or `-o` pointing at a directory now produce a clean message that
  names the file you asked for rather than the internal temporary one.
- **`--config` pointing at a missing file was ignored**, silently falling back
  to the defaults. A config you name on the command line must exist.
- **A re-export kept the first run's summary forever.** The derived counts sat
  outside the regenerated block, so exporting a session twice left a header
  reading "Commands: 1" above a body listing four, and stale YAML frontmatter
  to match. The summary now lives inside the block, the frontmatter is
  refreshed on merge, and prose you added around it is still preserved.
- **Notes were never redacted.** The transcript was scrubbed of high-confidence
  secrets before export but annotations were not, so a token pasted into
  `sectape note` reached the shared document verbatim. As with the transcript,
  only the export is scrubbed; `notes.jsonl` keeps what you wrote.
- **A recording of nothing but notes exported as empty.** The markdown writer
  listed "Notes: 1" and then printed "No commands were captured in this
  session", dropping them. The other three writers already kept them.
- **A filtered export overwrote the complete one.** `sectape export --only-failed`
  wrote its subset to the recording's own file, replacing a four-command
  document with a one-command document and saying nothing. A narrowed export
  now gets its own name - `lab (failed).md`, `lab (last 20).md` - and `-o`
  still puts it wherever you ask.
- **A mistyped config value corrupted exports or crashed.** `patterns = "oops"`
  under `[redaction]` was read as one regex per character, so every `o`, `p`
  and `s` in the export became `<REDACTED>` with no warning; a non-numeric
  `max_output_lines` escaped as a raw traceback; and `redact = "no"` silently
  meant the opposite, because a non-empty string is truthy. Every value is now
  type-checked and reported against the key it came from.
- **`list` columns did not line up.** Padding counted characters, so a
  recording named in Japanese or with an emoji in it pushed every column after
  it out of place - and the header was two columns off even for plain ASCII.
  The header and every row are now built from one template, measured in screen
  columns.
- Counts in messages are pluralised properly (`1 pane`, not `1 pane(s)`).

### Changed

- **A long label made a title as long as itself.** The export filename was
  capped at 120 characters and the session directory at 80, but the label
  itself reached the document title, the YAML frontmatter and the HTML
  `<title>` with no bound at all - so a label built from a command
  substitution gave a four-thousand-character heading. Capped at the same
  length as its own filename.
- **A long command made a heading as long as itself.** Output lines are cut at
  300 characters but commands were not, so a pasted `curl` with four hundred
  fields produced an eight-thousand-character markdown heading and an HTML
  card to match - unreadable, and useless in a table of contents. The heading
  is shortened at a word boundary now; the command in the block below it, and
  in every other format, is still complete.

- **A pager at the end of a pipeline left its redrawn screen in the export.**
  Full-screen programs are replaced with a one-line summary, but only the
  first program on the line was considered - so `less /var/log/syslog` was
  handled and `journalctl -u nginx | less` was not, which is how most people
  actually reach a pager. It matters most for `git log | less` and
  `systemctl status`, because both set `LESS=...X` and so stay on the primary
  screen where the redraw really is captured.
- **The `Programs` summary named only the first program on each line.** A
  session that ran `nmap ... | tee`, `cat | grep | wc` and `curl && jq`
  reported nmap, cat and curl and nothing else, so the summary was wrong about
  what the session had actually used. Lines are split on shell operators now.
  The split is done on tokens, so a pipe inside quotes - `grep 'a|b'`,
  `awk -F'|'` - stays an argument, and a line that will not tokenise is left
  whole.

- **The HTML page lost pane attribution in its failed-only view.** Panes were
  shown only as separators between steps, and that view hides them - which is
  exactly when you are comparing what went wrong across tabs. Each step now
  carries its own pane, as the markdown export already did.
- **The `text` export gave no sign that panes had changed.** Commands from
  several tabs interleave by time, and with nothing to mark the switch they
  read as one shell running everything in sequence - a `tail -f` in one tab
  looked as though it had exited before the next command started. Pane
  switches are marked with a `#` comment, like the header and notes, and only
  when a session has more than one pane.

- **A filtered export carried every note in the session, and then too few.** `--last 2` came out
  as ten unrelated annotations followed by the two commands you asked for.
  Notes are narrowed with the commands now. A note belongs to the last command
  that had started when it was written - the rule the timeline already places
  it by - so `--last 2` keeps those two commands and the notes about them.
  Scoping instead to when a command was actually *running* went too far the
  other way and dropped the note you write about what just happened, which is
  most of them. An unfiltered export is unchanged, and a capture with no
  timestamps to compare against keeps all of its notes.

- **Notes are held to the same size limits as command output.** Output has
  always been trimmed for readability; notes were not, so
  `cat big.log | sectape note` put the whole file into every export - 849 KB
  of text that the same limits would have cut to 12 KB as command output. The
  document is bounded and says how much it left out; `notes.jsonl` still keeps
  the note in full.

- **Exports follow your umask.** Atomic writes go through `mkstemp`, which
  always creates 0600, so the documents the tool exists to produce came out
  unshareable even though the output directory is documented as being left to
  the umask. A new export now gets the permissions a plain `open()` would have
  given it, and re-exporting keeps whatever permissions the file already had.
  The state tree is unaffected and stays owner-only.

- **`list` shows a real command count for far more recordings.** Counting had
  one size threshold for two paths that differ by roughly forty times in cost:
  a log with shell-integration markers only needs a regex sweep (24 MB in about
  0.2s), while one without has to be replayed and read off the screen. The two
  now have their own ceilings, so large marked sessions are counted instead of
  showing `?`, and the slow path is capped lower than it was - better on both
  counts.

- **The VT replay is roughly a hundred times faster.** Each character was
  rebuilt into a fresh string, which made a single long line quadratic;
  rows are now cells, and a run of ordinary characters is appended in one
  step. Ordinary output went from 0.2 MB/s to about 20 MB/s, so exporting a
  session that `cat`ted something large is seconds rather than minutes. The
  new implementation was checked against the old one over 16,000 renders of
  random terminal input, byte for byte.

### Added

- **Colour could be turned off but never on.** `SECTAPE_COLOR` recognised
  `0`/`never`/`off` and nothing else, so `SECTAPE_COLOR=always` was silently
  ignored and `sectape list | less -R` - or a CI log that renders ANSI
  perfectly well - came out grey with no way to ask otherwise. The variable now
  settles it in both directions and, being the explicit tool-specific setting,
  outranks `NO_COLOR` either way. A value it does not recognise leaves the
  terminal check to decide rather than quietly meaning *on*. It is documented
  now too; it never was.

- **Three settings had no environment variable, though the README promised one
  for every value.** `max_output_lines`, `max_output_chars` and
  `redact_replacement` could only be set in a config file, so a CI job or a
  container had no way to reach them. `SECTAPE_MAX_OUTPUT_LINES`,
  `SECTAPE_MAX_OUTPUT_CHARS` and `SECTAPE_REDACT_REPLACEMENT` now work like the
  rest, with the same clear error a bad value gets elsewhere: a non-numeric
  limit says which variable it was and what it saw. The test walks `Settings`'
  own fields, so a setting added later without a variable fails it rather than
  quietly making the sentence wrong again.

- Redaction covers the token formats their issuers have since moved to, which
  the patterns had not caught up with: GitHub's fine-grained `github_pat_`
  tokens, and AWS `ASIA` temporary keys - the kind STS and SSO hand out, so
  the kind most people actually have. Also Google, Stripe, npm and PyPI
  tokens, and a password embedded in a URL, where the host is kept and only
  the password goes.

- The README told everyone to `pipx install sectape`, which 404s - the package
  is not on PyPI. It now documents installing from a checkout, which is what
  actually works today. Both documented commands were run against a clean
  virtualenv; `twine check` passes on the built wheel and sdist, so publishing
  is a decision rather than a piece of work.

- When the export cannot be written - a full disk, a read-only output
  directory - the session's last words are no longer a bare `Permission
  denied` as your shell exits. It now says the recording is safe, where it is,
  and the command to retry it with. The pane log was always kept; nothing said
  so at the moment it mattered.

- The markdown export names the machine the session was recorded on. The HTML
  page and the JSON both carried it and the default format did not, which
  matters as soon as you record on more than one box.

- The README described an `ssh` session inside a recording as falling back to
  reading commands off the screen. It does not: the fallback is chosen per
  pane log, and a log with markers never uses it, so the remote session is
  kept as the output of the `ssh` command. Documented as it behaves, with
  tests.

- `list` shows a recording's own name rather than its directory slug, and
  `list --json` carries both. A label with no ASCII in it slugs to a digest,
  which told the reader nothing; the name shown is still accepted by `show`,
  `export` and `rm`.

- `doctor` and `config show` report config keys sectape does not recognise. A
  misspelled `fromat = "json"` was simply not read, leaving the default format
  with nothing to show for it.

### Internal

- The test suite no longer fails for anyone with `SECTAPE_*` exported in their
  own shell — which is to say, for anyone who uses the tool.
- `unittest.main()` sat in the middle of `tests/test_record.py`, so running
  that file directly never reached the bash end-to-end suite.
- `write_json_atomic` and `write_text_atomic` are one helper.
- Recordings made before the tool was renamed have proper coverage: their
  commands, exit codes and durations, a log with no size marker at all, the
  listing count, a pipeline read whole, and a session directory holding one
  pane from either era. Every change in this release touches that path, and
  it had one test.
- Removed the vestigial `backups/` directory. It was created inside every
  state directory on every run and never written to, so each user had an empty
  directory for a feature that does not exist. Existing ones can be deleted.
- The shipped completion scripts are checked: both must parse under `zsh -n` /
  `bash -n`, and both must offer exactly the commands the parser defines, so a
  new command cannot quietly stop being completable.
- No end-to-end test types at a shell that may not be reading yet. A fixed
  sleep after the banner was the cause of every pty flake seen here, including
  two that only failed on CI; the tests now wait for output the typed line
  cannot contain, so the proof can only come from the shell having run the
  command. Several short attempts rather than one long wait, which turned out
  to be faster than the sleeps as well as steadier.
- The pane log's permissions are checked with the umask set to 0, which is
  the case that would catch a laxer mode - on a machine whose umask is already
  022 a wrong mode looks right.
- The privacy claim that a password typed at a hidden prompt never reaches the
  log is checked rather than asserted. It holds - nothing displayed is nothing
  recorded - and the test proves it is not passing vacuously: the password's
  length is echoed, so the shell demonstrably read it, and the answer to a
  *visible* prompt in the same session does appear in the log.
- The failure this project was written for - a terminal left in raw mode after
  a recording - is now actually asserted. The end-to-end tests said so in their
  docstring and checked the escape state, but nothing looked at the line
  discipline, so nothing would have noticed a lost `tcsetattr`. Checked on
  normal exit, on SIGTERM and on SIGHUP, along with the terminal really being
  in raw mode during, so the test cannot pass vacuously.
- The two unclean endings a recording actually meets have end-to-end tests:
  SIGHUP, which is what closing the terminal window sends, and SIGKILL, after
  which nothing is exported at the time and the pane log on disk has to be
  enough to get the work back.
- The documented override precedence has tests: defaults < config file <
  environment < `--state-dir` / `--output-dir` / `--no-redact`, including that
  a `~` in either path flag is expanded.
- The HTML page is pinned as a single self-contained file: nothing fetched
  from the network, and every `localStorage` access guarded. An exported page
  is opened from disk, where the browser refuses storage outright - the
  toggles have to keep working, and only remembering the choice may be lost.
- `ui.py` has tests: no escape sequence may reach a pipe, `NO_COLOR` and
  `SECTAPE_COLOR` are honoured, and glyphs fall back to ASCII off a UTF-8
  terminal.
- Removed section banners left behind by an earlier refactor: they announced a
  webhook receiver, a VT emulator, session state and redaction in modules that
  hold none of those, and documented the marker payload under the tool's
  previous name. The marker wire format now sits with the markers.
- The replayer has property tests over random terminal input: it must never
  raise, must never leak a control character into a document, must be
  deterministic, and must never pad a row past the terminal width.

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
