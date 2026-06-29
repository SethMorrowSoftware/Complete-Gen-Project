# ATS‑Pi Install — DB9 / J4 Serial Path (alternative, experimental)

**What this is:** an alternative way to read ASCO transfer‑switch **position** — over the
controller's **J4 DB9 serial port** instead of through dry contacts on the ADAM‑6060. You would use
this if you cannot get a usable dry or 10–30 VDC position contact (see `INSTALL-MORNING-ADAM.md`),
or if you simply prefer a single serial cable.

> ⚠️ **READ THIS BEFORE YOU INVEST TIME — this path may not work on your controller.**
> Your controller is an **ASCO 300 "Group 1"** unit (P/N **473670‑006R**). Strong evidence says
> these controllers speak **ASCOBus** (ASCO's *proprietary* protocol) on J4 natively, and that
> Modbus is only obtained by adding a **Flight Systems M327 modem** that translates ASCOBus→Modbus.
> The ATS‑Pi companion speaks **Modbus, not ASCOBus.** So a plain USB‑to‑RS‑485 adapter on J4 may
> return **nothing**. Treat this as a **go/no‑go experiment**: there is a quick test (Step 4) that
> tells you in a few minutes whether it can work *at all*. **Do not make this your only install
> plan for a deadline** — keep the ADAM dry‑contact path ready as the reliable fallback.

There are three unknowns on this path, all of which the test in Step 4 either resolves or rules out:
1. **Protocol** — does the controller emit native Modbus, or ASCOBus (needs the M327)?
2. **Pinout** — which DB9 pins are the RS‑485 data pair? (We identify this with a meter, Step 3.)
3. **Register** — which Modbus register holds position? (Only relevant if Step 4 passes.)

---

## 0. Safety and what you're touching

The J4 connector is a **low‑voltage signal port**, but it lives inside 480 V switchgear. Open the
cabinet only under the same rules as the main install: **qualified electrician, LOTO where required,
PPE.** The controller itself can stay powered for the metering in Step 3 (you need it powered to see
the idle signal), but treat everything around it as live.

J4 is a **9‑pin D‑sub (DB9)** on the controller board, currently **unplugged**. The RS‑485 signal
set documented for this controller family is **Y, Z, B, A, 24, GND** (two differential pairs, a
24 V rail, and ground), with a **jumper to select full‑ vs half‑duplex**. For a 2‑wire USB adapter
you want **half‑duplex (2‑wire)** mode.

---

## 1. What you need

- The **Waveshare USB‑to‑RS‑485** adapter (terminals **A**, **B**, and **GND/PE**).
- A short length of shielded twisted pair, ferrules.
- Multimeter (DC volts).
- The ATS‑Pi (or a laptop) to run the test scan.
- A jumper/DB9 breakout or carefully made flying leads to reach individual DB9 pins.

> The Waveshare is a **2‑wire** (half‑duplex) device: **A** = the non‑inverting line (D+), **B** =
> the inverting line (D−), **GND** = signal ground/shield. It has no separate transmit/receive
> pairs, which is why the controller must be put in **half‑duplex/2‑wire** mode.

---

## 2. Set the controller's serial port

Before wiring, make the controller emit on J4 in a form a 2‑wire Modbus master can use:

1. **Duplex jumper:** set the controller's RS‑485 jumper to **half‑duplex (2‑wire)**. (On this
   family that is the "half" position; consult the controller's own legend for which jumper.)
2. **Protocol:** if the controller exposes a serial **protocol** setting, choose **Modbus** (often
   shown as "Mbus"). **If the only choices are ASCOBus variants, this path will not work without the
   M327 modem — stop here and use the ADAM path.**
3. **Baud / address:** note the configured **baud rate** and **device address**. You will match
   these on the Pi. If you can set them, pick **19200 baud, address 1** as a simple default.
4. Framing for Modbus RTU on this family is **8 data bits, no parity, 1 stop bit (8N1).**

---

## 3. Identify the RS‑485 pins on the DB9 with a meter

Because we do not have the unit's pin table, we find the data pair empirically. RS‑485 is robust:
wrong guesses here do **not** damage anything, so this is safe to probe.

1. Power the controller and make sure J4 is in **Modbus/2‑wire** mode (Step 2). Leave J4 unplugged.
2. Set the meter to **DC volts**. Put the **black** probe on the DB9 metal shell or a known ground
   pin (the "GND" pin), and touch the **red** probe to each of the 9 pins in turn.
3. You are looking for the **two pins that sit at a small steady idle bias** relative to ground —
   the RS‑485 transmit pair idles a few hundred millivolts apart, with one line slightly more
   positive than the other. Those two are your **A/B (D+/D−)** data lines. The pin that reads
   continuity to the shell/0 V is **GND**.
4. Note your best guess: **A‑candidate pin**, **B‑candidate pin**, **GND pin**. Polarity (which is A
   vs B) does not have to be right on the first try — RS‑485 tolerates a swap, and we fix it in
   Step 4 if there's no response.

> If you can obtain the real pin table from Flight Systems doc **73‑K473670‑00** or the **M327V2**
> manual, use it instead of guessing — it is the authoritative source. The meter method is the
> fallback when those documents aren't available.

---

## 4. Wire the adapter and run the GO/NO‑GO test

This single test tells you whether the serial path is even possible.

1. With the controller **de‑energized for the landing** (or via a safe procedure), connect:
   - Waveshare **A** → your **A‑candidate** DB9 pin
   - Waveshare **B** → your **B‑candidate** DB9 pin
   - Waveshare **GND** → the DB9 **GND** pin / shield (ground at the Pi end only to avoid a loop)
2. Plug the Waveshare into the ATS‑Pi (or laptop). It appears as e.g. `/dev/ttyUSB0`.
3. Run a Modbus RTU scan at the controller's baud/address. For example, using a quick Python probe:
   ```bash
   # adjust port, baud, and the address (slave id) to match the controller
   python3 - <<'PY'
   from pymodbus.client import ModbusSerialClient
   c = ModbusSerialClient(port="/dev/ttyUSB0", baudrate=19200, bytesize=8,
                          parity="N", stopbits=1, timeout=1)
   c.connect()
   for addr in range(1, 248):            # try every Modbus address
       r = c.read_holding_registers(0, count=2, slave=addr)
       if not r.isError():
           print("RESPONSE from address", addr, "->", r.registers); break
   else:
       print("NO RESPONSE on any address")
   c.close()
   PY
   ```
4. **Interpret the result:**
   - **You get a response** → the controller speaks Modbus. The serial path is alive. Continue to
     Step 5.
   - **"NO RESPONSE"** → first **swap A and B** (Step 3 polarity) and run once more. Still nothing →
     the controller is almost certainly **ASCOBus** (or J4 isn't in Modbus mode). **Stop. Use the
     ADAM dry‑contact path.** Do not keep fighting this on install day.

---

## 5. If it responded — find the position register and configure `hybrid`

The companion has **no serial‑only driver**; serial position is read via the **`hybrid`** driver,
which uses the serial link for position and still keeps the ADAM available. So you will run
`driver: hybrid`.

1. **Locate the position register.** With the scan working, read candidate holding registers and
   transfer the switch Utility↔Generator to see which value flips between two states (e.g. 0 ↔ 1).
   That register/bit is "position." (On related ASCO Modbus maps, position appears as a
   "SwitchPosition" register reading 0 = Normal, 1 = Emergency — confirm by watching it change.)
2. **Configure the companion** (`/etc/atspi/config.yaml`), filling in the verified values:
   ```yaml
   io:
     driver: hybrid
     asco_serial:
       port: "/dev/atspi-asco"      # udev-stable symlink for the Waveshare (or /dev/ttyUSB0)
       baudrate: 19200
       parity: N
       stopbits: 1
       slave: 1                     # the controller's Modbus address
       status_register: <ADDR>      # the position register you confirmed
       # plus any bit/offset keys the serial driver requires for this register
     adam:
       host: "192.168.1.251"
       port: 502
       unit_id: 1
   ```
3. **Restart and verify** exactly as in the ADAM runbook (`systemctl restart atspi`, watch the
   position track the switch in GenWatch).

> ⚠️ **Freshness caveat (audit item H‑2):** a serial read can return a *stale but valid* frame if the
> controller stops updating. Before trusting serial position in production, confirm the companion
> treats a frozen frame as stale (this is the open H‑2 item). Until then, the **dry‑contact ADAM
> path is the more trustworthy source** for go‑live.

---

## 6. Decision summary

```
 Can you get a DRY or 10–30 VDC position contact?  ── yes ──►  Use INSTALL-MORNING-ADAM.md (reliable)
                         │
                         no
                         ▼
 Does J4 have a "Modbus" protocol option, and does the Step-4 scan respond?
        │                                   │
       yes ──► Step 5: hybrid + serial     no ──► ASCOBus/needs M327 modem ─► back to ADAM path
```

**Bottom line:** the serial path is worth a quick test if the dry‑contact route is blocked, but it
is **not** a guaranteed deadline solution. The ADAM dry/DC path in `INSTALL-MORNING-ADAM.md` is the
one to rely on.
