#!/usr/bin/env bash
#
# setup-network.sh — one-time network commissioning for the GenWatch Pi.
#
# Gives the Pi a PERSISTENT static IP on the wired interface (so it keeps its
# address across reboots) and points GenWatch at the ATS-Pi. Targets
# NetworkManager (Raspberry Pi OS Bookworm). Interactive, validated, idempotent.
#
# Host networking stays in the OS (not the web UI) on purpose: the genwatch
# service is hardened/non-root and a web form that changes the box's own IP is a
# lockout foot-gun. This guided helper is the safe one-time commissioning step.
#
# Run as root, ideally with a console attached the first time:
#     sudo deploy/scripts/setup-network.sh
#
# It will NOT reconfigure the interface your SSH session is on (it warns first).
#
set -euo pipefail

CONFIG="${CONFIG:-/etc/genwatch/config.yaml}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
ask()  { local p="$1" d="$2" r; read -rp "$(printf '\033[1m%s\033[0m [%s]: ' "$p" "$d")" r; printf '%s' "${r:-$d}"; }
is_ip()   { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
is_cidr() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]]; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo deploy/scripts/setup-network.sh"
command -v nmcli >/dev/null 2>&1 || die "nmcli not found — this helper targets NetworkManager (Bookworm). For dhcpcd, edit /etc/dhcpcd.conf instead."

SSH_IF=""
if [ -n "${SSH_CONNECTION:-}" ]; then
  myip="$(awk '{print $3}' <<<"$SSH_CONNECTION")"
  SSH_IF="$(ip -o -4 addr show 2>/dev/null | awk -v ip="$myip" '$4 ~ "^"ip"/" {print $2; exit}')"
fi

say "GenWatch network commissioning"
echo "    Each device (this Pi, the ATS-Pi, the ADAM) plugs into the LAN switch on"
echo "    one subnet, each with a stable static IP. This sets THIS Pi's."
[ -n "$SSH_IF" ] && warn "You're connected over '$SSH_IF'. Configure a DIFFERENT wired interface (eth0) to keep this session alive."
echo

IFACE="$(ask "Wired interface to configure" "eth0")"
if [ "$IFACE" = "$SSH_IF" ]; then
  warn "'$IFACE' is your current SSH link — reconfiguring it may drop this session."
  [ "$(ask "Continue anyway?" "no")" = "yes" ] || die "aborted"
fi

cur_cidr="$(ip -o -4 addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)"
IPADDR="$(ask "Static IP for $IFACE in CIDR (e.g. 192.168.0.52/23)" "${cur_cidr:-192.168.0.52/23}")"
is_cidr "$IPADDR" || die "IP must be CIDR like 192.168.0.52/23 (note the /23 to match your LAN)"
GATEWAY="$(ask "Gateway (router)" "192.168.1.1")"
is_ip "$GATEWAY" || die "bad gateway address"
DNSV="$(ask "DNS servers (space-separated)" "$GATEWAY 1.1.1.1")"
ATSHOST="$(ask "ATS-Pi IP on the LAN (blank to skip config update)" "")"
[ -n "$ATSHOST" ] && { is_ip "$ATSHOST" || die "bad ATS-Pi IP"; }
ATSUNIT=""
[ -n "$ATSHOST" ] && ATSUNIT="$(ask "ATS-Pi expected_unit_id (must equal its site.unit_id)" "23")"

echo
say "About to apply:"
echo "    $IFACE    -> $IPADDR   gw $GATEWAY   dns [$DNSV]"
[ -n "$ATSHOST" ] && echo "    ats.host -> $ATSHOST:5020  expected_unit_id=$ATSUNIT   (in $CONFIG)"
echo
[ "$(ask "Proceed?" "no")" = "yes" ] || die "aborted — nothing changed"

PROF="$(nmcli -t -f NAME,DEVICE connection show 2>/dev/null | awk -F: -v d="$IFACE" '$2==d {print $1; exit}')"
[ -z "$PROF" ] && PROF="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
if [ -z "$PROF" ]; then PROF="genwatch-wired"; say "Creating profile '$PROF'"; nmcli connection add type ethernet ifname "$IFACE" con-name "$PROF" >/dev/null; fi
say "Using NetworkManager profile: $PROF"

nmcli connection modify "$PROF" \
  connection.interface-name "$IFACE" \
  ipv4.method manual \
  ipv4.addresses "$IPADDR" \
  ipv4.gateway "$GATEWAY" \
  ipv4.dns "$DNSV" \
  connection.autoconnect yes
say "Persistent static IP written (survives reboot)."
nmcli connection up "$PROF" >/dev/null 2>&1 && say "Profile activated." || warn "Could not activate now ($IFACE on the switch?). It will apply on next plug/boot."

if [ -n "$ATSHOST" ] && [ -f "$CONFIG" ]; then
  if command -v /opt/genwatch/venv/bin/python3 >/dev/null 2>&1; then PY=/opt/genwatch/venv/bin/python3; else PY=python3; fi
  "$PY" - "$CONFIG" "$ATSHOST" "$ATSUNIT" <<'PYEOF'
import sys, os, shutil, time, yaml
cfg, host, unit = sys.argv[1], sys.argv[2], sys.argv[3]
st = os.stat(cfg)
shutil.copy2(cfg, cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
d = yaml.safe_load(open(cfg)) or {}
ats = d.setdefault("ats", {})
ats["enabled"] = True
ats["host"] = host
ats.setdefault("port", 5020)
ats.setdefault("framer", "socket")
ats.setdefault("slave", 1)
if unit:
    ats["expected_unit_id"] = int(unit)
yaml.safe_dump(d, open(cfg, "w"), default_flow_style=False, sort_keys=False)
os.chmod(cfg, st.st_mode); os.chown(cfg, st.st_uid, st.st_gid)
print("    updated", cfg, "(backup saved alongside)")
PYEOF
  systemctl restart genwatch 2>/dev/null && say "genwatch restarted with the new config." || warn "genwatch not restarted — restart it when ready."
fi

echo
say "Result:"
ip -br addr show "$IFACE" 2>/dev/null || true
echo
say "Reachability check:"
if ping -c1 -W2 "$GATEWAY" >/dev/null 2>&1; then say "  gateway $GATEWAY ......... OK"; else warn "  gateway $GATEWAY ......... unreachable (cable/switch/IP?)"; fi
if [ -n "$ATSHOST" ]; then
  if timeout 2 bash -c "</dev/tcp/$ATSHOST/5020" 2>/dev/null; then say "  ATS-Pi $ATSHOST:5020 ... OK"; else warn "  ATS-Pi $ATSHOST:5020 ... not reachable (ATS-Pi powered + on the switch + serving?)"; fi
fi
echo
say "Done — this IP is now persistent across reboots."
