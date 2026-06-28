#!/usr/bin/env bash
#
# setup-network.sh — one-time network commissioning for the ATS-Pi.
#
# Gives the Pi a PERSISTENT static IP on the wired interface (so it keeps its
# address across reboots) and points the service at the ADAM-6060. Targets
# NetworkManager (Raspberry Pi OS Bookworm). Interactive, validated, idempotent.
#
# WHY a script and not the web UI: the atspi service is deliberately hardened
# (non-root, can't touch /etc or run nmcli). Letting the app rewrite host
# networking would undo that and risks locking out a headless box. This helper
# is the safe middle ground: a guided one-time commissioning step.
#
# Run as root, ideally with a keyboard/console attached the first time:
#     sudo ./setup-network.sh
#
# It will NOT reconfigure the interface your SSH session is on (it warns first),
# so configuring eth0 while you're on wlan0 is safe.
#
set -euo pipefail

CONFIG="${CONFIG:-/etc/atspi/config.yaml}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
ask()  { local p="$1" d="$2" r; read -rp "$(printf '\033[1m%s\033[0m [%s]: ' "$p" "$d")" r; printf '%s' "${r:-$d}"; }
is_ip()   { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
is_cidr() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]]; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo ./setup-network.sh"
command -v nmcli >/dev/null 2>&1 || die "nmcli not found — this helper targets NetworkManager (Raspberry Pi OS Bookworm). For dhcpcd-based systems, configure /etc/dhcpcd.conf instead."

# ── Identify the interface carrying this SSH session (so we don't cut it) ─────
SSH_IF=""
if [ -n "${SSH_CONNECTION:-}" ]; then
  myip="$(awk '{print $3}' <<<"$SSH_CONNECTION")"
  SSH_IF="$(ip -o -4 addr show 2>/dev/null | awk -v ip="$myip" '$4 ~ "^"ip"/" {print $2; exit}')"
fi

say "ATS-Pi network commissioning"
echo "    Each device (this Pi, the ADAM-6060, GenWatch) plugs into the LAN switch"
echo "    and gets a stable static IP on the same subnet. This sets THIS Pi's."
[ -n "$SSH_IF" ] && warn "You're connected over '$SSH_IF'. Configure a DIFFERENT wired interface (eth0) to keep this session alive."
echo

# ── Gather settings (sensible defaults; everything overridable) ──────────────
IFACE="$(ask "Wired interface to configure" "eth0")"
if [ "$IFACE" = "$SSH_IF" ]; then
  warn "'$IFACE' is your current SSH link — reconfiguring it may drop this session."
  [ "$(ask "Continue anyway?" "no")" = "yes" ] || die "aborted (pick the wired interface you are NOT connected over)"
fi

cur_cidr="$(ip -o -4 addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)"
IPADDR="$(ask "Static IP for $IFACE in CIDR (e.g. 192.168.0.51/23)" "${cur_cidr:-192.168.0.51/23}")"
is_cidr "$IPADDR" || die "IP must be CIDR like 192.168.0.51/23 (note the /23 to match your LAN)"
GATEWAY="$(ask "Gateway (router)" "192.168.1.1")"
is_ip "$GATEWAY" || die "bad gateway address"
DNSV="$(ask "DNS servers (space-separated)" "$GATEWAY 1.1.1.1")"
ADAM="$(ask "ADAM-6060 IP on the LAN (blank to skip config update)" "")"
[ -n "$ADAM" ] && { is_ip "$ADAM" || die "bad ADAM IP"; }

bind_ip="${IPADDR%/*}"
echo
say "About to apply:"
echo "    $IFACE        -> $IPADDR   gw $GATEWAY   dns [$DNSV]"
[ -n "$ADAM" ] && echo "    io.adam.host -> $ADAM        (in $CONFIG)"
[ -n "$ADAM" ] && echo "    modbus_server.host -> $bind_ip   (F3: bind to the OT IP, not 0.0.0.0)"
echo
[ "$(ask "Proceed?" "no")" = "yes" ] || die "aborted — nothing changed"

# ── Find (or create) the NetworkManager profile for this interface ───────────
PROF="$(nmcli -t -f NAME,DEVICE connection show 2>/dev/null | awk -F: -v d="$IFACE" '$2==d {print $1; exit}')"
if [ -z "$PROF" ]; then
  PROF="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
fi
if [ -z "$PROF" ]; then
  PROF="atspi-wired"
  say "Creating NetworkManager profile '$PROF'"
  nmcli connection add type ethernet ifname "$IFACE" con-name "$PROF" >/dev/null
fi
say "Using NetworkManager profile: $PROF"

nmcli connection modify "$PROF" \
  connection.interface-name "$IFACE" \
  ipv4.method manual \
  ipv4.addresses "$IPADDR" \
  ipv4.gateway "$GATEWAY" \
  ipv4.dns "$DNSV" \
  connection.autoconnect yes
say "Persistent static IP written (survives reboot)."

if nmcli connection up "$PROF" >/dev/null 2>&1; then
  say "Profile activated."
else
  warn "Could not activate now (is $IFACE plugged into the switch and the switch on?). It will apply on next plug/boot."
fi

# ── Point the service at the ADAM + bind to our OT IP (F3) ───────────────────
if [ -n "$ADAM" ] && [ -f "$CONFIG" ]; then
  if command -v /opt/atspi/venv/bin/python3 >/dev/null 2>&1; then PY=/opt/atspi/venv/bin/python3; else PY=python3; fi
  "$PY" - "$CONFIG" "$ADAM" "$bind_ip" <<'PYEOF'
import sys, os, shutil, time, yaml
cfg, adam, bind = sys.argv[1], sys.argv[2], sys.argv[3]
st = os.stat(cfg)
shutil.copy2(cfg, cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
d = yaml.safe_load(open(cfg)) or {}
d.setdefault("io", {}).setdefault("adam", {})["host"] = adam
d.setdefault("modbus_server", {})["host"] = bind
yaml.safe_dump(d, open(cfg, "w"), default_flow_style=False, sort_keys=False)
os.chmod(cfg, st.st_mode); os.chown(cfg, st.st_uid, st.st_gid)
print("    updated", cfg, "(backup saved alongside)")
PYEOF
  systemctl restart atspi 2>/dev/null && say "atspi restarted with the new config." || warn "atspi not restarted (service not enabled yet?) — restart it when ready."
fi

# ── Verify ───────────────────────────────────────────────────────────────────
echo
say "Result:"
ip -br addr show "$IFACE" 2>/dev/null || true
echo
say "Reachability check:"
if ping -c1 -W2 "$GATEWAY" >/dev/null 2>&1; then say "  gateway $GATEWAY ......... OK"; else warn "  gateway $GATEWAY ......... unreachable (cable/switch/IP?)"; fi
if [ -n "$ADAM" ]; then
  if ping -c1 -W2 "$ADAM" >/dev/null 2>&1; then say "  ADAM-6060 $ADAM ... OK"; else warn "  ADAM-6060 $ADAM ... unreachable (powered? on the switch? correct IP set on the ADAM?)"; fi
fi
echo
say "Done — this IP is now persistent across reboots."
echo "    Next: set the ADAM-6060 to $ADAM (Advantech .NET utility), put GenWatch's"
echo "    ats.host = ${bind_ip}, and (production) firewall so only GenWatch reaches :5020."
