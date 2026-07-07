# ATS-Pi tools

Helper utilities for commissioning the ATS-Pi companion.

## `Set-AdamIp.ps1` — re-IP an ADAM-6060 from a Windows laptop

The ADAM-6060 ships at `10.0.0.1/24` (Modbus/TCP 502) and its network identity
can only be **written** through Advantech's **Adam/Apax .NET Utility** — the
module's web page is a dead Java applet, and the 6060 exposes no network-config
registers over Modbus (see `docs/BENCH.md §10`). This script does **not** write
the IP; it automates the tedious, error-prone parts around that one utility step
and verifies the result:

1. **Phase A** — puts the wired NIC on the ADAM's staging subnet (`10.0.0.50/24`)
   and confirms the module is reachable: ping + TCP 502 + a **read-only** Modbus
   coil read that proves it's really the ADAM answering.
2. **Pause** — prints the exact utility steps and waits while you set the Static
   IP in the Adam/Apax .NET Utility (config password `00000000`).
3. **Phase B** — moves the NIC onto the target OT subnet and re-verifies the
   module at its new address the same three ways (with retries for the reboot).

Read-only throughout — it never changes the ADAM's outputs or the transfer switch.

### Usage

Run from an **elevated** PowerShell (Windows PowerShell 5.1 or PowerShell 7) on a
laptop cabled to the ADAM:

```powershell
Unblock-File .\Set-AdamIp.ps1
powershell -ExecutionPolicy Bypass -File .\Set-AdamIp.ps1        # defaults: 10.0.0.1 -> 192.168.1.36
# or override:
.\Set-AdamIp.ps1 -NewIp 192.168.1.36 -InterfaceAlias 'Ethernet' -RestoreDhcp
```

Key parameters (see `Get-Help .\Set-AdamIp.ps1 -Full`): `-NewIp` (default
`192.168.1.36`), `-CurrentIp` (default `10.0.0.1`), `-InterfaceAlias`
(auto-detected wired NIC if omitted), `-PrefixLength` (default `24`),
`-RestoreDhcp` (hand the NIC back to DHCP when done).

After it reports **SUCCESS**, point the companion at the new address in
`/etc/atspi/config.yaml` (`io.adam.host: "192.168.1.36"`) and
`sudo systemctl restart atspi`; bench-verify with `atspi-bench --host 192.168.1.36`
before it drives the switch.
