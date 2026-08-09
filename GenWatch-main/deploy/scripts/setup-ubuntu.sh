#!/usr/bin/env bash
#
# setup-ubuntu.sh — one-shot GenWatch setup on an Ubuntu Server, TCP transport.
#
# Wraps the standard installer and then configures /etc/genwatch/config.yaml for:
#   - real comms (mock: false)
#   - transport: tcp  → a serial-to-Ethernet gateway (Lantronix/Moxa/ser2net)
#                       wired to the H-100's RS-232 PC port
#   - the ATS-Pi companion (optional)
#   - the first admin account (username + password)
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

# ── MQTT status publishing (optional) ────────────────────────────────────────
# Publishes ON/OFF to a status topic on every utility↔generator transition.
MQTT_ENABLED="$(ask "Publish generator status to an MQTT broker? (yes/no)" "${MQTT_ENABLED:-no}")"
MQTT_HOST=""; MQTT_PORT=""; MQTT_TOPIC=""
if [[ "${MQTT_ENABLED,,}" == y* ]]; then
  MQTT_HOST="$(ask "  MQTT broker host/IP" "${MQTT_HOST:-127.0.0.1}")"
  MQTT_PORT="$(ask "  MQTT broker port" "${MQTT_PORT:-1883}")"
  is_port "$MQTT_PORT" || die "bad MQTT port: $MQTT_PORT"
  MQTT_TOPIC="$(ask "  Status topic" "${MQTT_TOPIC:-facility/generator/status}")"
fi

# ── First admin account. Operators sign in with their own username and
# ── password; accounts live in the service database, not in config.yaml.
# ── A blank password leaves existing accounts untouched.
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PW="${ADMIN_PW:-}"
if [ -z "$ADMIN_PW" ]; then
  echo
  ADMIN_USER="$(ask "Admin username" "$ADMIN_USER")"
  read -rsp "$(printf '\033[1mAdmin password\033[0m (blank = keep existing accounts): ')" ADMIN_PW; echo
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
[ -n "$MQTT_HOST" ] && echo "    mqtt           -> $MQTT_HOST:$MQTT_PORT  topic=$MQTT_TOPIC" || echo "    mqtt           -> disabled"
echo "    admin account  -> $( [ -n "$ADMIN_PW" ] && echo "$ADMIN_USER (created or password reset)" || echo 'unchanged' )"
echo
[ "$(ask "Proceed?" "yes")" = "yes" ] || die "aborted — nothing changed"

# ── 1. Run the standard installer (Ubuntu needs the OS override) ─────────────
say "Running the GenWatch installer (this builds the UI + venv; takes a few minutes)…"
GENWATCH_ALLOW_UNSUPPORTED_OS=1 bash "$INSTALLER"

[ -f "$CONFIG" ]  || die "installer did not create $CONFIG — check its output above."
[ -x "$VENV_PY" ] || die "venv python missing at $VENV_PY — installer may have failed."

# ── 3. Write the config (backup first; preserves owner/mode + jwt_secret) ────
say "Configuring $CONFIG (a timestamped backup is saved alongside)…"
GW_HOST="$GW_HOST" GW_PORT="$GW_PORT" SLAVE="$SLAVE" \
ATS_ENABLED="$ATS_ENABLED" ATS_HOST="$ATS_HOST" ATS_PORT="$ATS_PORT" ATS_UNIT="$ATS_UNIT" \
MQTT_ENABLED="$MQTT_ENABLED" MQTT_HOST="$MQTT_HOST" MQTT_PORT="$MQTT_PORT" MQTT_TOPIC="$MQTT_TOPIC" \
CONFIG="$CONFIG" \
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

if os.environ.get("MQTT_ENABLED", "").lower().startswith("y"):
    m = d.setdefault("mqtt", {})
    m["enabled"] = True
    m["host"] = os.environ["MQTT_HOST"]
    m["port"] = int(os.environ["MQTT_PORT"])
    m["topic"] = os.environ["MQTT_TOPIC"]

yaml.safe_dump(d, open(cfg, "w"), default_flow_style=False, sort_keys=False)
os.chmod(cfg, st.st_mode); os.chown(cfg, st.st_uid, st.st_gid)
print("    config written.")
PY

# ── 3b. Create (or reset) the admin account ─────────────────────────
# Goes through the same service layer as `genwatch useradd` and the Users
# page, so the password policy and the last-admin guard rails apply here
# too. Run non-interactively via the venv so this script keeps its
# "every prompt has a matching env var" contract.
if [ -n "$ADMIN_PW" ]; then
  say "Creating the admin account '$ADMIN_USER'…"
  ADMIN_USER="$ADMIN_USER" ADMIN_PW="$ADMIN_PW" CONFIG="$CONFIG" \
  sudo -u genwatch -E "$VENV_PY" - <<'ACCOUNT_PY' || die "could not create the admin account (see the error above)."
import os, sys
from genwatch.config import load
from genwatch.db import Database
from genwatch.services.users import UserError, UserService, ensure_bootstrap_user

settings = load(os.environ["CONFIG"])
db = Database(settings.db_path)
ensure_bootstrap_user(db, settings.auth)
svc = UserService(db, settings.auth)
name, pw = os.environ["ADMIN_USER"], os.environ["ADMIN_PW"]
try:
    if svc.get(name) is None:
        svc.create(username=name, password=pw, role="admin", created_by="setup-ubuntu.sh")
        print(f"    created admin account {name!r}.")
    else:
        svc.set_password(name, pw)
        svc.set_role(name, "admin", actor="setup-ubuntu.sh")
        print(f"    reset the password for existing account {name!r}.")
except UserError as e:
    print(f"    {e}", file=sys.stderr)
    sys.exit(1)
finally:
    db.close()
ACCOUNT_PY
fi

# ── 4. Start + verify ────────────────────────────────────────────────────────
say "Starting genwatch…"
systemctl restart genwatch
sleep 1
if systemctl is-active --quiet genwatch; then
  say "genwatch is active (running)."
else
  warn "genwatch did not start — showing the last log lines:"
  journalctl -u genwatch -n 20 --no-pager || true
  die "service failed to start (often no admin account yet — run: sudo -u genwatch genwatch useradd <name> --role admin — or an unreachable gateway). Fix and re-run."
fi

echo
say "Reachability checks:"
if command -v nc >/dev/null 2>&1; then
  if nc -z -w3 "$GW_HOST" "$GW_PORT" 2>/dev/null; then say "  gateway $GW_HOST:$GW_PORT ... OK"; else warn "  gateway $GW_HOST:$GW_PORT ... not reachable (bridge powered + raw-TCP/Always mode?)"; fi
  if [ -n "$ATS_HOST" ]; then
    if nc -z -w3 "$ATS_HOST" "$ATS_PORT" 2>/dev/null; then say "  ATS-Pi $ATS_HOST:$ATS_PORT ... OK"; else warn "  ATS-Pi $ATS_HOST:$ATS_PORT ... not reachable (powered + on the LAN + serving?)"; fi
  fi
  if [ -n "$MQTT_HOST" ]; then
    if nc -z -w3 "$MQTT_HOST" "$MQTT_PORT" 2>/dev/null; then say "  MQTT broker $MQTT_HOST:$MQTT_PORT ... OK"; else warn "  MQTT broker $MQTT_HOST:$MQTT_PORT ... not reachable (broker running + listening?)"; fi
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
