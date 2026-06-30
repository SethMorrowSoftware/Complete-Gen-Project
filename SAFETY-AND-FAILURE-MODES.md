# Safety & Failure‑Mode Analysis — GenWatch + ATS‑Pi

**Purpose:** demonstrate that the monitoring/control system is safe and reliable on real
generator/transfer‑switch hardware. Every behaviour below is implemented in code (file references
given) and is verifiable on the bench.

---

## 1. The one principle that makes this safe

> **The ASCO transfer switch keeps doing its own job no matter what our system does.**

The ATS has its own internal automatic‑transfer logic and its own engine‑start to the generator.
Our system (GenWatch + ATS‑Pi + ADAM) **observes** that switch and can **optionally command** a
transfer — but it is **never in the switch's safety path.** If every box we add loses power, network,
or crashes, the transfer switch still transfers automatically on a real outage exactly as it did
before we installed anything. The worst case from any failure of our equipment is **loss of remote
visibility/control — never an unsafe transfer and never a load stranded on the generator.**

Four supporting design rules, all enforced in code:

| Rule | Meaning | Where |
|---|---|---|
| **Fail‑safe direction** | every command path de‑energizes to "utility / released" | `io_adam.py`, wiring |
| **Defense in depth** | a command must pass *several* independent gates to fire | `control.py`, `state.py`, `safety.py` |
| **Observe, don't disturb** | reads are isolated; a reader can't hold a command alive | `server.py` (ConnectionTracker, C‑1) |
| **Self‑release on doubt** | any loss of the commanding link releases maintained commands | `safety.py` (ICD §8.3) |

---

## 2. Failure‑mode table (the core of this document)

"Maintained command" = a held Force‑Transfer or Inhibit. "Released" = the relay de‑energizes, which
on this wiring means **the load returns to / stays on utility** and the switch resumes its own
automatic control.

