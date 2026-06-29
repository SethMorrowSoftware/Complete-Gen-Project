# Install Guide — Hooking the ATS‑Pi into *Our* System

A plain‑language, start‑to‑finish guide for connecting the ATS‑Pi companion to **our actual
equipment** so GenWatch can **see the transfer‑switch position** and **command a transfer**.

This guide is written for our specific setup:

- **Generator:** Generac with an **H‑100** controller — *monitoring only* (GenWatch reads it; it
  does **not** control the generator).
- **Transfer switch:** **ASCO Series 300**, 600 A / 480 V, with the **473670 "Group 1" controller**
  (the board with terminals 1–16 and the J4 port).
- **Existing transfer control:** a manual **Normal / Under‑Load toggle switch** wired to controller
  terminals **8–9** (flipping to "Under‑Load" opens 8–9 and transfers to the generator).
- **I/O module:** **ADAM‑6060** (6 digital inputs "DI", 6 relay outputs "RL").
- **Two Raspberry Pis:** one runs the **ATS‑Pi companion**, one runs **GenWatch**.

---

## 1. The big picture (read this first)

```
                          ┌─────────── GenWatch Pi (web UI :8000) ───────────┐
                          │   reads generator vitals from the H-100          │
   Generac H-100 ─(Modbus)┘   sends ATS commands to the companion :5020      │
                                                  │
                                                  ▼ (network)
                          ┌──────────── ATS-Pi companion :5020 ──────────────┐
                          │   talks to the ADAM-6060 over the network :502   │
                          └──────────────────────┬───────────────────────────┘
                                                 ▼ (network)
                                        ADAM-6060  (DI + RL)
                              DI ◄── position signals from the ASCO
                              RL ──► command inputs on the ASCO controller
                                                 │
                                                 ▼
                                  ASCO 473670 controller (terminals 6–13)
```

Two jobs:
- **Read position** — ADAM **DI** inputs watch the switch and tell GenWatch "on utility / on
  generator."
- **Command transfer** — ADAM **RL** relays press the ASCO controller's command buttons
  electrically, so GenWatch's button can transfer the load.

The Generac/H‑100 stays exactly as it is — GenWatch keeps reading it for generator data. All
**control** goes through the **ASCO**, which is why we need the companion.

---

## 2. Parts list

- ADAM‑6060 module (have it)
- **24 VDC DIN‑rail power supply** (e.g. Mean Well DR‑30‑24) + an inline **1 A fuse**
- **One small interposing relay with a 24 VDC coil and a NC (normally‑closed) contact** — for the
  transfer wiring (explained in Part A). A changeover/SPDT relay works too (use its NC side).
- *(Optional)* up to 3 more interposing relays if you also want Test / Inhibit / Bypass and/or if a
  position signal turns out to be 120 VAC (Part B).
