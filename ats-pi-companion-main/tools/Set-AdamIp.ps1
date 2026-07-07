#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Helper for re-IP'ing an Advantech ADAM-6060 from a Windows laptop.

.DESCRIPTION
    The ADAM-6060 ships at 10.0.0.1/24 (Modbus/TCP 502). Its network identity
    can only be *written* through Advantech's Adam/Apax .NET Utility (the web
    page is a dead Java applet, and the 6060's Modbus map exposes no network
    registers). This script does NOT write the IP — it automates everything
    around that one utility step so you don't have to hand-juggle the laptop's
    NIC and can positively verify the result:

      Phase A  Put the wired NIC on the ADAM's staging subnet (10.0.0.x) and
               confirm the module is reachable (ping + TCP 502 + a read-only
               Modbus coil read that proves it's really the ADAM answering).
      PAUSE    You open the Adam/Apax .NET Utility, Search, select the module
               (config password 00000000), set Static IP + mask, Apply.
      Phase B  Move the NIC onto the target OT subnet and re-verify the module
               at its new address the same three ways.
      Restore  Optionally hand the NIC back to DHCP.

    Read-only throughout (the Modbus probe only *reads* coils). Nothing here
    changes the ADAM's outputs or the transfer switch.

.PARAMETER InterfaceAlias
    Wired adapter to reconfigure (e.g. 'Ethernet'). Auto-detected if omitted.

.PARAMETER CurrentIp
    The ADAM's present address. Default 10.0.0.1 (factory).

.PARAMETER NewIp
    The OT address to give the ADAM. Default 192.168.1.36.

.PARAMETER PrefixLength
    Subnet prefix for both subnets. Default 24 (255.255.255.0).

.PARAMETER StagingIpA
    Laptop IP used to reach the ADAM at CurrentIp. Default 10.0.0.50.

.PARAMETER StagingIpB
    Laptop IP used to reach the ADAM at NewIp. Default derived from NewIp (.50).

.PARAMETER UnitId
    Modbus slave id for the verification read. Default 1.

.PARAMETER RestoreDhcp
    Set the NIC back to DHCP when finished.

.EXAMPLE
    .\Set-AdamIp.ps1
    Uses all defaults (10.0.0.1 -> 192.168.1.36) on the auto-detected NIC.

.EXAMPLE
    .\Set-AdamIp.ps1 -NewIp 192.168.0.60 -InterfaceAlias 'Ethernet' -RestoreDhcp
#>
[CmdletBinding()]
param(
    [string]$InterfaceAlias,
    [string]$CurrentIp    = '10.0.0.1',
    [string]$NewIp        = '192.168.1.36',
    [int]   $PrefixLength = 24,
    [string]$StagingIpA   = '10.0.0.50',
    [string]$StagingIpB,
    [int]   $UnitId       = 1,
    [switch]$RestoreDhcp
)

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "[adam-ip] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[adam-ip] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[adam-ip] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[adam-ip] $m" -ForegroundColor Red; exit 1 }

# Default staging-B to the .50 host of the NewIp subnet if not supplied.
if (-not $StagingIpB) {
    $octets = $NewIp.Split('.')
    if ($octets.Count -ne 4) { Die "NewIp '$NewIp' is not a valid IPv4 address." }
    $StagingIpB = "$($octets[0]).$($octets[1]).$($octets[2]).50"
}

# ── Pick the wired adapter ────────────────────────────────────────────────
function Resolve-Adapter {
    param([string]$Alias)
    if ($Alias) {
        $a = Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue
        if (-not $a) { Die "No adapter named '$Alias'. Available: $((Get-NetAdapter | Select-Object -Expand Name) -join ', ')" }
        return $a
    }
    $cands = Get-NetAdapter -Physical | Where-Object {
        $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch 'Wi-?Fi|Wireless|Bluetooth|Virtual|Loopback'
    }
    if (-not $cands) { Die "No 'Up' wired adapter found. Plug in the cable, or pass -InterfaceAlias. Adapters: $((Get-NetAdapter | ForEach-Object {"$($_.Name)[$($_.Status)]"}) -join ', ')" }
    if ($cands.Count -gt 1) {
        Warn "Multiple wired adapters up:"
        $cands | ForEach-Object { Warn "   - $($_.Name)  ($($_.InterfaceDescription))" }
        Die "Pass -InterfaceAlias to pick one."
    }
    return $cands
}

