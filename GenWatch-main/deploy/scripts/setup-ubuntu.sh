#!/usr/bin/env bash
#
# setup-ubuntu.sh — one-shot GenWatch setup on an Ubuntu Server, TCP transport.
#
# Wraps the standard installer and then configures /etc/genwatch/config.yaml for:
#   - real comms (mock: false)
#   - transport: tcp  → a serial-to-Ethernet gateway (Lantronix/Moxa/ser2net)
#                       wired to the H-100's RS-232 PC port
#   - the ATS-Pi companion (optional)
#   - the admin password
# …then starts the service and runs diagnostics.
#
# It is interactive (sensible defaults, everything overridable) and idempotent —
# safe to re-run. Every value can also be supplied via an environment variable of
# the same name to run non-interactively (e.g. GW_HOST=10.0.0.5 sudo -E ...).
#
# Run as root from the repository root:
#     sudo deploy/scripts/setup-ubuntu.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$REPO_ROOT/deploy/scripts/install.sh"
CONFIG="${CONFIG:-/etc/genwatch/config.yaml}"
VENV_PY=/opt/genwatch/venv/bin/python3

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
ask()  { local p="$1" d="$2" r; read -rp "$(printf '\033[1m%s\033[0m [%s]: ' "$p" "$d")" r; printf '%s' "${r:-$d}"; }
is_ip()   { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
is_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )); }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo deploy/scripts/setup-ubuntu.sh"
[ -f "$INSTALLER" ]  || die "Can't find the installer at $INSTALLER — run this from the GenWatch repo."

say "GenWatch on Ubuntu — TCP setup"
echo "    This installs GenWatch and points it at the H-100 over a serial-to-Ethernet"
echo "    gateway (raw-TCP), plus the ATS-Pi companion. Press Enter to accept defaults."
echo

# ── Gather settings ──────────────────────────────────────────────────────────
GW_HOST="$(ask "H-100 gateway IP (serial-to-Ethernet bridge)" "${GW_HOST:-192.168.1.249}")"
is_ip "$GW_HOST" || die "bad gateway IP: $GW_HOST"
GW_PORT="$(ask "Gateway raw-TCP port" "${GW_PORT:-10001}")"
is_port "$GW_PORT" || die "bad port: $GW_PORT"
SLAVE="$(ask "H-100 Modbus slave id (factory default 100)" "${SLAVE:-100}")"
[[ "$SLAVE" =~ ^[0-9]+$ ]] || die "slave id must be a number"

ATS_ENABLED="$(ask "Integrate the ATS-Pi companion? (yes/no)" "${ATS_ENABLED:-yes}")"
ATS_HOST=""; ATS_PORT=""; ATS_UNIT=""
if [[ "${ATS_ENABLED,,}" == y* ]]; then
  ATS_HOST="$(ask "  ATS-Pi IP" "${ATS_HOST:-192.168.1.250}")"
  is_ip "$ATS_HOST" || die "bad ATS-Pi IP: $ATS_HOST"
  ATS_PORT="$(ask "  ATS-Pi port" "${ATS_PORT:-5020}")"
  is_port "$ATS_PORT" || die "bad ATS-Pi port: $ATS_PORT"
  ATS_UNIT="$(ask "  ATS-Pi expected_unit_id (must equal its site.unit_id)" "${ATS_UNIT:-23}")"
  [[ "$ATS_UNIT" =~ ^[0-9]+$ ]] || die "expected_unit_id must be a number"
fi

# ── Admin password (no echo). Blank = leave whatever is already configured. ──
ADMIN_PW="${ADMIN_PW:-}"
if [ -z "$ADMIN_PW" ]; then
  echo
  read -rsp "$(printf '\033[1mAdmin password\033[0m (blank = keep existing): ')" ADMIN_PW; echo
  if [ -n "$ADMIN_PW" ]; then
    read -rsp "$(printf '\033[1mConfirm password\033[0m: ')" PW2; echo
    [ "$ADMIN_PW" = "$PW2" ] || die "passwords did not match"
  fi
fi

echo
say "About to apply:"
echo "    transport      -> tcp   ($GW_HOST:$GW_PORT, RTU framing)"
echo "    modbus.slave   -> $SLAVE"
[ -n "$ATS_HOST" ] && echo "    ats            -> $ATS_HOST:$ATS_PORT  expected_unit_id=$ATS_UNIT" || echo "    ats            -> disabled"
echo "    admin password -> $( [ -n "$ADMIN_PW" ] && echo 'will be set' || echo 'unchanged' )"
echo
[ "$(ask "Proceed?" "yes")" = "yes" ] || die "aborted — nothing changed"