| # | Failure | What the system does | Effect on the load / ATS | Safe? |
|---|---|---|---|---|
| 1 | **Network lost: GenWatch ↔ ATS‑Pi** | ATS‑Pi sees the commanding TCP connection drop and **releases maintained commands on the next 1 s tick** (doesn't even wait the full 30 s). `safety.py:note_commander_lost`. | Any held transfer releases → **utility**; switch resumes automatic control. | ✅ |
| 2 | **Network silent (no clean drop), GenWatch ↔ ATS‑Pi** | No successful read for **30 s** → ATS‑Pi auto‑releases maintained commands and retries every 1 s until the relay physically drops. `safety.py` `TIMEOUT_S=30`. | Held transfer releases → **utility**. | ✅ |
| 3 | **Network lost: ATS‑Pi ↔ ADAM** | Modbus reads fail → driver marks disconnected; position reads stop updating and the served state degrades; commands can't be driven. | Relays hold their **last electrical state** until power/up‑link returns; a maintained command is still released by #1/#2/#6 fail‑safes. ATS keeps its own control. | ✅ |
| 4 | **Network lost: GenWatch ↔ H‑100 gateway** | GenWatch shows **Comms LOST**, keeps running, reconnects in the background; **never invents telemetry**. Remote H‑100 control (if used) is blocked while comms aren't healthy. `control.py` comms gate. | None — H‑100 is monitored only; the generator runs on its own controller. | ✅ |
| 5 | **Power lost: ATS‑Pi** | Process dies; all ADAM relays it drove **de‑energize**. | Relays open → **utility**; manual toggle still works; ATS automatic control unaffected. | ✅ |
| 6 | **Power lost: ADAM‑6060** | All relay contacts open (no coil power). | Every command line opens → **utility**. | ✅ |
| 7 | **Power lost: GenWatch server** | Monitoring/UI stops. | None — GenWatch isn't in the ATS or generator control path here. | ✅ |
| 8 | **ATS‑Pi process crash/hang** | systemd **restarts it** (`Restart=always`, 5 s). On hang, the systemd **software watchdog** (`WatchdogSec=60`) kills+restarts it. On startup it **resets all command outputs to released** (ICD §9.3, `__main__.py`). | Brief gap; relays released on restart → **utility**. | ✅ |
| 9 | **Pi OS / kernel hang (ATS‑Pi)** | The **hardware watchdog** (`RuntimeWatchdogSec=15s`) resets the whole Pi if userspace stops petting it. | Pi reboots (~seconds); on the way down/up relays de‑energize → **utility**. | ✅ |
| 10 | **Pi loses power *with a transfer latched* (the hard case)** | Software can't run, so the **ADAM's own host‑idle watchdog** (configured in the ADAM, timeout 5–10 s, all DO safety‑values = OFF) drops the relays independently of the Pi. This is the **F1 fail‑safe**, proven by the cable‑pull test. | Relays drop → **utility**, with no Pi involved. | ✅ (must be bench‑verified — see §5) |
| 11 | **ADAM unplugged / fails** | Reads/writes error; companion marks disconnected. | Relay contacts open → **utility**; ATS automatic control unaffected. | ✅ |
| 12 | **GenWatch process crash/hang** | systemd restarts it; software + hardware watchdogs as in #8/#9. | None on the ATS (a dropped GenWatch link triggers #1's release). | ✅ |
| 13 | **Operator sends a bad/stale command** | Must pass a **two‑step confirm token** (single‑use, 30 s TTL, bound to the operator *and* the action). A stale browser tab's token is rejected. `control.py`. | Command can't fire from a stale/replayed click. | ✅ |
| 14 | **Command sent in the wrong mode** | The **mode gate** rejects it: in `manual` only *Inhibit* is allowed; Test/Force‑Transfer/Bypass are refused; in `test`/`unknown` all are refused. `state.py` `_ALLOWED_MODES_FOR_ADDR`. | Unsafe command rejected; a `FAULT_INPUT` is surfaced. | ✅ |
| 15 | **H‑100 remote command with the panel not in AUTO** | GenWatch refuses the write server‑side (the H‑100 would silently ignore it). `control.py` panel‑AUTO gate. | No "phantom" command; operator told to set the panel. | ✅ |
| 16 | **ATS‑Pi reports a confused position** (both aux contacts closed) | Detected as `FAULT_CALIBRATION` (distinct from a normal mid‑transfer "both open"). `io_adam.py`. | Surfaced as a fault; GenWatch falls back to its own derivation rather than trusting bad data. | ✅ |
| 17 | **ATS‑Pi data stale but link "up"** (frozen frame) | GenWatch checks freshness; if stale it **stops treating the ATS‑Pi as authoritative** and falls back to the H‑100‑derived load source ("via gen telemetry"). `ats.py`. | No stale position shown as live. | ✅ |
| 18 | **Clock skew between the two Pis > 5 s** | GenWatch raises a **TIME_SKEW** alarm (ICD §11); NTP keeps them aligned. `ats.py` `_TIME_SKEW_THRESHOLD_S=5.0`. | Operator is warned; timestamps stay trustworthy. | ✅ |
| 19 | **Total loss of our whole system** (both Pis + ADAM dead) | Nothing of ours runs. | **The ASCO transfers automatically on an outage exactly as before** — our gear is purely additive. | ✅ |
| 20 | **Real utility outage** (the normal event) | The ATS starts the generator and transfers **on its own**; GenWatch/ATS‑Pi just observe and report it. | Normal protected operation, independent of our system's health. | ✅ |

---

## 3. The command interlocks (why a transfer can't fire by accident)

A remote Force‑Transfer must pass **all** of these, in order — any one blocks it:

1. **Authentication + role** — operator/admin; force‑transfer additionally requires admin. `control.py`
2. **Two‑step confirm token** — single‑use, 30 s, bound to operator and to the specific action.
3. **Healthy comms** — refused if the link to the device is degraded/lost.
4. **Fresh data** — refused if the registers backing state/position are stale even when the link is up.
5. **Mode gate** — refused unless the mode permits that command (§2 #14).
6. **State validity** — e.g. can't "Start" while running; can't transfer in an invalid state.
7. **(H‑100 path) panel in AUTO** — refused otherwise.

De‑asserts (release / back‑to‑utility) are **always allowed**, even when the above would block an
assert — the safe direction is never gated. `io_adam.py` `drive_outputs`.

---

## 4. The four independent watchdog layers

| Layer | Watches | Reaction | Timer |
|---|---|---|---|
| **Comms‑loss (ICD §8.3)** | the GenWatch↔ATS‑Pi command link | release maintained commands → utility | 30 s (or instant on TCP drop) |
| **Software (systemd)** | the app process is responsive | SIGKILL + restart | `WatchdogSec=60` |
| **Hardware (Pi SoC)** | the whole Pi/kernel is alive | reset the Pi | `RuntimeWatchdogSec=15s` |
| **F1 (ADAM host‑idle)** | the Pi is talking to the ADAM at all | ADAM drops its own relays | 5–10 s (set in the ADAM) |

They are independent and stacked: the comms watchdog covers a hung *link*, the software watchdog a
hung *process*, the hardware watchdog a hung *kernel*, and the F1 ADAM watchdog a *dead Pi*. The only
one that can release a latched relay when the Pi has **no power at all** is F1 — which is why it is a
mandatory bench test before go‑live.

---

## 5. What must be verified on the bench (honest list)

Most of the above is provable on the desk before the unit is ever on the live switch. Two items are
hardware‑specific and **must be physically demonstrated and recorded on the sign‑off**:

1. **F1 cable‑pull (scenario #10):** assert a transfer, pull the Pi's network cable, confirm the ADAM
   drops the relay within its watchdog timeout (→ utility). Re‑run after any ADAM swap/reset — nothing
   in software can warn you if the ADAM's safety‑values were lost.
2. **8‑9 transfer sense:** confirm the transfer input is **close‑to‑transfer** so a de‑energized relay
   = utility (the basis of scenarios #1, #2, #5, #6, #9, #10). If it were reversed, the fail‑safe
   direction would invert — so this is checked with a meter before landing the relay.

Everything else (comms‑loss 30 s release, mode‑gate rejection, confirm tokens, startup release,
position‑fault detection, GenWatch fallback) is reproducible on the bench with `atspi-bench` and is
covered by the automated test suites (the **GenWatch backend suite — 255 tests — passes on this
build**, alongside the ATS‑Pi companion suite), plus the bench procedures in `docs/BENCH.md`.

---

## 6. One‑paragraph summary for stakeholders

The transfer switch protects the load on its own; our system adds remote monitoring and an *optional*
remote transfer command that is wrapped in seven interlocks and four independent watchdogs. Every
credible failure — lost network, lost power, a crashed process, a hung kernel, a dead Pi, a failed
ADAM — drives the system to the **same safe state: the load on utility under the switch's own
automatic control.** No single failure, and no combination of failures of our equipment, can force an
unsafe transfer or strand the load on the generator. The two hardware‑specific guarantees (the dead‑Pi
relay release and the transfer‑input polarity) are demonstrated and recorded during commissioning.