# ── Force a static IPv4 onto the adapter (clears any prior v4 config) ──────
function Set-StaticIp {
    param([string]$Alias, [string]$Ip, [int]$Prefix)
    Info "Setting $Alias -> $Ip/$Prefix (static)"
    # Turn off DHCP first so New-NetIPAddress doesn't conflict with a lease.
    Set-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue
    Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetRoute -InterfaceAlias $Alias -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceAlias $Alias -IPAddress $Ip -PrefixLength $Prefix | Out-Null
    Set-DnsClientServerAddress -InterfaceAlias $Alias -ResetServerAddresses -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2   # let the stack settle
}

function Restore-Dhcp {
    param([string]$Alias)
    Info "Restoring $Alias to DHCP"
    Get-NetIPAddress -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetRoute -InterfaceAlias $Alias -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias $Alias -AddressFamily IPv4 -Dhcp Enabled
    Set-DnsClientServerAddress -InterfaceAlias $Alias -ResetServerAddresses -ErrorAction SilentlyContinue
}

# ── Read-only Modbus/TCP probe: FC01 read coils, prove the ADAM answers ────
function Test-AdamModbus {
    param([string]$Ip, [int]$Port = 502, [int]$Unit = 1)
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($Ip, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(2500)) { return $false }   # connect timeout
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $stream.ReadTimeout = 2500

        # MBAP header (7) + PDU: FC01 (read coils), start 0x0000, quantity 6.
        $txid = 1
        $pdu  = 0x01, 0x00, 0x00, 0x00, 0x06
        $len  = 1 + $pdu.Count                      # unit id + PDU
        [byte[]]$req = @(
            [byte](($txid -shr 8) -band 0xFF), [byte]($txid -band 0xFF),
            0x00, 0x00,                             # protocol id = 0 (Modbus)
            [byte](($len  -shr 8) -band 0xFF), [byte]($len  -band 0xFF),
            [byte]$Unit
        ) + [byte[]]$pdu
        $stream.Write($req, 0, $req.Length)
        $stream.Flush()

        $buf = New-Object byte[] 64
        $n = $stream.Read($buf, 0, $buf.Length)
        # Valid response: >=9 bytes and the function byte echoes 0x01 (no 0x80 error bit).
        return ($n -ge 9 -and $buf[7] -eq 0x01)
    } catch {
        return $false
    } finally {
        if ($client) { $client.Close() }
    }
}

function Test-Reach {
    param([string]$Ip, [string]$Label)
    # Positional target: works on both Windows PowerShell 5.1 (-ComputerName)
    # and PowerShell 7 (-TargetName), which renamed the parameter.
    $ping = Test-Connection $Ip -Count 3 -Quiet -ErrorAction SilentlyContinue
    $port = $false
    try { $port = (Test-NetConnection -ComputerName $Ip -Port 502 -WarningAction SilentlyContinue).TcpTestSucceeded } catch {}
    $mb = if ($port) { Test-AdamModbus -Ip $Ip -Unit $UnitId } else { $false }
    Info ("{0,-8} {1,-15}  ping={2}  tcp/502={3}  modbus={4}" -f $Label, $Ip,
          ($(if($ping){'OK'}else{'--'})), ($(if($port){'OK'}else{'--'})), ($(if($mb){'OK'}else{'--'})))
    return [pscustomobject]@{ Ping=$ping; Port=$port; Modbus=$mb }
}

# ── Run ────────────────────────────────────────────────────────────────────
$adapter = Resolve-Adapter -Alias $InterfaceAlias
$if = $adapter.Name
Info "Using wired adapter: $if  ($($adapter.InterfaceDescription))"

# Warn if Wi-Fi is up — it can make Windows route around the wired NIC.
$wifi = Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -match 'Wi-?Fi|Wireless' }
if ($wifi) { Warn "Wi-Fi adapter '$($wifi.Name)' is up — if reachability fails, disable it: Disable-NetAdapter -Name '$($wifi.Name)' -Confirm:`$false" }

