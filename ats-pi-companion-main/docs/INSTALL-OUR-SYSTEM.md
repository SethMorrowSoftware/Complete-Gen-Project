# Install Guide — Connecting the ATS‑Pi to Our System

**What this does:** GenWatch already monitors the generator. This adds the **ATS‑Pi** so GenWatch
can also **(1) see which source the load is on** and **(2) transfer the load.** Everything runs
through the **ADAM‑6060** I/O module: its **inputs (DI)** read switch position, its **relays (RL)**
command the ASCO.

---

## Our equipment
| Part | What it is |
|---|---|
| **Generator** | Generac **H‑100** — GenWatch already monitors it (no change) |
| **Transfer switch** | **ASCO Series 300**, 600 A / 480 V, **473670 controller** |
| **Existing transfer control** | manual **Normal / Under‑Load toggle** on controller terminals **8–9** |
| **Position contacts** | aux‑contact block — terminals **15‑16 and 19‑20** (confirmed dry, opposite states) |
| **I/O module** | **ADAM‑6060** — 6 inputs (**DI**), 6 relays (**RL**) |
| **Pis** | one runs the **companion**, one runs **GenWatch** |

---

## The whole job in one picture
```
 READ position:   ASCO aux 15-16 & 19-20 ──► ADAM DI1 / DI2 ──► companion ──► GenWatch
 SEND transfer:   GenWatch ──► companion ──► ADAM RL1 ──► interposing relay ──► ASCO 8-9 toggle loop
```
Just two pieces of wiring: **two dry contacts into the DI side**, and **one relay on the RL side.**

---

