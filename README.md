# codexloop

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
- **Race-free prompt injection**: text and Enter go as separate `screen stuff`
  packets; after `INJECT_VERIFY_S` the loop re-checks the screen
  (`Context 0%` + no `Working`) and re-sends Enter if the prompt got stuck.
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
- **systemd unit per project**: `Restart=always`, starts on boot.

Deliberately *not* done: tracking context-window percentage. Some providers
(e.g. vLLM's `responses` API) report usage cumulatively and only at session
end - it cannot be monitored live. Hence the marker protocol + a session time
cap instead.

## Requirements

- Linux + systemd (optional; without it run the loop manually),
- GNU screen, python3 (>=3.9),
- codex CLI with the project directory trusted (`[projects."<dir>"]` in
  `~/.codex/config.toml`) - otherwise the TUI asks for trust and the loop
  stalls; the installer warns about a missing entry,
- optional: signal-cli (for phone alerts), qrencode (terminal QR).

## Install on a new machine

```bash
git clone https://github.com/kacper-daftcode/codexloop && cd codexloop
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

Tune `NATURAL_END_HINT` (default "about 40% of the context window") to your
model/workflow.

## Logs

`logs/loop.log` (progress + classifications), `logs/iter-NNNN.screen.log`
(full TUI transcript), `logs/iter-NNNN.last.txt` (agent summary),
`logs/iter-NNNN.stderr.txt`, `logs/notify.log`.

## Migration / updates

A deployment is just a directory inside the project holding the conf and
copies of the scripts. To update: copy the new files over, then
`touch .../STOP` (the restart happens after the current iteration ends
naturally; systemd picks the new code up). Legacy keys `OPT_EVERY_N` /
`OPT_PROMPT` are read as aliases of `FRONT2_EVERY_N` / `FRONT2_PROMPT`.

## Known pitfalls (deployment history)

- After a long prompt body, Enter must go as a separate `stuff` - otherwise
  it can be treated as a paste without actually submitting (hence
  `INJECT_VERIFY_S`).
- `Worked for ...` means the turn ended and the TUI sits waiting for a human
  - without the re-prompt watchdog the loop idles for hours.
- Model-stream errors need an explicit resume; plain idleness detection is
  not enough.
- Don't type into the window during the first minute of a session (it would
  collide with prompt injection).

## License

MIT - see [LICENSE](LICENSE).
