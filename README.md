# codexloop

> **EN**: Unattended, continuous session loop for the [codex CLI](https://github.com/openai/codex)
> agent. Each iteration opens the full codex TUI inside a named GNU screen
> session, injects the working prompt, and waits until the agent itself closes
> the session via a marker file (`ITER_DONE`) taught to it through a prompt
> suffix. Then a fresh session starts. You can watch live (`screen -x`), all
> anomalies are reported to a log and (optionally) to your phone via Signal.
> Docs below are Polish-first.

---

# codexloop - autonomiczna petla sesji codex (TUI w screen, notyfikacje Signal)

Nieinterwencyjne ciagle sesje [codex CLI](https://github.com/openai/codex):
kazda iteracja otwiera **pelne TUI codexa** w nazwanej sesji GNU screen,
wstrzykuje prompt i czeka, az agent sam zamknie sesje (marker). Potem pauza i
kolejna swieza sesja. Do uklonu: mozesz **podejrzec na zywo** (`screen -x`),
a o sytuacjach niestandardowych poinformuje Cie **Signal**.

Bojowo przetestowane: 60+ iteracji / kilka dni bez interwencji, dwa rownolegle
fronty badawcze na zmiane w jednej petli.

## Cechy (i jakie problemy zalatwiaja)

- **Marker konca pracy (ITER_DONE)** - prompt agenta dostaje doklejona
  instrukcje: gdy dojdziesz do naturalnego konca, zapisz podsumowanie i
  `touch ITER_DONE`. Petla nie zgaduje, gdzie koniec; agent mowi sam.
- **Anti-stall TUI**: prompt w TUI to turn-based - model moze skonczyc ture
  i "przysnac" na prompcie bez markera. Watchdog co minute skanuje hardcopy;
  widzi `Worked for ...` bez spinnera -> dosyla "kontynuuj" (limitowane).
  Przy >=85% kontekstu nie dosyla, tylko alarmuje (zapomniany marker).
- **Wstrzykniecie promptu bez race'ow**: tekst i Enter osobnymi `screen stuff`;
  po INJECT_VERIFY_S weryfikacja hardcopy (`Context 0%` + brak `Working`) i
  ponowienie Entera, jesli prompt utknal w composerze.
- **Sondy zdrowia srodowiska** (HEALTHCHECK_CMD/HEALTHFIX_CMD): np. tunel
  SSH do serwera modeli, z proba naprawy i backoffem.
- **Backpressure jaki jest**: MAX_ITER_MINUTES (ubicie sesji-maratonu),
  stall-detekcja po mtime logu TUI, min-duration detection, exit codes,
  znikniecie okna screen bez markera = alarm z ogonkiem stderr.
- **Notyfikacje**: zawsze `logs/notify.log`; po sparowaniu - Signal
  (Note to Self) przez signal-cli.
- **Drugi front**: co N-ta iteracja moze isc innym promptem (np. rownolegly
  projekt badawczy; sekwencyjnie w tym samym oknie - bez konfliktow zapisow).
- **Konfiguracja na goraco**: codexloop.conf przeladowywany miedzy iteracjami.
- **systemd**: usluga per projekt, Restart=always, start po reboucie.

Czego swiadomie NIE robi: nie mierzy procentu kontekstu (niektorzy providerzy,
np. vLLM/responses, raportuja usage kumulatywnie i dopiero na koncu sesji -
nie da sie tego sledzic na zywo). Zamiast tego: protokol ITER_DONE + limit
czasu sesji.

## Wymagania

- Linux + systemd (dla demona; bez systemd dziala recznie),
- GNU screen, python3 (>=3.9),
- codex CLI zaufany dla katalogu projektu (`[projects."<dir>"]` w
  ~/.codex/config.toml) - inaczej TUI zapyta o trust i petla stanie,
- opcjonalnie: signal-cli (notyfikacje), qrencode (QR w terminalu).

## Instalacja na nowej maszynie

```bash
scp codexloop.tar.gz nowy-host:  &&  ssh nowy-host
tar xzf codexloop.tar.gz && cd codexloop
bash install.sh /sciezka/do/projektu --name mojproj
# edytuj <projekt>/codexloop/codexloop.conf (PROMPT, progi, FRONT2_*)
systemctl enable --now codexloop-mojproj
# OPCJONALNIE powiadomienia na Signal (bez tego wszystko dziala, alarmy
# laduja tylko w logs/notify.log):
bash <projekt>/codexloop/signal-setup.sh
```

`install.sh --with-signal` od razu przeprowadzi parowanie; domyslnie krok jest
pominety i mozna go wykonac w dowolnej chwili pozniej (bez restartu petli).

## Sterowanie

- podglad na zywo: `<projekt>/codexloop/watch.py` lub `screen -x codexloop-<name>`
  (odpiecie `Ctrl-a d`; mozna pisac do agenta, ale nie w pierwszej ~1 min
  nowej sesji, gdy petla wstrzykuje prompt),
- stop po biezacej iteracji: `touch <projekt>/codexloop/STOP`,
- natychmiastowa smierc sesji + wyjscie: `touch <projekt>/codexloop/STOPNOW`,
- usluga: `systemctl stop codexloop-<name>`.

## Co przychodzi na Signal (kiedy cos jest niestandardowe)

- iteracja z exit != 0, okno znikniete bez markera (z ogonkiem stderr),
- sesja ubita po MAX_ITER_MINUTES, sesja podejrzanie krotka,
- TUI bezczynne dluzej niz STALL_NOTIFY_MIN albo przy >=85% kontekstu bez
  ITER_DONE, healthcheck FAIL mimo naprawy, agent prosil o czlowieka
  (`HUMAN_NEEDED`), start petli.

## Kontrakt z agentem (co dostaje w prompcie przy EXTRA_SUFFIX=1)

1. Pracuj normalnie.
2. Na naturalnym koncu sesji: podsumowanie do `state/LAST_SUMMARY.txt`,
   potem `touch ITER_DONE` - petla zamknie okno.
3. Potrzeba czlowieka: `echo 'BLOCKED: powod' > HUMAN_NEEDED`.

`NATURAL_END_HINT` (domyslnie "ok. 40% kontekstu") dopasuj do modelu/projektu.

## Logi

`logs/loop.log` (przebieg i klasyfikacje), `logs/iter-NNNN.screen.log`
(pelny zrzut TUI), `logs/iter-NNNN.last.txt` (podsumowanie agenta),
`logs/iter-NNNN.stderr.txt`, `logs/notify.log`.

## Migracja / aktualizacja

Deployment = katalog w projekcie z conf + kopia skryptow. Aktualizacja:
skopiuj nowe `codexloop.py` i reszte do katalogu deploymentu, potem
`touch .../STOP` (restart nastapi po naturalnym koncu biezacej iteracji;
systemd podniesie nowy kod). Stare klucze `OPT_EVERY_N`/`OPT_PROMPT` czytane
sa jako aliasy `FRONT2_EVERY_N`/`FRONT2_PROMPT`.

## Znane pulapki (historia wdrozenia)

- Enter po dlugiej tresci promptu musi isc osobnym `stuff` - inaczej bywa
  potraktowany jak wklejenie bez submita (stad INJECT_VERIFY_S).
- "Worked for" = tura skonczona i TUI czeka na czlowieka - bez watchdogu
  petla stoi w miejscu godzinami (stad reprompty).
- Nie wstrzykuj nic w pierwszej minucie nowej sesji (kolizja z injekcja).