- Control wire (18 AWG), ferrules, labels
- 3 Ethernet cables + the network switch
- Laptop with the **Advantech Adam/Apax .NET Utility** (to set the ADAM's IP)
- Multimeter

---

## 3. Safety — non‑negotiable

- This is **480 V / 600 A** switchgear. A **qualified electrician** does all landings, with **both
  sources de‑energized and locked out (LOTO)** and proper **NFPA 70E PPE**.
- The **command** wiring physically transfers 600 A of load. Treat it as the careful part:
  - Bench‑test the relays first with **nothing connected to the ASCO**.
  - Prove the **fail‑safe** (Part F) before trusting it.
  - Do the first real transfer as a **planned test**, not during business hours.

---

## Part A — Transfer control wiring (the main job)

Our switch transfers when terminals **8–9 are opened** (that's what the Under‑Load toggle does). We
add the ADAM **in series** with that toggle using one interposing relay, so **either** the manual
toggle **or** GenWatch can open 8–9. We never parallel 8–9.

### Wiring
```
ASCO 8 ──[ Normal/Under-Load toggle ]──[ interposing relay NC contact ]── ASCO 9

   24 VDC + ──[ ADAM RL1 contact ]── interposing relay COIL ── 24 VDC −
```

1. **Break into the 8–9 loop** and insert the interposing relay's **NC contact in series** with the
   existing toggle. (8 → toggle → relay NC → 9.)
2. **Drive the relay coil from ADAM RL1:** 24 VDC+ → ADAM **RL1** → relay coil → 24 VDC−.

### How it behaves
| Situation | Toggle | ADAM RL1 / relay | 8–9 | Result |
|---|---|---|---|---|
| Normal | Normal (closed) | off → NC closed | closed | load on utility |
| **Manual** transfer | **Under‑Load (open)** | off | open | transfer (operator) |
| **GenWatch** transfer | Normal (closed) | **on** → NC opens | open | transfer (software) |
| Power/comms lost | — | off → NC closes | closed | **auto‑returns to utility (safe)** |

The manual toggle keeps working exactly as today. GenWatch gains transfer control on top of it. If
anything loses power or the link drops, the relay releases and the load goes back to utility on its
own.

### (Optional) the other commands
These ASCO inputs are "close‑to‑command," so the ADAM relays drive them **directly** (no interposing
relay, no toggle involved):
- **ADAM RL0 → ASCO 6‑7** = Test (momentary start + test transfer)
- **ADAM RL2 → ASCO 10‑11** = Inhibit transfer
- **ADAM RL3 → ASCO 12‑13** = Bypass transfer time delay

Wire only the ones you want. For the boss's "transfer," **RL1 on 8‑9 (above) is the one that
matters.** Leave RL4/RL5 unused. Never touch ASCO 14‑15‑16 (factory use).

---

## Part B — Position readback wiring (so GenWatch shows it transferred)

This lets GenWatch display "on utility / on generator." It needs two position signals from the ASCO:

- **On‑Normal** signal → ADAM **DI1**
- **On‑Emergency** signal → ADAM **DI2**

**Find them and check what they are** (switch on Normal vs Generator, meter each):
- **Dry contact** (continuity flips, ~0 V) → wire straight to the DI + DI‑common. No extra parts.
- **10–30 VDC** (on/off with position) → wire straight to the DI (wet mode). No extra parts.
- **120 VAC** → use one interposing relay per signal (relay NC/NO dry contact → DI), same idea as
  Part A.

Each signal: one leg to **DI1** (or DI2), the other to the **DI common** (`DI.GND`).

> Position is **recommended** (so a transfer is confirmed on screen) but the **transfer control in
> Part A works without it.** If position signals are hard to get on day one, you can wire Part A,
> get control working, and add Part B later.

---

## Part C — Power, IP, and network the ADAM

1. **Power:** 24 VDC supply, fused 1 A. **+24 V → ADAM `+Vs`**, **0 V → ADAM `GND`**. Meter for 24 V
   and correct polarity **before** plugging in.
2. **IP:** the ADAM ships at 10.0.0.1. With the **.NET Utility**, set it to **192.168.1.251**, mask
   255.255.255.0. (Don't use the ADAM's web page — it's a dead Java applet on the 6060.)
3. **Network:** ADAM, ATS‑Pi (**192.168.1.250**), and GenWatch all into the same switch/subnet.
4. **Check:** from the ATS‑Pi, `ping 192.168.1.251` must reply.

---

## Part D — Configure the ATS‑Pi companion

Edit `/etc/atspi/config.yaml`:

```yaml
modbus_server:
  host: "192.168.1.250"   # the ATS-Pi's IP; GenWatch connects here
  port: 5020
  unit_id: 1

site:
  unit_id: 23             # GenWatch's expected_unit_id must match this

io:
  driver: adam
  adam:
    host: "192.168.1.251" # the ADAM
    port: 502
    unit_id: 1
    di_read: coils        # if position reads stuck/backwards, try: discrete_inputs
    debounce_samples: 3
    assumed_mode: auto    # 'auto' = transfer commands allowed (REQUIRED for control)
    require_hw_watchdog: false
    i_understand_no_crash_backstop: true
```

> `assumed_mode: auto` is what lets GenWatch issue transfer commands. In `manual` it would allow
> Inhibit only.

Start it:
```bash
sudo systemctl restart atspi
systemctl status atspi      # expect: active (running)
```

---

## Part E — Configure GenWatch

1. Make sure GenWatch is **out of mock mode** and reading the real H‑100 (so generator data is real):
   in `/etc/genwatch/config.yaml` set `mock: false` and a real `transport` to the H‑100, then
   `sudo systemctl restart genwatch`.
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

In the GenWatch UI (`http://<GenWatch-Pi>:8000`), the **ATS control** buttons (force‑transfer /
inhibit / test) are the ones that drive the ASCO through the companion. *(The generator start/stop
buttons write to the H‑100, which is monitor‑only here, so ignore those.)*

---

## Part F — Commission and test (do this carefully, in order)

**1. Bench the ADAM (nothing connected to the ASCO):**
- `ping` the ADAM; run `atspi-bench --host 192.168.1.251 --skip-dos` and jumper DI1/DI2 to common to
  confirm the inputs read. If they don't, switch `di_read` to `discrete_inputs`.
- Then `atspi-bench --host 192.168.1.251` to click each relay (RL0–RL3) and confirm by meter.

**2. Prove the fail‑safe (before connecting commands to the ASCO):**
- In the Advantech **.NET Utility**, enable the ADAM's **host‑idle watchdog** (timeout 5–10 s) and
  set **all relay safety values to OFF**.
- **Cable‑pull test:** have the companion assert a transfer (RL1 on), then **pull the Pi's network
  cable**. Confirm **RL1 drops within the timeout** (your interposing relay releases → 8‑9 closes →
  utility). This is the proof that a dead Pi can't strand the load on the generator.

**3. Verify comms‑loss auto‑release:**
- With everything connected on the bench, assert a transfer from GenWatch, then block the GenWatch↔
  companion link. The companion must drop the maintained relay at **~30 s**.

**4. First real transfer — planned window only:**
- With the electrician present and a planned outage window, use GenWatch to **transfer to generator**,
  confirm the load moves and position reads "generator," then **transfer back** and confirm utility.
- Verify the **manual Normal/Under‑Load toggle still works** independently.

Only after all four pass is the control path trustworthy for daily use.

---

## Using it day‑to‑day

| You want to… | In GenWatch… | What happens |
|---|---|---|
| Transfer load to the generator | **ATS → Force‑Transfer** | RL1 opens 8‑9 → ASCO transfers to gen |
| Return load to utility | release force‑transfer | RL1 closes 8‑9 → ASCO retransfers |
| (optional) Timed test | **ATS → Test** | RL0 pulses 6‑7 |
| (optional) Block transfers | **ATS → Inhibit** | RL2 holds 10‑11 |

The manual **Normal/Under‑Load toggle** on the panel always works too — software is *added*, not a
replacement.

---

## Quick troubleshooting

- **ATS buttons greyed out / 404 in GenWatch:** companion not reachable or `ats.enabled` not set —
  check Part D/E and `ping 192.168.1.250`.
- **Commands rejected:** `assumed_mode` is not `auto`, or comms to the companion is unhealthy.
- **Position stuck/backwards:** flip `di_read` to `discrete_inputs`; don't swap field wires.
- **Transfer doesn't happen on RL1:** confirm the interposing relay's **NC** contact is the one in
  series, the toggle is in **Normal**, and RL1 actually energizes the coil (meter it).
- **Service won't start:** read `journalctl -u atspi -n 40` — usually a config key typo or the
  watchdog acknowledgement missing.

---

### One‑line summary
Wire **ADAM RL1 → an interposing relay's NC contact → in series with the existing 8‑9 toggle** for
transfer control, wire **two position signals → ADAM DI1/DI2** for readback, point the companion at
the ADAM and GenWatch at the companion, then **prove the fail‑safe and do a planned test transfer**
before going live.
