#!/usr/bin/env python3
"""codexloop - unattended, continuous codex TUI sessions in GNU screen.

Each iteration opens the full codex TUI in a named screen session, injects
the working prompt and waits until the agent itself closes the session via
the ITER_DONE marker (contract appended to the prompt, see EXTRA_SUFFIX).
Then the window is closed and a fresh session starts. Forever.

Watch live:  screen -x <SCREEN_NAME>   (detach: Ctrl-a d)

Control files in the deployment directory (the one holding codexloop.conf):
  ITER_DONE     - agent creates it = natural session end, loop closes window
  HUMAN_NEEDED  - agent writes "BLOCKED: reason" here = notification goes out
  STOP          - finish current iteration, then exit
  STOPNOW       - terminate the current window immediately, then exit
  signal.number - phone number used by notify.sh (see signal-setup.sh)

Configuration: codexloop.conf (KEY=VALUE) in the same directory; the loop
reloads it between iterations (threshold/prompt changes need no restart).
"""

import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("CODEXLOOP_HOME") or Path(__file__).resolve().parent)
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
CONF_FILE = BASE_DIR / "codexloop.conf"

DEFAULTS = {
    "NAME": BASE_DIR.name,
    "PROJECT_DIR": "",
    "PROMPT": "familiarize yourself with the project and continue",
    "EXTRA_SUFFIX": "1",
    "NATURAL_END_HINT": "about 40% of the context window",
    # Second front: every N-th iteration runs FRONT2_PROMPT. 0 = disabled.
    "FRONT2_EVERY_N": "0",
    "FRONT2_PROMPT": "",
    "FRONT2_STATE_FILE": "",               # relative to PROJECT_DIR; missing file -> main front
    "CODEX_BIN": "codex",
    "CODEX_ARGS": "--dangerously-bypass-approvals-and-sandbox",
    "SCREEN_NAME": "",
    # Health check before each iteration; empty = always healthy.
    # Example (ssh tunnel to a vLLM server):
    #   HEALTHCHECK_CMD=python3 -c "import socket; socket.create_connection(('127.0.0.1',18001),5)"
    #   HEALTHFIX_CMD=systemctl restart my-tunnel
    "HEALTHCHECK_CMD": "",
    "HEALTHFIX_CMD": "",
    "MAX_ITER_MINUTES": "480",             # longer session -> kill + notify
    "STALL_NOTIFY_MIN": "45",              # no new TUI output for this long -> notify
    "MIN_OK_MINUTES": "20",                # shorter session -> suspicious -> notify
    "PAUSE_BETWEEN_ITERS_S": "15",
    "FAIL_BACKOFF_CAP_MIN": "30",
    "READY_WAIT_S": "20",
    "INJECT_VERIFY_S": "180",
    "REPROMPT_MIN_S": "120",
    "REPROMPT_MAX": "6",
}

# Backward-compat aliases (older deployments used OPT_*).
ALIASES = {"OPT_EVERY_N": "FRONT2_EVERY_N", "OPT_PROMPT": "FRONT2_PROMPT"}

SUFFIX_TEXT = (
    " [automation note: this session is supervised by the codexloop harness. "
    "Work as usual. When you reach the natural end of the session ({hint}), "
    "write a short summary to {base}/state/LAST_SUMMARY.txt and run: "
    "touch {base}/ITER_DONE - the loop will close the window and start a new "
    "session. If you need a human decision, run: "
    "echo 'BLOCKED: reason' > {base}/HUMAN_NEEDED]"
)


def load_conf():
    def parse_val(val):
        val = val.strip()
        if val.startswith(("'", '"')):
            try:
                parts = shlex.split(val)
                return parts[0] if parts else ""
            except ValueError:
                return val
        if " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        return val

    conf = dict(DEFAULTS)
    if CONF_FILE.exists():
        for line in CONF_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            key = ALIASES.get(key, key)
            conf[key] = parse_val(val)
    for key in list(conf):
        env_key = "CODEXLOOP_" + key
        if os.environ.get(env_key):
            conf[key] = os.environ[env_key]
    return conf


CONF = load_conf()


def conf_resolved():
    name = CONF["NAME"] or BASE_DIR.name
    screen = CONF["SCREEN_NAME"] or f"codexloop-{name}"
    project = CONF["PROJECT_DIR"] or str(BASE_DIR.parent)
    return name, screen, project


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{now_iso()} {msg}"
    with open(LOG_DIR / "loop.log", "a") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def notify(msg):
    """Fire-and-forget notification; never raises."""
    name, _, _ = conf_resolved()
    try:
        subprocess.run(
            [str(BASE_DIR / "notify.sh"), msg],
            timeout=60,
            capture_output=True,
            env={**os.environ, "CODEXLOOP_NOTIFIER_NAME": name},
        )
    except Exception as exc:  # noqa: BLE001
        log(f"notify.sh failed: {exc}")


