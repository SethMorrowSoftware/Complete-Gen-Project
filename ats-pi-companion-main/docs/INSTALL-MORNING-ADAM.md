# ATS‑Pi Install — ADAM‑6060 Position Sensing (field runbook)

**Goal of this document:** get the ASCO transfer‑switch **position** (on Utility / on Generator)
into GenWatch through the ATS‑Pi companion, using the **ADAM‑6060** you already have, and
**without** adding interposing relays *if* the position signal turns out to be a dry contact or a
10–30 VDC signal (the ADAM reads both directly). This is the **monitor‑only** install: we wire the
*reading* of position and deliberately do **not** wire the command relays. That is the fast, safe
way to call the project "installed."

This runbook explains every step and why it matters. Read it once through before starting.

> ⚠️ **SAFETY — READ FIRST.** The ASCO is 480 V / 600 A switchgear. Every wire landing in this
> document is done by a **qualified electrician**, with **both sources de‑energized and locked out
> (LOTO)**, wearing NFPA 70E‑rated PPE. The only thing done with the cabinet energized is a single,
> deliberate voltage measurement in Step 2, and only if your safe‑work procedure allows a live
> metering task. When in doubt, do it dead.

---

## 0. The big picture — how the signal flows

Understanding the chain makes every step obvious:

```
  ASCO position contact            ADAM-6060                 ATS-Pi (companion)              GenWatch
 (closes on Normal/Emerg)  →   Digital Input (DI)   →   reads ADAM over Modbus/TCP   →   reads companion
                                                          (port 502, master)              (port 5020)
```

1. The **ASCO** gives us a contact (or a voltage) that tells us which source the load is on.
2. That signal drives an **ADAM‑6060 Digital Input (DI)**. The ADAM is a small Modbus‑over‑Ethernet
   I/O module: 6 digital inputs + 6 relay outputs.
3. The **ATS‑Pi** runs the `atspi` companion service. It is the **Modbus master** to the ADAM
   (it polls the ADAM on TCP port **502**) and turns the DI states into a clean "position" value.
4. The ATS‑Pi also runs a **Modbus server** on port **5020**. **GenWatch** connects to it and reads
   the position, so it appears on the GenWatch screen next to the H‑100 generator data.

We need exactly **two** input signals for position:

| Signal | Meaning | Goes to ADAM input |
|---|---|---|
| **On Normal** | load is on Utility | **DI1** |
| **On Emergency** | load is on Generator | **DI2** |

(The companion can also use source‑available and load‑disconnect inputs, but **none of those are
required** to report position. We skip them today.)

---

## 1. Gather tools, parts, and information

**Tools**
- Multimeter (continuity/ohms + AC volts + DC volts)
- Insulated screwdrivers, wire strippers, ferrules
- Flashlight, labels/tape

**Parts (what you already have)**
- ADAM‑6060 module
- 24 VDC DIN‑rail power supply (e.g. Mean Well DR‑30‑24) + an inline **1 A fuse**
- Hookup wire (18 AWG control wire is fine)
- 3 Ethernet patch cables and the network switch
- The ATS‑Pi (already imaged with the `atspi` service)

**Information to confirm before you start**
- IP plan: **ATS‑Pi = 192.168.1.250**, **ADAM = 192.168.1.251**, GenWatch = its existing IP.
  (Change these only if your site uses a different subnet — if so, keep all three on the same one.)
