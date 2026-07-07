@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Set ADAM-6060 IP
color 0B

rem ============================================================================
rem  Set-AdamIp.cmd  -  double-click helper to re-IP an Advantech ADAM-6060
rem
rem  Windows won't run a .ps1 on double-click and blocks it by policy; a .cmd
rem  just runs. This one self-elevates, puts your wired NIC on the right subnet
rem  with netsh (no PowerShell execution-policy issues), and verifies the ADAM.
rem
rem  It does NOT write the ADAM's IP itself - that one step is done in the
rem  Advantech Adam/Apax .NET Utility (the 6060 has no web/Modbus path for it).
rem  Everything is read-only toward the ADAM; nothing here touches its relays.
rem
rem  Defaults: ADAM 10.0.0.1  ->  192.168.1.36     (edit the SET lines to change)
rem ============================================================================

set "CURRENT=10.0.0.1"
set "NEWIP=192.168.1.36"
set "STAGE_A=10.0.0.50"
set "STAGE_B=192.168.1.50"
set "MASK=255.255.255.0"

rem --- Self-elevate to Administrator if we aren't already ---------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator rights - click "Yes" on the prompt...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
  exit /b
)

echo(
echo   ================================================================
echo    Set ADAM-6060 IP:   %CURRENT%   -^>   %NEWIP%
echo   ================================================================
echo(

rem --- Pick the wired adapter from netsh's own list (rock-solid, no quoting) --
echo Your network interfaces:
echo(
netsh interface show interface
echo(
echo Look above for the WIRED adapter whose State is "Connected" - usually
echo named "Ethernet". Type its exact Interface Name ^(or press Enter for
echo the default^). If Wi-Fi is also "Connected", turn it OFF first so the
echo checks don't route around the wired link.
echo(
set "IF=Ethernet"
set /p "IF=Wired adapter name [default: Ethernet]: "
if not defined IF set "IF=Ethernet"
echo(
echo Using wired adapter: "!IF!"

echo(
echo This changes the IPv4 settings on "!IF!". If that is your only network
echo connection you will lose other network access while this runs.
set /p GO="Proceed? [Y/N] "
if /i not "!GO!"=="Y" ( echo Aborted - no changes made. & pause & exit /b 0 )

rem ===========================  PHASE A  =====================================
echo(
echo --- Phase A: putting "!IF!" on %STAGE_A% to reach the ADAM at %CURRENT% ---
netsh interface ipv4 set address name="!IF!" static %STAGE_A% %MASK% >nul
if errorlevel 1 ( echo Failed to set %STAGE_A% on "!IF!". Run as admin / check the name. & pause & exit /b 1 )
timeout /t 3 /nobreak >nul

set "OKA="
for /l %%i in (1,1,4) do (
  if not defined OKA (
    ping -n 2 -w 1000 %CURRENT% | find "TTL=" >nul && set "OKA=1"
    if not defined OKA timeout /t 2 /nobreak >nul
  )
)
if not defined OKA (
  echo(
  echo   Could NOT reach the ADAM at %CURRENT%.
  echo   Check: cable seated, ADAM powered ^(status LED blinking^), Wi-Fi off.
  echo   If the ADAM was already re-IP'd, edit CURRENT at the top of this file.
  echo(
  pause & exit /b 1
)
echo Reachable: ADAM answers at %CURRENT%.

rem ===========================  PAUSE  =======================================
echo(
echo   ============== DO THIS NOW in the Adam/Apax .NET Utility ==============
echo     1. Click Search - the module appears at %CURRENT%
echo     2. Select it  ^(config password: 00000000^)
echo     3. Network page: Static IP = %NEWIP%, mask %MASK%, port 502
echo        ^(set a gateway only if your OT LAN has one^)
echo     4. Apply ^(re-enter 00000000^). The module reboots onto %NEWIP%.
echo   ======================================================================
echo(
pause

rem ===========================  PHASE B  =====================================
echo(
echo --- Phase B: moving "!IF!" to %STAGE_B% to verify the ADAM at %NEWIP% ---
netsh interface ipv4 set address name="!IF!" static %STAGE_B% %MASK% >nul
echo Waiting for the ADAM to finish rebooting...
timeout /t 6 /nobreak >nul

set "OKB="
for /l %%i in (1,1,6) do (
  if not defined OKB (
    ping -n 2 -w 1000 %NEWIP% | find "TTL=" >nul && set "OKB=1"
    if not defined OKB timeout /t 3 /nobreak >nul
  )
)

set "PORT=False"
if defined OKB (
  for /f %%R in ('powershell -NoProfile -Command "(Test-NetConnection -ComputerName %NEWIP% -Port 502 -WarningAction SilentlyContinue).TcpTestSucceeded"') do set "PORT=%%R"
)

echo(
if not defined OKB (
  echo   NOT REACHABLE at %NEWIP% yet. Likely causes:
  echo     - the Apply in the utility did not take ^(re-open, re-check Network page^)
  echo     - the new IP/mask are not on this subnet ^(%STAGE_B%/%MASK%^)
  echo     - still rebooting - wait 15s and re-run this file ^(it will re-verify^)
) else if /i "!PORT!"=="True" (
  echo   SUCCESS - the ADAM answers Modbus/TCP at %NEWIP%:502.
) else (
  echo   PARTIAL - %NEWIP% pings but port 502 did not confirm yet.
  echo   Give it a few seconds, then test:  Test-NetConnection %NEWIP% -Port 502
)

rem ===========================  RESTORE  =====================================
echo(
set /p DH="Hand adapter "!IF!" back to DHCP now? [Y/N] "
if /i "!DH!"=="Y" (
  netsh interface ipv4 set address name="!IF!" source=dhcp >nul
  netsh interface ipv4 set dnsservers name="!IF!" source=dhcp >nul
  echo Adapter restored to DHCP.
) else (
  echo Left "!IF!" on static %STAGE_B%. To restore later:
  echo   netsh interface ipv4 set address name="!IF!" source=dhcp
)

echo(
echo Next, on the ATS-Pi:  set io.adam.host: "%NEWIP%" in /etc/atspi/config.yaml
echo                       then:  sudo systemctl restart atspi
echo(
pause
endlocal