def next_iter_num():
    nums = []
    for p in list(LOG_DIR.glob("iter-*.screen.log")) + list(LOG_DIR.glob("iter-*.jsonl")):
        try:
            nums.append(int(p.name.split("-")[1].split(".")[0]))
        except (IndexError, ValueError):
            continue
    return max(nums) + 1 if nums else 1


def stop_mode():
    if (BASE_DIR / "STOPNOW").exists():
        return "now"
    if (BASE_DIR / "STOP").exists():
        return "graceful"
    return None


def iter_front(num):
    try:
        n = int(CONF.get("FRONT2_EVERY_N") or 0)
    except ValueError:
        n = 0
    if n <= 0 or not CONF.get("FRONT2_PROMPT"):
        return "main"
    state = CONF.get("FRONT2_STATE_FILE", "")
    if state:
        _, _, project = conf_resolved()
        sp = Path(state)
        if not sp.is_absolute():
            sp = Path(project) / sp
        if not sp.exists():
            return "main"
    return "front2" if num % n == 0 else "main"


def reload_conf():
    """Re-read config between iterations (no service restart needed)."""
    CONF.clear()
    CONF.update(load_conf())


def ensure_healthy():
    """True when the environment answers; on failure tries HEALTHFIX_CMD once."""
    check = CONF.get("HEALTHCHECK_CMD", "")
    if not check.strip():
        return True

    def ok():
        try:
            return subprocess.run(
                check, shell=True, timeout=45, capture_output=True
            ).returncode == 0
        except Exception:
            return False

    if ok():
        return True
    log("healthcheck FAIL - running HEALTHFIX_CMD")
    fix = CONF.get("HEALTHFIX_CMD", "")
    if fix.strip():
        try:
            subprocess.run(fix, shell=True, timeout=120, capture_output=True)
        except Exception as exc:  # noqa: BLE001
            log(f"healthfix failed: {exc}")
        time.sleep(8)
    good = ok()
    log(f"health after fix: {'OK' if good else 'STILL DOWN'}")
    return good


def screen_cmd(*args):
    _, screen, _ = conf_resolved()
    return subprocess.run(["screen", "-S", screen, *args], capture_output=True)


class IterResult:
    def __init__(self):
        self.exit_code = None
        self.done_close = False
        self.killed_long_run = False
        self.killed_stopnow = False
        self.screen_died = False
        self.stalled = False
        self.human_needed = ""
        self.last_message = ""
        self.duration_min = 0.0