- A laptop with the **Advantech Adam/Apax .NET Utility** installed (used once, to set the ADAM's IP).

---

## 2. Find and classify the two position signals (the key step)

Everything downstream depends on this. You are looking for two signals on the ASCO — one that is
active when the switch is **on Normal**, one active when **on Emergency**.

### 2a. Where these signals live
In order of how easy they are to use:
1. **Door pilot lamps** labeled NORMAL / EMERGENCY (or SOURCE 1 / SOURCE 2). The wire feeding each
   lamp is a position signal.
2. **Auxiliary‑contact block** on the switch (on this unit, the block with terminals **13–20**,
   marked BACK/FRONT). These are dry contacts mechanically driven by the switch.
3. **Customer terminal strips** (the numbered blocks, e.g. 21–50 and the TB‑9 block) where the
   factory may have landed position contacts for remote indication.

### 2b. Identify which is Normal and which is Emergency
1. Put the switch on **Normal** (utility). With the meter, probe candidate pairs and find the one
   that is **made** — for a contact that means **continuity (≈0 Ω)**; for a lamp feed that means a
   **voltage is present**. Mark that one **"NORM."**
2. Transfer the switch to **Emergency** (start the generator / do a test transfer / exercise with
   load). The *other* signal will now become made. Mark it **"EMERG."**
3. You should see them swap cleanly: NORM made on Utility and open on Generator, EMERG the reverse.

### 2c. Classify each signal — this decides how you wire it
For **each** of NORM and EMERG, measure what it actually is:

- **Continuity test (meter on Ω / continuity):** isolate the pair if you can, and watch it as the
  switch moves. If it **opens and closes** with position and reads **~0 V** across it in both
  states, it is a **DRY CONTACT.**
- **DC volts test:** if instead it carries voltage, measure **VDC**. If you read a steady
  **10–30 VDC** that is present on one source and gone on the other, it is a **DC SIGNAL.**
- **AC volts test:** if VDC is ~0 but **VAC** reads ~120 V present/absent with position, it is a
  **120 VAC SIGNAL.**

Write down, for both NORM and EMERG: **dry / DC (volts) / AC (volts).** That's all you need.

### 2d. What each result means
| Measurement | How you'll wire it (Step 4) | Extra parts |
|---|---|---|
| **Dry contact** | straight to the ADAM DI, **dry mode** | none |
| **10–30 VDC** | straight to the ADAM DI, **wet mode** | none |
| **120 VAC** | the ADAM can't read AC — needs one interposing relay per signal | 2 relays |

> The dry aux contacts (13–20) are the best target: if you can read position there as a dry
> contact, you need **nothing but wire**. Do **not** splice into a contact that is already
> switching a 120 V lamp — that puts 120 V on the ADAM. Find a dry/DC version of the signal instead.

If both signals come back **dry** or **10–30 VDC**, you are relay‑free — continue. If a signal is
**120 VAC only** and you have no dry/DC alternative, stop here and add an interposing relay for that
signal (see `INSTALL-DB9-SERIAL.md` notes or ask) — everything else below is identical.

---

## 3. Understand the ADAM‑6060 terminals (before you land anything)

The ADAM‑6060 has three groups of terminals. **Confirm the exact labels against the legend printed
on your module** — wording varies slightly by revision.

- **Power:** `+Vs` and `GND` — this is where the 24 VDC supply lands (Step 5).
- **Digital Inputs:** `DI0`…`DI5` and one or more **DI common** terminals (often labelled `DI.COM`
  or `DI.GND`). A position signal lands across a `DIn` terminal and the DI common.
- **Relay Outputs:** `RL0`…`RL5` (each a dry Form‑A contact). **We do not use these today** — leave
  them empty.

**Dry mode vs wet mode:** the ADAM‑6060 input can either supply its own sensing current and look for
a **closure** (dry mode), or it can look for an **applied 10–30 VDC** (wet mode). You select the
behaviour by *how you wire it* and by the contact type. We use dry mode for dry contacts and wet
mode for a 10–30 VDC signal. The ADAM **cannot** accept 120 VAC on a DI — that is why AC signals
need a relay.

---

## 4. Wire the two position signals to the ADAM

Pick the row that matches what you measured in Step 2c. In all cases, do this **dead**, then label
both wires ("ATS‑PI NORM → DI1", "ATS‑PI EMERG → DI2").

**A) Dry contact (no relay):**
1. Run one conductor from one side of the **NORM** dry contact to ADAM **DI1**.
2. Run the other side of the NORM contact to the ADAM **DI common**.
3. Repeat for **EMERG** → ADAM **DI2** and DI common.
4. Result: when the switch is on Utility the NORM contact closes, pulling DI1; on Generator the
   EMERG contact closes, pulling DI2.

**B) 10–30 VDC signal (no relay, wet mode):**
1. Land the **NORM** DC signal (the "+" that appears when on Utility) on ADAM **DI1**, and its
   return/0 V on the ADAM DI common.
2. Land the **EMERG** DC signal on **DI2** and its return on DI common.
3. Result: the applied 10–30 VDC reads as "on" for that channel.

**C) 120 VAC signal (needs one relay per signal):**
1. Wire the NORM 120 VAC feed to a **120 VAC‑coil** interposing relay coil.
2. Wire that relay's **dry NO contact** to ADAM **DI1 + DI common**.
3. Repeat with a second relay for EMERG → **DI2 + DI common**.
4. The relay isolates the ADAM completely from the 120 V circuit.

**Do not wire ADAM relays RL0–RL5 today.** Command wiring (Test / Force‑Transfer / Inhibit) is a
separate, planned‑outage job. Monitor‑only means the ADAM can never actuate the switch.

---

## 5. Power and network the ADAM

1. **Power supply:** feed the 24 VDC supply from a fused source (1 A). Land **+24 V → ADAM `+Vs`**
   and **0 V → ADAM `GND`**. **Meter the output for ~24 VDC and correct polarity before** plugging
   it into the ADAM — reversed polarity can damage the module.
2. **Power‑on check:** the ADAM's status LED will blink — that's normal. Note: the **ADAM‑6060 has
   no per‑channel I/O lights**, so you confirm input states by Modbus read (Step 7), not by LEDs.