# ── 1. Run the standard installer (Ubuntu needs the OS override) ─────────────
say "Running the GenWatch installer (this builds the UI + venv; takes a few minutes)…"
GENWATCH_ALLOW_UNSUPPORTED_OS=1 bash "$INSTALLER"

[ -f "$CONFIG" ]  || die "installer did not create $CONFIG — check its output above."
[ -x "$VENV_PY" ] || die "venv python missing at $VENV_PY — installer may have failed."

# ── 2. Hash the admin password (if one was given) ───────────────────────────
ADMIN_HASH=""
if [ -n "$ADMIN_PW" ]; then
  say "Hashing the admin password…"
  # Extract just the bcrypt hash, regardless of any surrounding text.
  ADMIN_HASH="$(genwatch hash "$ADMIN_PW" 2>/dev/null | grep -oE '\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}' | head -1 || true)"
  [ -n "$ADMIN_HASH" ] || die "could not generate a password hash via 'genwatch hash'."
fi

# ── 3. Write the config (backup first; preserves owner/mode + jwt_secret) ────
say "Configuring $CONFIG (a timestamped backup is saved alongside)…"
GW_HOST="$GW_HOST" GW_PORT="$GW_PORT" SLAVE="$SLAVE" \
ATS_ENABLED="$ATS_ENABLED" ATS_HOST="$ATS_HOST" ATS_PORT="$ATS_PORT" ATS_UNIT="$ATS_UNIT" \
ADMIN_HASH="$ADMIN_HASH" CONFIG="$CONFIG" \
"$VENV_PY" - <<'PY'
import os, time, shutil, yaml
cfg = os.environ["CONFIG"]
st  = os.stat(cfg)
shutil.copy2(cfg, cfg + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
d = yaml.safe_load(open(cfg)) or {}

d["mock"] = False
d["transport"] = "tcp"
mt = d.setdefault("modbus_tcp", {})
mt["host"] = os.environ["GW_HOST"]
mt["port"] = int(os.environ["GW_PORT"])
mt["framer"] = "rtu"
d.setdefault("modbus", {})["slave"] = int(os.environ["SLAVE"])

if os.environ.get("ATS_ENABLED", "").lower().startswith("y"):
    a = d.setdefault("ats", {})
    a["enabled"] = True
    a["host"] = os.environ["ATS_HOST"]
    a["port"] = int(os.environ["ATS_PORT"])
    a["framer"] = "socket"
    a.setdefault("slave", 1)
    a["expected_unit_id"] = int(os.environ["ATS_UNIT"])

h = os.environ.get("ADMIN_HASH", "")
if h:
    d.setdefault("auth", {})["admin_password_hash"] = h

yaml.safe_dump(d, open(cfg, "w"), default_flow_style=False, sort_keys=False)
os.chmod(cfg, st.st_mode); os.chown(cfg, st.st_uid, st.st_gid)
print("    config written.")
PY

# ── 4. Start + verify ────────────────────────────────────────────────────────
say "Starting genwatch…"
systemctl restart genwatch
sleep 1
if systemctl is-active --quiet genwatch; then
  say "genwatch is active (running)."
else
  warn "genwatch did not start — showing the last log lines:"
  journalctl -u genwatch -n 20 --no-pager || true
  die "service failed to start (often a missing admin password or unreachable gateway). Fix and re-run."
fi

echo
say "Reachability checks:"
if command -v nc >/dev/null 2>&1; then
  if nc -z -w3 "$GW_HOST" "$GW_PORT" 2>/dev/null; then say "  gateway $GW_HOST:$GW_PORT ... OK"; else warn "  gateway $GW_HOST:$GW_PORT ... not reachable (bridge powered + raw-TCP/Always mode?)"; fi
  if [ -n "$ATS_HOST" ]; then
    if nc -z -w3 "$ATS_HOST" "$ATS_PORT" 2>/dev/null; then say "  ATS-Pi $ATS_HOST:$ATS_PORT ... OK"; else warn "  ATS-Pi $ATS_HOST:$ATS_PORT ... not reachable (powered + on the LAN + serving?)"; fi
  fi
else
  warn "  'nc' not installed — skipping socket checks (apt-get install -y netcat-openbsd to enable)."
fi

echo
say "Diagnostics (genwatch doctor):"
genwatch doctor || warn "doctor reported issues — review above (link may still be coming up)."

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Done."
[ -n "$HOST_IP" ] && say "Browse to:  http://${HOST_IP}:8000"
say "Logs:       journalctl -u genwatch -e"
say "Re-run safely any time:  sudo deploy/scripts/setup-ubuntu.sh"