def run_iteration(num):
    name, screen, project = conf_resolved()
    res = IterResult()
    screen_log = LOG_DIR / f"iter-{num:04d}.screen.log"
    last_path = LOG_DIR / f"iter-{num:04d}.last.txt"
    err_path = LOG_DIR / f"iter-{num:04d}.stderr.txt"
    done_f = BASE_DIR / "ITER_DONE"
    human_f = BASE_DIR / "HUMAN_NEEDED"
    summary_f = STATE_DIR / "LAST_SUMMARY.txt"
    for f in (done_f, human_f, summary_f):
        f.unlink(missing_ok=True)
    STATE_DIR.mkdir(exist_ok=True)

    front = iter_front(num)
    prompt = CONF["FRONT2_PROMPT"] if front == "front2" else CONF["PROMPT"]
    if CONF["EXTRA_SUFFIX"] == "1":
        prompt += SUFFIX_TEXT.format(base=BASE_DIR, hint=CONF["NATURAL_END_HINT"])

    inner = f"cd {shlex.quote(project)} && exec {CONF['CODEX_BIN']} {CONF['CODEX_ARGS']}"
    cmd = [
        "screen", "-DmS", screen,
        "-L", "-Logfile", str(screen_log),
        "bash", "-lc", inner,
    ]
    log(f"iter {num}: start [front={front}] (screen -x {screen})")
    with open(err_path, "a") as errf:
        proc = subprocess.Popen(cmd, stdout=errf, stderr=errf)

        # TUI startup + prompt injection. Enter goes in a SEPARATE stuff -
        # a single "text+\r" packet can be swallowed like a paste w/o submit.
        time.sleep(float(CONF["READY_WAIT_S"]))
        screen_cmd("-p", "0", "-X", "stuff", prompt)
        time.sleep(2)
        screen_cmd("-p", "0", "-X", "stuff", "\r")
        injected_at = time.monotonic()
        inject_checked = False

        start = time.monotonic()
        stall_notified = False
        human_notified = False
        stream_err_notified = False
        end_grace_deadline = None
        last_hc_scan = 0.0
        idle_strikes = 0
        reprompts = 0
        last_reprompt = 0.0
        high_ctx_notified = False

        while True:
            time.sleep(5)
            now = time.monotonic()
            elapsed_min = (now - start) / 60
            dead = proc.poll() is not None

            # Verify the prompt actually started (deadlock: text sits in the
            # composer un-submitted - status still says "Context 0%").
            if not inject_checked and now - injected_at >= float(CONF["INJECT_VERIFY_S"]):
                inject_checked = True
                stuck = False
                hc = STATE_DIR / "hc.txt"
                try:
                    screen_cmd("-p", "0", "-X", "hardcopy", str(hc))
                    time.sleep(1)
                    txt = hc.read_text(errors="replace")
                    stuck = ("Context 0% used" in txt) and ("Working" not in txt)
                except OSError:
                    pass
                if stuck and not dead:
                    log(f"iter {num}: TUI did not start the prompt - resending Enter")
                    screen_cmd("-p", "0", "-X", "stuff", "\r")

            if stop_mode() == "now" and not dead:
                log(f"iter {num}: STOPNOW - terminating screen")
                res.killed_stopnow = True
                proc.terminate()
                continue

            if not res.killed_long_run and not dead and (
                elapsed_min >= float(CONF["MAX_ITER_MINUTES"])
            ):
                res.killed_long_run = True
                log(f"iter {num}: MAX_ITER_MINUTES exceeded - killing")
                notify(
                    f"{name} iter {num}: session ran over "
                    f"{CONF['MAX_ITER_MINUTES']} min - killed"
                )
                proc.terminate()
                continue

            if human_f.exists() and not human_notified:
                human_notified = True
                res.human_needed = human_f.read_text(errors="replace").strip()[:200]
                notify(f"{name} iter {num}: agent asks for a human: {res.human_needed}")

            # Natural end reported by the agent.
            if done_f.exists() and not dead:
                if end_grace_deadline is None:
                    end_grace_deadline = now + 20  # let the TUI flush its rollout
                    res.done_close = True
                    log(f"iter {num}: ITER_DONE - closing the window in 20 s")
                elif now >= end_grace_deadline:
                    proc.terminate()
                continue

            # Stall: no fresh TUI output.
            if not dead and screen_log.exists() and not stall_notified:
                idle_min = (time.time() - screen_log.stat().st_mtime) / 60
                if idle_min >= float(CONF["STALL_NOTIFY_MIN"]):
                    stall_notified = True
                    res.stalled = True
                    notify(
                        f"{name} iter {num}: no new output for {idle_min:.0f} min "
                        f"(elapsed {elapsed_min:.0f} min) - possible stall"
                    )

            if dead:
                break

            # Watchdog: TUI sat idle at the prompt after a finished turn
            # ("Worked for ...") w/o ITER_DONE, or a model-stream error froze
            # the turn - in both cases the fix is to send "continue".
            if not dead and inject_checked and now - last_hc_scan >= 60:
                last_hc_scan = now
                hc = STATE_DIR / "hc.txt"
                screen_cmd("-p", "0", "-X", "hardcopy", str(hc))
                try:
                    txt = hc.read_text(errors="replace")
                except OSError:
                    txt = ""
                working = "Working (" in txt
                idle_prompt = ("Worked for" in txt) and not working
                errored = not working and any(
                    p in txt
                    for p in (
                        "stream disconnected",
                        "error sending request",
                        "stream error:",
                    )
                )
                if working:
                    idle_strikes = 0
                elif errored and stop_mode() is None:
                    if not stream_err_notified:
                        stream_err_notified = True
                        notify(
                            f"{name} iter {num}: model stream error - resuming session"
                        )
                    if reprompts < int(CONF["REPROMPT_MAX"]) and (
                        now - last_reprompt >= float(CONF["REPROMPT_MIN_S"])
                    ):
                        reprompts += 1
                        last_reprompt = now
                        idle_strikes = 0
                        cont = (
                            "the session was interrupted by a model stream error; "
                            "continue from where you stopped (session-end protocol "
                            "unchanged)"
                        )
                        screen_cmd("-p", "0", "-X", "stuff", cont)
                        time.sleep(2)
                        screen_cmd("-p", "0", "-X", "stuff", "\r")
                        log(f"iter {num}: stream-error - resumed #{reprompts}")
                elif idle_prompt and stop_mode() is None:
                    idle_strikes += 1
                    cm = re.search(r"Context (\d+)% used", txt)
                    pct = int(cm.group(1)) if cm else 0
                    if idle_strikes >= 2 and reprompts < int(CONF["REPROMPT_MAX"]) and (
                        now - last_reprompt >= float(CONF["REPROMPT_MIN_S"])
                    ):
                        if pct >= 85 and not high_ctx_notified:
                            high_ctx_notified = True
                            notify(
                                f"{name} iter {num}: TUI idle at {pct}% without "
                                "ITER_DONE - agent probably forgot the marker"
                            )
                        elif pct < 85:
                            reprompts += 1
                            last_reprompt = now
                            idle_strikes = 0
                            cont = (
                                "continue; when the session reaches its natural "
                                f"end ({CONF['NATURAL_END_HINT']}), close it with "
                                "the protocol: summary to state/LAST_SUMMARY.txt + "
                                f"touch {BASE_DIR}/ITER_DONE"
                            )
                            screen_cmd("-p", "0", "-X", "stuff", cont)
                            time.sleep(2)
                            screen_cmd("-p", "0", "-X", "stuff", "\r")
                            log(
                                f"iter {num}: idle at prompt (ctx {pct}%) - "
                                f"sent 'continue' #{reprompts}"
                            )

        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        res.exit_code = proc.returncode

    screen_cmd("-X", "quit")  # hygiene, in case the screen window got orphaned

    res.duration_min = (time.monotonic() - start) / 60
    if summary_f.exists():
        res.last_message = summary_f.read_text(errors="replace")[-4000:]
        last_path.write_text(res.last_message)
    if not res.done_close and not (res.killed_stopnow or res.killed_long_run):
        res.screen_died = True
    done_f.unlink(missing_ok=True)
    human_f.unlink(missing_ok=True)
    return res


