#!/usr/bin/env python3
"""codexloop - autonomiczna petla sesji codex TUI w GNU screen.

Dzialanie: kazda iteracja otwiera codex TUI w nazwanej sesji screen,
wstrzykuje prompt i czeka, az agent zamknie sesje markerem ITER_DONE
(instrukcja doklejana do promptu - patrz EXTRA_SUFFIX). Potem okno jest
zamykane, pauza, kolejna swieza sesja. Bez konca.

Podglad na zywo:  screen -x <SCREEN_NAME>   (odpiecie: Ctrl-a d)

Pliki kontrolne w katalogu deploymentu (ten z codexloop.conf):
  ITER_DONE     - agent go tworzy = naturalny koniec sesji, petla zamyka okno
  HUMAN_NEEDED  - agent wpisuje "BLOCKED: powod" = notyfikacja
  STOP          - skoncz biezaca iteracje i wyjdz
  STOPNOW       - ubij biezace okno natychmiast i wyjdz
  signal.number - numer telefonu dla notify.sh (signal-setup.sh)

Konfiguracja: codexloop.conf (KEY=VALUE) w tym samym katalogu; petla
przeladowuje ja miedzy iteracjami (zmiany progow/promptow bez restartu).
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
    "PROMPT": "zapoznaj sie z projektem i kontynuuj",
    "EXTRA_SUFFIX": "1",
    "NATURAL_END_HINT": "ok. 40% kontekstu",
    # Drugi front: co N-ta iteracja jedzie innym promptem. 0 = wylaczony.
    "FRONT2_EVERY_N": "0",
    "FRONT2_PROMPT": "",
    "FRONT2_STATE_FILE": "",               # wzgledem do PROJECT_DIR; brak pliku -> front 1
    "CODEX_BIN": "codex",
    "CODEX_ARGS": "--dangerously-bypass-approvals-and-sandbox",
    "SCREEN_NAME": "",
    # Health-check przed kazda iteracja; puste = zawsze zdrowy.
    # Przyklad (tunel ssh do vLLM):
    #   HEALTHCHECK_CMD=python3 -c "import socket; socket.create_connection(('127.0.0.1',18001),5)"
    #   HEALTHFIX_CMD=systemctl restart kimi-tunnel
    "HEALTHCHECK_CMD": "",
    "HEALTHFIX_CMD": "",
    "MAX_ITER_MINUTES": "480",             # sesja dluzsza -> ubicie + notify
    "STALL_NOTIFY_MIN": "45",              # brak nowego outputu TUI tyle minut -> notify
    "MIN_OK_MINUTES": "20",                # sesja krotsza -> podejrzana -> notify
    "PAUSE_BETWEEN_ITERS_S": "15",
    "FAIL_BACKOFF_CAP_MIN": "30",
    "READY_WAIT_S": "20",
    "INJECT_VERIFY_S": "180",
    "REPROMPT_MIN_S": "120",
    "REPROMPT_MAX": "6",
}

# Aliasy wstecznej kompatybilnosci (deployment sm103a uzywa OPT_*).
ALIASES = {"OPT_EVERY_N": "FRONT2_EVERY_N", "OPT_PROMPT": "FRONT2_PROMPT"}

SUFFIX_TEXT = (
    " [nota automatu: sesja nadzorowana przez petle codexloop. Pracuj jak "
    "zwykle. Gdy dojdziesz do naturalnego konca sesji ({hint}), zapisz krotkie "
    "podsumowanie do {base}/state/LAST_SUMMARY.txt i wykonaj komende: "
    "touch {base}/ITER_DONE - petla sama zamknie okno i wystartuje nowa sesje. "
    "Jesli potrzebujesz decyzji czlowieka, wykonaj: "
    "echo 'BLOCKED: powod' > {base}/HUMAN_NEEDED]"
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
    """Odswieza konfiguracje z pliku miedzy iteracjami (bez restartu uslugi)."""
    CONF.clear()
    CONF.update(load_conf())


def ensure_healthy():
    """True gdy srodowisko odpowiada; przy bledzie probuje HEALTHFIX_CMD raz."""
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

        # Start TUI + wstrzykniecie promptu. Enter idzie OSOBNYM stuff -
        # jeden pakiet "tekst+\r" bywa polykany jak paste bez submita.
        time.sleep(float(CONF["READY_WAIT_S"]))
        screen_cmd("-p", "0", "-X", "stuff", prompt)
        time.sleep(2)
        screen_cmd("-p", "0", "-X", "stuff", "\r")
        injected_at = time.monotonic()
        inject_checked = False

        start = time.monotonic()
        stall_notified = False
        human_notified = False
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

            # Weryfikacja, ze prompt faktycznie ruszyl (deadlock: tekst wisi
            # w composerze bez submita - wtedy status to wciaz "Context 0%").
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
                    log(f"iter {num}: TUI bez reakcji po prompcie - dosylam sam Enter")
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
                log(f"iter {num}: przekroczony MAX_ITER_MINUTES - killing")
                notify(
                    f"{name} iter {num}: sesja trwa ponad "
                    f"{CONF['MAX_ITER_MINUTES']} min - ubilem"
                )
                proc.terminate()
                continue

            if human_f.exists() and not human_notified:
                human_notified = True
                res.human_needed = human_f.read_text(errors="replace").strip()[:200]
                notify(f"{name} iter {num}: agent prosi o czlowieka: {res.human_needed}")

            # Naturalne zakonczenie zglaszane przez agenta.
            if done_f.exists() and not dead:
                if end_grace_deadline is None:
                    end_grace_deadline = now + 20  # daj TUI dopsiac rollout
                    res.done_close = True
                    log(f"iter {num}: ITER_DONE - zamykam okno za 20 s")
                elif now >= end_grace_deadline:
                    proc.terminate()
                continue

            # Stall: brak swiezego outputu TUI.
            if not dead and screen_log.exists() and not stall_notified:
                idle_min = (time.time() - screen_log.stat().st_mtime) / 60
                if idle_min >= float(CONF["STALL_NOTIFY_MIN"]):
                    stall_notified = True
                    res.stalled = True
                    notify(
                        f"{name} iter {num}: brak nowego outputu od {idle_min:.0f} min "
                        f"(uplynelo {elapsed_min:.0f} min) - mozliwy stall"
                    )

            if dead:
                break

            # Watchdog: tura skonczona ("Worked for"), TUI bezczynne, a agent
            # nie zamknal sesji markerem ITER_DONE -> doslij "kontynuuj".
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
                if working:
                    idle_strikes = 0
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
                                f"{name} iter {num}: TUI bezczynne przy {pct}% bez "
                                "ITER_DONE - prawdopodobnie zapomnial markera"
                            )
                        elif pct < 85:
                            reprompts += 1
                            last_reprompt = now
                            idle_strikes = 0
                            cont = (
                                "kontynuuj; gdy sesja dojdzie do naturalnego "
                                f"konca ({CONF['NATURAL_END_HINT']}), zamknij ja "
                                f"protokolem: podsumowanie do state/LAST_SUMMARY.txt + "
                                f"touch {BASE_DIR}/ITER_DONE"
                            )
                            screen_cmd("-p", "0", "-X", "stuff", cont)
                            time.sleep(2)
                            screen_cmd("-p", "0", "-X", "stuff", "\r")
                            log(
                                f"iter {num}: idle na prompcie (ctx {pct}%) - "
                                f"doslano 'kontynuuj' #{reprompts}"
                            )

        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        res.exit_code = proc.returncode

    screen_cmd("-X", "quit")  # porzadek, gdyby sesja screen osierocila okno

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
    headline = text.splitlines()[0][:160] if text else "(brak podsumowania)"
    flagi = []
    if res.done_close:
        flagi.append("DONE")
    if res.killed_stopnow:
        flagi.append("STOPNOW")
    if res.killed_long_run:
        flagi.append("MAX_ITER_MINUTES")
    if res.screen_died:
        flagi.append("screen-gone-bez-DONE")
    log(
        f"iter {num}: end exit={res.exit_code} dur={res.duration_min:.0f}min "
        f"{'/'.join(flagi) or 'ok'} | {headline}"
    )
    problems = []
    if res.killed_stopnow:
        problems.append("sesja przerwana przez STOPNOW")
    if res.killed_long_run:
        problems.append("sesja ubita po przekroczeniu MAX_ITER_MINUTES")
    if res.screen_died:
        problems.append(
            f"okno screen zniknelo bez markera ITER_DONE (exit {res.exit_code}) - "
            "TUI padlo albo sesja skonczyla sie bez zamkniecia"
        )
        err_path = LOG_DIR / f"iter-{num:04d}.stderr.txt"
        if err_path.exists():
            tail = err_path.read_text(errors="replace")[-300:].replace("\n", " | ")
            if tail.strip():
                problems.append(f"stderr: {tail[:250]}")
    if res.duration_min < float(CONF["MIN_OK_MINUTES"]) and not (
        res.killed_stopnow or res.killed_long_run
    ):
        problems.append(f"sesja podejrzanie krotka ({res.duration_min:.0f} min)")
    if res.human_needed:
        problems.append(f"agent prosil o czlowieka: {res.human_needed}")
    if problems:
        notify(f"{name} iter {num} NIESTANDARDOWA: " + " ;; ".join(problems))
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
    notify(f"{name}: petla wystartowala (kolejna iteracja: {num})")

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
                f"{name}: healthcheck dalej FAIL po probie naprawy; "
                f"ponawiam za {wait:.0f} min"
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
