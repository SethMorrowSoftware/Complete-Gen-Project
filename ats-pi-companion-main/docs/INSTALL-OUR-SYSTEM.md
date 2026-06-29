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
- **One interposing relay** for the 8‑9 transfer wiring: a **24 VDC coil** + a **changeover (SPDT)
  contact** (so it has COM / NC / NO — you'll use **COM + NC**). A plug‑in "ice‑cube" relay on a DIN
  socket is easiest, e.g. **Finder 40.52** or **Omron MY2**, **24 VDC coil**. (Full wiring in Part A.2.)
- *(Optional)* up to 3 more interposing relays only if a **position** signal turns out to be 120 VAC
  (Part B). The Test / Inhibit / Bypass commands do **not** need extra relays.
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

### A.0 — How EVERY one of these connections works (read this once)

Each ASCO command is a **pair of terminals**, and the ASCO controller is watching for a **contact
bridged across that pair**. An **ADAM relay is just a contact (a switch) with two terminals** — on
your module they're labelled **RL0+ / RL0−**, **RL1+ / RL1−**, etc. To "press" a command you put
**one ASCO terminal on each side of the relay**:

```
ASCO (first number)  ──────►  RLx +
                                 ⌇   ← contact closes INSIDE the ADAM when the companion fires RLx
ASCO (second number) ──────►  RLx −
```

Two rules that matter:
- **One wire to each side.** ASCO 6 on one terminal, ASCO 7 on the other — **never both on the same
  terminal** (that would jam the command on permanently).
- **Polarity doesn't matter** on these dry contacts: `+` and `−` are simply the two sides of the
  switch, so 6→`+`/7→`−` is identical to 6→`−`/7→`+`.

### A.1 — The simple commands: Test / Inhibit / Bypass (optional)

These three ASCO inputs are "**close to command**," so an ADAM relay drives each one **directly** —
its two terminals straddle the ASCO pair. No toggle, no extra relay:

| ADAM relay | `+` terminal → | `−` terminal → | ASCO function |
|---|---|---|---|
| **RL0** | ASCO **6** | ASCO **7** | **Test** (momentary start + test transfer) |
| **RL2** | ASCO **10** | ASCO **11** | **Inhibit** transfer |
| **RL3** | ASCO **12** | ASCO **13** | **Bypass** transfer time delay |

Wire only the ones you want. Leave **RL4 / RL5 unused.** **Never** land anything on ASCO **14‑15‑16**
(factory use).

### A.2 — The transfer command on 8‑9 (the MAIN one — needs ONE extra relay)

**Why 8‑9 is not wired like the others:**
1. 8‑9 **transfers when it is OPENED**, not closed (your Normal/Under‑Load toggle opens it to
   transfer).
2. 8‑9 **already has the toggle** on it, and we must keep that working.
3. The ADAM relay is **normally‑open** (it *closes* to command). To *open* a loop on command you need
   a **normally‑closed** contact — which the ADAM doesn't have by itself.

So we add **one small interposing relay**: the ADAM switches its **coil**, and the interposing
relay's **normally‑closed (NC)** contact sits **in series** inside the 8‑9 loop, alongside the
toggle. We do **not** wire RL1 onto 8‑9 directly, and we **never parallel** 8‑9.

#### What you need (the extra relay)
- **One interposing relay** with:
  - a **24 VDC coil** (runs off the same 24 VDC supply as the ADAM), and
  - a **changeover / SPDT contact** so it has **COM, NC, NO** terminals — you will use **COM + NC**.
  - A **plug‑in ice‑cube relay on a DIN socket** is easiest (e.g. **Finder 40.52** or **Omron MY2**,
    **24 VDC coil**).
- The 8‑9 circuit is tiny (5 VDC / 5 mA), so any signal relay's contact is far more than enough.

#### Wire it in two halves

**HALF 1 — the coil (ADAM RL1 turns the interposing relay on/off):**
```
24 VDC (+) ───────────────────►  ADAM RL1 +
ADAM RL1 − ───────────────────►  interposing-relay COIL (terminal A1)
interposing-relay COIL (A2) ──►  24 VDC (−)
```
When the companion fires **RL1**, the ADAM contact closes, 24 V appears across the coil, and the
interposing relay **energizes**. (Coil polarity doesn't matter on a DC ice‑cube relay; A1/A2 are
just the two coil terminals.)

**HALF 2 — the NC contact, inserted into the existing 8‑9 loop:**
Today the loop is simply `ASCO 8 → toggle → ASCO 9`. **Break the wire between the toggle and ASCO 9**
and route it through the relay's **COM → NC**:
```
ASCO 8 ─────────────►  toggle (one side)
toggle (other side) ►  interposing-relay COM
interposing-relay NC ► ASCO 9
```
The full loop becomes: **8 → toggle → COM →(NC contact)→ 9.**

> Use only the relay's **COM** and **NC** terminals. **Do not use NO** — it would do the opposite.

#### How it behaves
| Situation | Toggle | Interposing relay (driven by RL1) | 8‑9 loop | Result |
|---|---|---|---|---|
| Normal running | Normal (closed) | **off** → NC **closed** | closed | load on **utility** |
| **Manual** transfer | **Under‑Load (open)** | off | open (at the toggle) | transfer — operator |
| **GenWatch** transfer | Normal (closed) | **on** → NC **opens** | open (at the relay) | transfer — software |
| Power / Pi / comms lost | any | off → NC **closed** | closed | **returns to utility automatically (safe)** |

The manual toggle works exactly as before; GenWatch's transfer button now *also* opens the loop
(RL1 → coil → NC opens). Anything that drops power or the network releases the relay, the NC closes,
and the load falls back to utility on its own. **RL1 + this one interposing relay is the connection
that gives your boss software transfer control.**

#### Before you cut into 8‑9 — verify the toggle
Meter it: continuity across the toggle should read **closed on "Normal"** and **open on
"Under‑Load."** That confirms it's a simple series contact you can insert the relay's NC into. If it
behaves differently (a multi‑pole/changeover doing something else), **stop and map it first.**

---

## Part B — Position readback wiring (so GenWatch shows it transferred)

This lets GenWatch display "on utility / on generator." It needs two position signals from the ASCO:

- **On‑Normal** signal → ADAM **DI1**
- **On‑Emergency** signal → ADAM **DI2**

Unlike the relays (two‑terminal switches), each **DI is read against the shared `DI GND` common.**
So each position signal uses **two landings: the signal to a `DIn` terminal, and its return to
`DI GND`:**
```
position signal  ──►  ADAM DI1   (On-Normal)   or   DI2 (On-Emergency)
its return/0V    ──►  ADAM DI GND  (the shared input common)
```

**Find the signals and check what they are** (put the switch on Normal vs Generator, meter each):
- **Dry contact** (continuity flips, ~0 V across it) → land it straight: one leg → `DI1`/`DI2`, other
  leg → `DI GND`. No extra parts.
- **10–30 VDC** (voltage present on one source, gone on the other) → land it straight (wet mode):
  the `+` → `DI1`/`DI2`, the `0 V` → `DI GND`. No extra parts.
- **120 VAC** → the ADAM can't read AC, so add **one interposing relay per signal**: the 120 VAC
  drives the relay coil, and the relay's **dry contact** lands across `DIn` and `DI GND` (same trick
  as Part A's relay, just feeding an input instead of an ASCO command).

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