def classify_and_report(num, res):
    name, _, _ = conf_resolved()
    text = res.last_message.strip()
    headline = text.splitlines()[0][:160] if text else "(no summary)"
    flags = []
    if res.done_close:
        flags.append("DONE")
    if res.killed_stopnow:
        flags.append("STOPNOW")
    if res.killed_long_run:
        flags.append("MAX_ITER_MINUTES")
    if res.screen_died:
        flags.append("screen-gone-no-DONE")
    log(
        f"iter {num}: end exit={res.exit_code} dur={res.duration_min:.0f}min "
        f"{'/'.join(flags) or 'ok'} | {headline}"
    )
    problems = []
    if res.killed_stopnow:
        problems.append("session interrupted by STOPNOW")
    if res.killed_long_run:
        problems.append("session killed after exceeding MAX_ITER_MINUTES")
    if res.screen_died:
        problems.append(
            f"screen window vanished without the ITER_DONE marker "
            f"(exit {res.exit_code}) - TUI crashed or session ended without closing"
        )
        err_path = LOG_DIR / f"iter-{num:04d}.stderr.txt"
        if err_path.exists():
            tail = err_path.read_text(errors="replace")[-300:].replace("\n", " | ")
            if tail.strip():
                problems.append(f"stderr: {tail[:250]}")
    if res.duration_min < float(CONF["MIN_OK_MINUTES"]) and not (
        res.killed_stopnow or res.killed_long_run
    ):
        problems.append(f"suspiciously short session ({res.duration_min:.0f} min)")
    if res.human_needed:
        problems.append(f"agent asked for a human: {res.human_needed}")
    if problems:
        notify(f"{name} iter {num} NON-STANDARD: " + " ;; ".join(problems))
        return False
    return True


def main():
    import fcntl

    lock_fh = open(BASE_DIR / "codexloop.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("codexloop already running", file=sys.stderr)
        return 1

    name, _, _ = conf_resolved()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    LOG_DIR.mkdir(exist_ok=True)
    num = next_iter_num()
    log(f"codexloop start, next iter = {num}, pid={os.getpid()}")
    notify(f"{name}: loop started (next iteration: {num})")

    consec_fail = 0
    while True:
        reload_conf()
        mode = stop_mode()
        if mode:
            log(f"stop file present ({mode}) - exiting")
            for f in ("STOP", "STOPNOW"):
                p = BASE_DIR / f
                if p.exists():
                    p.unlink()
            return 0

        if not ensure_healthy():
            consec_fail += 1
            wait = min(5 * consec_fail, float(CONF["FAIL_BACKOFF_CAP_MIN"]))
            notify(
                f"{name}: healthcheck still FAIL after the fix attempt; "
                f"retrying in {wait:.0f} min"
            )
            time.sleep(wait * 60)
            continue

        res = run_iteration(num)
        classify_and_report(num, res)
        ok_exit = (res.done_close or res.exit_code in (0, None)) and not res.screen_died
        consec_fail = 0 if ok_exit else consec_fail + 1
        if consec_fail > 0:
            wait = min(5 * consec_fail, float(CONF["FAIL_BACKOFF_CAP_MIN"]))
            log(f"iter {num}: failure #{consec_fail}; backoff {wait:.0f} min")
            time.sleep(wait * 60)
        else:
            time.sleep(float(CONF["PAUSE_BETWEEN_ITERS_S"]))
        num += 1


if __name__ == "__main__":
    sys.exit(main())