3. **Set the IP:** the ADAM ships at **10.0.0.1**. Connect your laptop, open the **Advantech .NET
   Utility**, find the module, and set its IP to **192.168.1.251**, mask **255.255.255.0**.
   (The 6060's built‑in web page is a dead Java applet — use the .NET utility, not a browser.)
4. **Cable everything to one subnet:** ADAM, ATS‑Pi (`192.168.1.250`), and GenWatch into the same
   switch.
5. **Prove reachability:** from the ATS‑Pi, run `ping 192.168.1.251`. You must get replies before
   continuing. No replies → check the cable, the switch, and that both ends are on `192.168.1.x`.

---

## 6. Configure the ATS‑Pi companion

The companion reads its settings from `/etc/atspi/config.yaml`. Put this in place (it is the
monitor‑only, ADAM‑only configuration). Each line is explained after.

```yaml
modbus_server:
  host: "192.168.1.250"   # the ATS-Pi's own IP; GenWatch connects here. Not 0.0.0.0.
  port: 5020              # the port GenWatch reads. Leave at 5020.
  unit_id: 1              # Modbus transport id for the GenWatch link.

site:
  unit_id: 23             # identity the companion reports; GenWatch's expected_unit_id must match.

io:
  driver: adam            # use the ADAM-6060 path (not 'mock', not 'hybrid').
  adam:
    host: "192.168.1.251" # the ADAM's IP.
    port: 502             # the ADAM speaks Modbus/TCP on 502. Leave as-is.
    unit_id: 1            # Modbus slave id used when talking TO the ADAM.
    di_read: coils        # how DIs are read; 'coils' is correct for the 6060 (see troubleshooting).
    debounce_samples: 3   # ~300 ms of debounce so contact chatter doesn't flicker the position.
    assumed_mode: auto    # operating posture; harmless here since no command relays are wired.
    require_hw_watchdog: false           # the 6060 can't expose its watchdog over Modbus.
    i_understand_no_crash_backstop: true # REQUIRED acknowledgement when the above is false.
```

Notes:
- `modbus_server.unit_id` (the GenWatch link) and `site.unit_id` (the identity GenWatch checks) are
  **different things** — leave them as shown.
- `io.adam.port: 502` (out to the ADAM) and `modbus_server.port: 5020` (in from GenWatch) are
  different ports for different links — do not merge them.
- The config validator **rejects unknown keys** — if you mistype a key the service won't start and
  will tell you which key is wrong. Don't invent channel‑map keys; the DI/DO map is fixed in code.
- `require_hw_watchdog: false` **must** be paired with `i_understand_no_crash_backstop: true` or the
  service refuses to start. That is the intended, audited posture for the ADAM‑6060.

Then start it:
```bash
sudo systemctl restart atspi
systemctl status atspi          # expect: active (running)
journalctl -u atspi -n 40       # read the last lines if it didn't start
```

---

## 7. Verify end‑to‑end

1. On the **ATS‑Pi**, sanity‑check the inputs read‑only (drives nothing):
   ```bash
   atspi-bench --host 192.168.1.251 --port 502 --unit-id 1 --skip-dos
   ```
   With the switch resting on **Utility** you should see **DI1 active, DI2 inactive**, and a position
   of **utility**. Transfer to **Generator** → **DI2 active**, position **generator**.
2. On **GenWatch**, the ATS tile should show **"via ATS‑Pi"** and the live position. Flip the switch
   and confirm GenWatch tracks it within a couple of seconds.

If both of those are true, **the full project is installed**: GenWatch sees the H‑100 generator
*and* the live ATS position.

---

## 8. Troubleshooting

- **Service won't start / `ConfigError`:** you mistyped a key or forgot
  `i_understand_no_crash_backstop: true`. The log names the offending key.
- **Position stuck `unknown`, or both inputs read the same no matter what:** the DI read function
  code is wrong for your module. Change `di_read: coils` → `di_read: discrete_inputs`, restart, and
  re‑test. Whichever one shows the inputs changing is correct — record it on the sign‑off.
- **Position is exactly backwards** (shows generator when on utility): you have NORM and EMERG
  swapped, **or** the contact you tapped is the opposite (NC) pole. **Do not "fix" it by swapping
  field wires blindly** — re‑confirm with the meter which contact is truly "on Normal," and land
  that one on DI1.
- **GenWatch shows no ATS / "not connected":** GenWatch can't reach the companion. Confirm
  `ping 192.168.1.250` from the GenWatch box, that the companion is `active (running)`, and that
  GenWatch's ATS config points at `192.168.1.250:5020` with `expected_unit_id: 23`.
- **`ping 192.168.1.251` fails:** ADAM IP not set, wrong subnet, or cabling — recheck Step 5.

---

## 9. What we deliberately deferred (do later, planned outage)

- **Command relays** (ADAM RL0–RL3 → ASCO controller terminals 6‑7 Test, 8‑9 Transfer‑to‑Emergency,
  10‑11 Inhibit, 12‑13 Bypass). These let GenWatch *command* a transfer. They require a planned
  outage and the ADAM host‑watchdog "cable‑pull" fail‑safe test before they can be trusted.
- **Source‑available inputs** (DI3/DI4) and **load‑disconnect** (DI0) — nice‑to‑have, not needed for
  position.

Monitor‑only is a complete, useful, safe install. Commands are a clean follow‑up, not a prerequisite.
