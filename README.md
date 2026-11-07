# codex-loop

Unattended, continuous session loop for the [codex CLI](https://github.com/openai/codex)
agent, with a live window you can watch and phone alerts only when something
leaves the rails.

Each iteration opens the **full codex TUI** inside a named GNU screen session,
injects the working prompt, and waits until the agent itself closes the
session via a marker file (`ITER_DONE`) taught to it through a prompt suffix.
Then a fresh session starts. You can attach and watch it work in real time
(`screen -x`), and anomalies go to a log file and (optionally) to your phone
via [Signal](https://signal.org/).

Battle-tested: 70+ iterations over several days without intervention, two
research fronts alternating inside a single loop on one GPU box.

## What it solves (learned the hard way)

- **Session-end protocol (`ITER_DONE`)** - the prompt tells the agent: when
  you naturally finish, write a summary and `touch ITER_DONE`. The loop never
  guesses where the end is; the agent says so itself.
- **TUI anti-stall**: codex's TUI is turn-based - the model can finish a turn
  and go idle at the composer without ever touching the marker. A watchdog
  scans the screen every minute; seeing `Worked for ...` without the spinner
  it re-sends "continue" (rate-limited). At >=85% context it stops nudging and
  alerts you instead (likely a forgotten marker).
- **Model-stream recovery**: a dropped connection to the model server leaves
  the TUI frozen on an error banner (`stream disconnected ...`). Detected the
  same way; the loop auto-resumes the session and notifies you.
- **Race-free prompt injection**: text goes in 400-character chunks and Enter
  as a separate `screen stuff` packet; after `INJECT_VERIFY_S` the loop re-checks the screen
  (`Context 0%` + no `Working`) and re-sends Enter if the prompt got stuck.
- **Process-local TUI sessions**: `TUI_EXTRA_ARGS=--no-daemon` keeps Codex's
  shared background server out of the loop, so closing the GNU screen window
  closes that iteration. The flag is never passed to `codex exec`.
- **Backpressure, for real**: `MAX_ITER_MINUTES` (kill runaway sessions),
  stall detection via TUI-log mtime, suspiciously-short-session detection,
  exit-code checks, a screen window vanishing without the marker = alert with
  an stderr tail.
- **Health probes** (`HEALTHCHECK_CMD`/`HEALTHFIX_CMD`): e.g. an SSH tunnel to
  your model server, with one fix attempt and exponential backoff.
- **Second front**: every N-th iteration can run a different prompt (e.g. a
  parallel research track) - sequentially, in the same window, so the two
  fronts never fight over the same files.
- **Hot configuration**: `codexloop.conf` is re-read between iterations;
  tuning prompts and thresholds requires no restart.
- **systemd unit per project**: restarts on failure, starts on boot.

Deliberately *not* done: assuming a live context-window percentage is always
available. Some providers (e.g. vLLM's `responses` API) report usage
cumulatively and only at session end. When Codex does display a percentage,
both `Context N% used` and `N% context left` are understood; otherwise the
marker protocol + a session time cap remain the fallback.

## Requirements

- Linux + systemd (optional; without it run the loop manually),
- GNU screen, python3 (>=3.9),
- codex CLI (tested with 0.167.0) with the project directory trusted
  (`[projects."<dir>"]` in
  `~/.codex/config.toml`) - otherwise the TUI asks for trust and the loop
  stalls; the installer warns about a missing entry,
- optional: signal-cli (for phone alerts), qrencode (terminal QR).

For a Codex CLI older than 0.167.0 that does not support `--no-daemon`, set
`TUI_EXTRA_ARGS=` in `codexloop.conf` (or supply compatible TUI-only flags).

## Safety and privacy

The default configuration passes `--dangerously-bypass-approvals-and-sandbox`
to Codex. This lets the agent execute commands, modify files, and use the
network without interactive approval. Use it only with trusted prompts and
projects, ideally under a dedicated unprivileged account or in a container.
Remove that flag from `CODEX_ARGS` if you want Codex to ask for approval, but
note that unattended iterations may then wait for input.

The generated system service runs as root unless you add a `User=` directive.
For a system-wide installation, create a dedicated user, adjust ownership of
the deployment, and review the generated unit before starting it. Alternatively,
install with `--no-systemd` and run the loop as an unprivileged user.

`HEALTHCHECK_CMD` and `HEALTHFIX_CMD` run through the shell, so treat the config
as executable code. Keep alternate-provider credentials outside the repository
in a chmod-600 file. Logs contain full agent transcripts and may include source
code, paths, prompts, or secrets; runtime logs and state are git-ignored, but
you should still restrict access to the deployment directory and never publish
those files.

## Install on a new machine

```bash
git clone https://github.com/kacper-daftcode/codex-loop && cd codex-loop
bash install.sh /path/to/project --name myproj
# review <project>/codexloop/codexloop.conf (PROMPT, thresholds, FRONT2_*)
systemctl enable --now codexloop-myproj
# OPTIONAL phone alerts (everything works without this; alerts land in
# logs/notify.log only):
bash <project>/codexloop/signal-setup.sh
```

`install.sh --with-signal` runs the pairing as part of the install; by default
the Signal step is skipped and can be done any time later (no loop restart
needed).

The default installer writes `/etc/systemd/system/codexloop-<name>.service` and
therefore needs sufficient privileges. Use `--no-systemd` for an unprivileged,
manual installation. Review the safety notes above before starting either mode.

## Controls

- watch live: `<project>/codexloop/watch.py` or `screen -x codexloop-<name>`
  (detach `Ctrl-a d`; you can type to the agent, but not during the first
  ~minute of a fresh session while the loop is injecting the prompt),
- stop after the current iteration: `touch <project>/codexloop/STOP`,
- kill the session immediately and exit: `touch <project>/codexloop/STOPNOW`,
- service: `systemctl stop codexloop-<name>`.

## What lands on Signal (when something is non-standard)

- iteration ended with exit != 0 / the window vanished without the marker
  (includes an stderr tail),
- session killed after `MAX_ITER_MINUTES`, suspiciously short session,
- TUI idle longer than `STALL_NOTIFY_MIN`, or idle at >=85% context without
  `ITER_DONE`, model-stream error (auto-resumed), healthcheck still failing
  after the fix attempt, the agent asking for a human (`HUMAN_NEEDED`), loop
  startup.

## Agent contract (appended to the prompt when EXTRA_SUFFIX=1)

1. Work normally.
2. At the natural end of the session: write a summary to
   `state/LAST_SUMMARY.txt`, then `touch ITER_DONE` - the loop closes the
   window.
3. Need a human? `echo 'BLOCKED: reason' > HUMAN_NEEDED`.

Tune `NATURAL_END_HINT` (default "about 30% of the context window") and
`WRAP_CTX_PCT` (default 35%) to your model/workflow.

## Optional exec mode and model rotation

Set `EXEC_MODE=1` to run one-shot `codex exec` iterations instead of the TUI.
This is useful on hosts where GNU screen or the TUI is unreliable. Timeouts,
stop files, summaries, health checks, and notifications still apply; interactive
watching and composer re-prompts do not. `EXEC_EXTRA_ARGS` applies only to exec
mode, while `TUI_EXTRA_ARGS` applies only to the TUI.

To compare providers, set `ALT_MODEL_EVERY_N=N` and `ALT_MODEL`. Every N-th
iteration then uses the alternate model in either mode. Optional settings are
`ALT_PROVIDER`, `ALT_MODEL_CONTEXT_WINDOW`, `ALT_REASONING_EFFORT`, and
`ALT_CODEX_BIN`. If `ALT_API_KEY_FILE` points to a chmod-600 KEY=VALUE file, its
entries are added to the alternate iteration's environment. Keep this file
outside the repository.

## Logs

`logs/loop.log` (progress + classifications), `logs/iter-NNNN.screen.log`
(full TUI transcript), `logs/iter-NNNN.last.txt` (agent summary),
`logs/iter-NNNN.stderr.txt`, `logs/notify.log`.

## Migration / updates

A deployment is just a directory inside the project holding the conf and
copies of the scripts. To update: copy the new files over, then
`touch .../STOP` and wait for the current iteration to finish. Once the service
becomes inactive, run `systemctl start codexloop-<name>` to load the new code.
Legacy keys `OPT_EVERY_N` / `OPT_PROMPT` are read as aliases of `FRONT2_EVERY_N`
/ `FRONT2_PROMPT`.

The repository was renamed from `codexloop` to `codex-loop`. Deployment
directories, configuration filenames, environment-variable prefixes, and
systemd service names intentionally keep the `codexloop` spelling for backwards
compatibility.

## Known pitfalls (deployment history)

- `screen stuff` can silently discard an oversized command. Send long prompt
  bodies in 400-character chunks, then Enter separately (and keep
  `INJECT_VERIFY_S` as the final check).
- `Worked for ...` means the turn ended and the TUI sits waiting for a human
  - without the re-prompt watchdog the loop idles for hours.
- Model-stream errors need an explicit resume; plain idleness detection is
  not enough.
- Don't type into the window during the first minute of a session (it would
  collide with prompt injection).

## License

MIT - see [LICENSE](LICENSE).