## Parts you need
- ADAM‑6060 (have it)
- 24 VDC power supply + an inline **1 A fuse**
- **One interposing relay:** **24 VDC coil**, **changeover/SPDT** contact (you'll use **COM + NC**).
  e.g. **Finder 40.52** or **Omron MY2**, 24 VDC coil, on a DIN socket.
- Control wire, ferrules, labels, multimeter
- 3 Ethernet cables + the network switch
- Laptop with the Advantech **.NET Utility** (sets the ADAM's IP)

---

## Safety — non‑negotiable
- **480 V / 600 A. Qualified electrician only. Both sources locked out (LOTO). NFPA 70E PPE.** Do
  all landings dead.
- The **transfer (RL) side physically moves 600 A** — it's the careful part: bench‑test the relays
  first, prove the fail‑safe, and do the first real transfer in a **planned window**.

---

## STEP 1 — Position wiring (so GenWatch sees the source)

Your two position contacts are **15‑16** and **19‑20** — both **dry** (no voltage), in opposite
states. One closes on Normal, the other on Emergency.

**1a. Map them with the meter (transfer test):** meter on continuity, move the switch
Normal ↔ Emergency, and watch the two contacts **swap**.
- The contact **closed when on Normal** → **On‑Normal → DI1**
- The contact **closed when on Emergency** → **On‑Emergency → DI2**
- ⚠️ Watch for a **clean full open/close.** If one won't fully open, there's a parallel path — stop
  and check before wiring.

**1b. Wire them (parallel — add one wire per terminal, leave the existing wires in place):**
```
On-Normal contact    →  one terminal → ADAM DI1 ,  other terminal → ADAM DI GND
On-Emergency contact →  one terminal → ADAM DI2 ,  other terminal → ADAM DI GND
```
The ADAM's **isolated inputs** read these cleanly alongside their existing wiring — **no relay, no
extra parts.**

---

## STEP 2 — Transfer wiring (so GenWatch can transfer)

Terminals **8–9 transfer when the loop is OPENED**, and they already have your Normal/Under‑Load
toggle. We add the ADAM **in series** using one interposing relay, so **either** the manual toggle
**or** GenWatch can transfer. **Never parallel 8–9.**

> **Why a relay is needed:** the ADAM relay is normally‑**open** (it *closes* to command), but 8–9
> needs a normally‑**closed** contact that *opens* to transfer. The interposing relay provides that.

**2a. Coil — ADAM RL1 switches the interposing relay on/off:**
```
24 VDC (+) → ADAM RL1 +        ADAM RL1 − → relay COIL (A1)        relay COIL (A2) → 24 VDC (−)
```

**2b. NC contact — insert it into the 8‑9 loop** (break the wire between the toggle and ASCO 9):
```
ASCO 8 → toggle → relay COM → relay NC → ASCO 9
```
Use **COM + NC only** — not NO.

**How it behaves:**
| Situation | 8‑9 loop | Result |
|---|---|---|
| Normal (RL1 off, toggle on Normal) | closed | load on **utility** |
| **GenWatch transfer** (RL1 on) | NC opens → open | **generator** |
| **Manual** toggle → Under‑Load | open at toggle | **generator** |
| Power / Pi / comms lost | NC closes → closed | **back to utility automatically (safe)** |

**Before cutting in:** meter the toggle — continuity **closed on "Normal," open on "Under‑Load."**
If it behaves differently, stop and map it first.

---

## STEP 3 — Power & network the ADAM
1. 24 VDC supply, fused 1 A → ADAM **+Vs / GND**. **Check polarity before plugging in.**
2. Set the ADAM's IP to **192.168.1.251** using the Advantech **.NET Utility** (not the web page).
3. Put the ADAM, ATS‑Pi (**192.168.1.250**), and GenWatch on the **same network switch**.
4. From the ATS‑Pi: `ping 192.168.1.251` must reply.

---

## STEP 4 — Configure the ATS‑Pi
Edit `/etc/atspi/config.yaml`:
```yaml
modbus_server: { host: "192.168.1.250", port: 5020, unit_id: 1 }
site: { unit_id: 23 }
io:
  driver: adam
  adam:
    host: "192.168.1.251"
    port: 502
    unit_id: 1
    di_read: coils            # if position reads stuck/backwards, change to: discrete_inputs
    debounce_samples: 3
    assumed_mode: auto        # 'auto' = transfer commands allowed (required for control)
    require_hw_watchdog: false
    i_understand_no_crash_backstop: true
```
Then:
```bash
sudo systemctl restart atspi
systemctl status atspi        # expect: active (running)
```

---

## STEP 5 — Configure GenWatch
1. Make sure GenWatch is **out of mock mode** and reading the real H‑100 — in
   `/etc/genwatch/config.yaml` set `mock: false` and a real `transport`, then restart.
2. Point GenWatch at the companion (same file):
   ```yaml
   ats:
     enabled: true
     host: 192.168.1.250
     port: 5020
     framer: socket
     slave: 1
     expected_unit_id: 23
   ```
3. `sudo systemctl restart genwatch`.

Use the **ATS control** buttons in GenWatch (those drive the ASCO through the companion). *The
generator start/stop buttons write to the H‑100, which is monitor‑only here, so ignore them.*

---

## STEP 6 — Commission & test (do these in order)
1. **Bench the ADAM** (nothing connected to the ASCO):
   `atspi-bench --host 192.168.1.251 --skip-dos`, jumper DI1/DI2 to confirm the inputs read; then
   `atspi-bench --host 192.168.1.251` to click RL1 and confirm by meter.
2. **Prove the fail‑safe:** in the .NET Utility, enable the ADAM **host‑idle watchdog** (5–10 s) with
   **all relay safety values OFF.** Assert a transfer, **pull the Pi's network cable**, and confirm
   **RL1 drops** (→ interposing relay releases → back to utility).
3. **Comms‑loss release:** assert a transfer from GenWatch, block the GenWatch↔companion link, and
   confirm the relay drops at **~30 s**.
4. **First real transfer — planned window only:** transfer to generator, confirm position reads
   "generator," transfer back, confirm utility. Verify the **manual toggle still works** on its own.

Only after all four pass is the control path trustworthy for daily use.

---

## Using it day‑to‑day
| You want to… | GenWatch button | What happens |
|---|---|---|
| Transfer load to the generator | **ATS → Force‑Transfer** | RL1 opens 8‑9 → ASCO transfers |
| Return load to utility | release force‑transfer | RL1 closes 8‑9 → ASCO retransfers |

The manual **Normal/Under‑Load toggle** always works too — software is *added*, not a replacement.

---

## Troubleshooting
| Problem | Fix |
|---|---|
| No position / stuck `unknown` | change `di_read: coils` → `discrete_inputs`, restart |
| Position backwards | DI1/DI2 are swapped — re‑check the transfer test; don't swap field wires |
| ATS buttons greyed out / 404 | companion unreachable or `ats.enabled` off — `ping 192.168.1.250` |
| Transfer doesn't fire on RL1 | confirm interposing **NC** (not NO) is in series, toggle on Normal, RL1 energizes the coil |
| Service won't start | `journalctl -u atspi -n 40` — usually a config typo |

---

## (Optional) extra commands
If you also want them later, these ASCO inputs are "close‑to‑command" — wire one ADAM relay
**straight across each pair** (`+` and `−` to the two terminals, polarity doesn't matter):

| ADAM relay | → ASCO | Function |
|---|---|---|
| RL0 | 6 / 7 | Test |
| RL2 | 10 / 11 | Inhibit |
| RL3 | 12 / 13 | Bypass time delay |

Leave **RL4 / RL5 unused.** **Never** touch ASCO **14‑15‑16** (factory use).

---

### In one line
**Two dry aux contacts (15‑16, 19‑20) → ADAM DI1/DI2 for position; ADAM RL1 → one interposing
relay's NC in series with the 8‑9 toggle for transfer; point the companion at the ADAM and GenWatch
at the companion; then prove the fail‑safe and do a planned test transfer.**
