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
 SEND transfer:   GenWatch ──► companion ──► ADAM RL1 ──► ASCO 8-9 (transfer)
```
Just two pieces of wiring: **two dry contacts into the DI side**, and **the relays straight onto the
ASCO command terminals on the RL side.**

---

## Parts you need
- ADAM‑6060 (have it)
- 24 VDC power supply + an inline **1 A fuse**
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
The ADAM‑6060's inputs are **opto‑isolated, dry‑contact** (open = 1, shorted to DI‑common = 0), so
they sense these contacts alongside the existing wiring without backfeeding — **as long as you
confirmed the clean full open/close in 1a** (that rules out a parallel path). **No relay, no extra
parts.**

---

## STEP 2 — Transfer / command wiring (so GenWatch can transfer)

Each ADAM relay is a **2‑terminal switch (`RLx+` / `RLx−`)** that bridges an ASCO command pair —
**one wire to each side, polarity doesn't matter.** Wire RL1 straight across **8‑9** for transfer,
the same way as the other commands.

### ⚠️ Verify the 8‑9 sense FIRST (this is the safety check)
A direct relay only works if **8‑9 transfers by CLOSING**, so that a de‑energized relay = open =
**utility (safe).** Meter the toggle / 8‑9:
- **Closed on "Under‑Load" (transfer), open on "Normal"** → good, direct RL1 is correct. ✅
- **The reverse** (8‑9 *opens* to transfer) → **STOP.** A direct relay would fail to GENERATOR on
  power loss — unsafe. You'd need an interposing (normally‑closed) relay instead; come back to this.

### Complete relay → ASCO wiring
| ADAM relay | `+` → ASCO | `−` → ASCO | Command | Needed for "transfer"? |
|---|---|---|---|---|
| **RL1** | **8** | **9** | **Transfer to generator** | ✅ **yes — the main one** |
| RL0 | 6 | 7 | Test (start + test transfer) | optional |
| RL2 | 10 | 11 | Inhibit transfer | optional |
| RL3 | 12 | 13 | Bypass transfer time delay | optional |
| RL4 | — | — | unused | leave empty |
| RL5 | — | — | unused | leave empty |

Wire only the ones you want; **RL1 → 8/9 is the one that gives your boss software transfer.** Never
touch ASCO **14‑15‑16** (factory use).

**How RL1 behaves (with 8‑9 close‑to‑transfer, toggle left on Normal):**
| Situation | RL1 | 8‑9 | Result |
|---|---|---|---|
| Normal | off (open) | open | load on **utility** |
| **GenWatch transfer** | on (closed) | closed | **generator** |
| **Manual** toggle → Under‑Load | — | closed | **generator** |
| Power / Pi / comms lost | off (open) | open | **back to utility automatically (safe)** |

The manual toggle still works in parallel; software transfer is added on top, and a de‑energized
relay always falls back to utility.

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
    di_read: coils            # if position stays 'unknown' / DIs read all-0, change to: discrete_inputs
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
generator **start / stop / exercise / transfer** buttons write to the H‑100 — monitor‑only here —
so ignore them. For load transfer use the **ATS → Force‑Transfer** button, which goes through the
companion.*

---

## STEP 6 — Commission & test (do these in order)
1. **Bench the ADAM** (nothing connected to the ASCO):
   `atspi-bench --host 192.168.1.251 --skip-dos`, jumper DI1/DI2 to confirm the inputs read; then
   `atspi-bench --host 192.168.1.251` to click RL1 and confirm by meter.
2. **Prove the fail‑safe:** in the .NET Utility, enable the ADAM **host‑idle watchdog** (5–10 s) with
   **all relay safety values OFF.** Assert a transfer, **pull the Pi's network cable**, and confirm
   **RL1 drops** (→ 8‑9 opens → back to utility).
3. **Comms‑loss release:** assert a transfer from GenWatch, block the GenWatch↔companion link, and
   confirm the relay drops at **~30 s**.
4. **First real transfer — planned window only:** transfer to generator, confirm position reads
   "generator," transfer back, confirm utility. Verify the **manual toggle still works** on its own.

Only after all four pass is the control path trustworthy for daily use.

---

## Using it day‑to‑day
| You want to… | GenWatch button | What happens |
|---|---|---|
| Transfer load to the generator | **ATS → Force‑Transfer** | RL1 closes 8‑9 → ASCO transfers |
| Return load to utility | release force‑transfer | RL1 opens 8‑9 → ASCO retransfers |

The manual **Normal/Under‑Load toggle** always works too — software is *added*, not a replacement.

---

## Troubleshooting
| Problem | Fix |
|---|---|
| No position / stuck `unknown` | change `di_read: coils` → `discrete_inputs`, restart |
| Position backwards | DI1/DI2 are swapped — re‑check the transfer test; don't swap field wires |
| ATS buttons greyed out / 404 | companion unreachable or `ats.enabled` off — `ping 192.168.1.250` |
| Transfer doesn't fire on RL1 | confirm RL1 (`+`/`−`) is across **8/9**, the toggle is on Normal, and 8‑9 is **close‑to‑transfer** (Step 2 check) |
| Service won't start | `journalctl -u atspi -n 40` — usually a config typo |

---

## Complete wiring reference (every connection)

**Power**
| ADAM terminal | Connect to |
|---|---|
| `+Vs` | 24 VDC **+** (fused 1 A) |
| `GND` | 24 VDC **−** |

**Inputs — position** *(meter Step 1a to learn which aux contact is Normal vs Emergency)*
| ADAM terminal | ASCO | Signal |
|---|---|---|
| `DI1` | On‑Normal aux contact (one leg) | load on utility |
| `DI2` | On‑Emergency aux contact (one leg) | load on generator |
| `DI GND` | the other leg of **both** aux contacts | input common |

*(The On‑Normal / On‑Emergency contacts are terminals **15‑16** and **19‑20** — meter to map.)*

**Relays — commands** *(each relay's `+`/`−` straddles the ASCO pair; polarity doesn't matter)*
| ADAM relay | `+` → ASCO | `−` → ASCO | Command |
|---|---|---|---|
| `RL1` | 8 | 9 | **Transfer to generator** (the main one) |
| `RL0` | 6 | 7 | Test (optional) |
| `RL2` | 10 | 11 | Inhibit (optional) |
| `RL3` | 12 | 13 | Bypass time delay (optional) |
| `RL4` | — | — | unused |
| `RL5` | — | — | unused |

**Network:** ADAM `192.168.1.251`, ATS‑Pi `192.168.1.250`, GenWatch — same switch.
**Do not** touch ASCO **14‑15‑16** (factory use).

---

### In one line
**Two dry aux contacts (15‑16, 19‑20) → ADAM DI1/DI2 for position; ADAM RL1 straight across ASCO
8‑9 for transfer (after confirming 8‑9 is close‑to‑transfer so a de‑energized relay = utility); point
the companion at the ADAM and GenWatch at the companion; then prove the fail‑safe and do a planned
test transfer.**