Warn "This will change the IPv4 settings on '$if'. If that adapter is your only network path you'll lose other connectivity while the script runs."
$go = Read-Host "Proceed? [y/N]"
if ($go -notmatch '^(y|yes)$') { Die "Aborted — no changes made." }

# Remember original config so RestoreDhcp / manual restore is possible.
$origDhcp = (Get-NetIPInterface -InterfaceAlias $if -AddressFamily IPv4).Dhcp
Info "Original DHCP state on ${if}: $origDhcp"

try {
    # ── Phase A: reach the ADAM at its current address ──────────────────────
    Info "── Phase A: staging on the ADAM's current subnet ──"
    Set-StaticIp -Alias $if -Ip $StagingIpA -Prefix $PrefixLength
    $a = Test-Reach -Ip $CurrentIp -Label 'CURRENT'
    if (-not $a.Ping) {
        Warn "Can't ping $CurrentIp. Check: cable seated, ADAM powered (status LED blinking), Wi-Fi disabled."
        Warn "If the module was already re-IP'd, re-run with -CurrentIp <its current address>."
        Die  "ADAM not reachable at $CurrentIp — fix the above and re-run."
    }
    Ok "ADAM reachable at $CurrentIp."
    if (-not $a.Modbus) { Warn "Ping worked but the Modbus read didn't — port 502 may be firewalled on the ADAM, or it isn't a 6060. Continuing." }

    # ── PAUSE for the one manual step in the utility ────────────────────────
    Write-Host ""
    Write-Host "  ================ DO THIS NOW in the Adam/Apax .NET Utility ================" -ForegroundColor White
    Write-Host "    1. Click Search - the module appears at $CurrentIp" -ForegroundColor White
    Write-Host "    2. Select it (config password: 00000000)" -ForegroundColor White
    Write-Host "    3. Network page -> Static IP = $NewIp, mask /$PrefixLength" -ForegroundColor White
    Write-Host "       (leave Modbus port 502; set gateway only if your OT LAN has one)" -ForegroundColor White
    Write-Host "    4. Apply (re-enter 00000000). The module reboots onto the new IP." -ForegroundColor White
    Write-Host "  ===========================================================================" -ForegroundColor White
    Write-Host ""
    Read-Host "Press Enter AFTER you've applied the new IP in the utility"

    # ── Phase B: verify the ADAM at its new address ─────────────────────────
    Info "── Phase B: moving the NIC to the target subnet to verify ──"
    Set-StaticIp -Alias $if -Ip $StagingIpB -Prefix $PrefixLength
    Info "Waiting a few seconds for the ADAM to finish rebooting..."
    Start-Sleep -Seconds 6

    $b = $null
    for ($try = 1; $try -le 5; $try++) {
        $b = Test-Reach -Ip $NewIp -Label "NEW(try$try)"
        if ($b.Ping -and $b.Port) { break }
        Start-Sleep -Seconds 3
    }

    if ($b.Ping -and $b.Port -and $b.Modbus) {
        Ok "SUCCESS — ADAM is answering Modbus at $NewIp:502."
        Ok "Next: on the ATS-Pi set io.adam.host: `"$NewIp`" and: sudo systemctl restart atspi"
    } elseif ($b.Ping) {
        Warn "PARTIAL — $NewIp pings but Modbus/502 didn't confirm. Give it a moment and re-test:"
        Warn "  Test-NetConnection $NewIp -Port 502"
    } else {
        Warn "NOT REACHABLE at $NewIp yet. Common causes:"
        Warn "  - the Apply didn't take (re-open the utility, re-check the Network page)"
        Warn "  - the new IP/mask don't match this NIC's subnet ($StagingIpB/$PrefixLength)"
        Warn "  - the module is still rebooting — wait 15 s and re-run Phase B checks"
    }
}
finally {
    if ($RestoreDhcp) {
        Restore-Dhcp -Alias $if
    } else {
        Warn "NIC '$if' left on a static IP ($StagingIpB). To hand it back to DHCP:"
        Warn "  Set-NetIPInterface -InterfaceAlias '$if' -AddressFamily IPv4 -Dhcp Enabled; Get-NetIPAddress -InterfaceAlias '$if' -AddressFamily IPv4 | Remove-NetIPAddress -Confirm:`$false"
    }
}
