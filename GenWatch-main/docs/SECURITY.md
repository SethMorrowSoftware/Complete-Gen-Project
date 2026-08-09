# Exposing the console to the internet

This document is the checklist for putting the Castle Generator Monitor on
a public address, and the reasoning behind each control. Read it before you
forward a port.

The short version:

1. Give every operator their own account (`genwatch useradd`).
2. Put TLS in front and set `security.public_exposure: true`.
3. Enroll two-factor on every account, then set `auth.require_totp: true`.
4. Turn on the Slack sign-in alerts so you find out about attempts.

---

## 0. What this thing can do if someone gets in

Worth being blunt about the stakes, because they set the bar for
everything below. An authenticated operator can start and stop a 100 kW
diesel generator and command a 600 A transfer switch. The hardware safeties
at the H-100 panel remain primary — the key switch must be in AUTO for a
remote command to engage at all, and the panel wins every disagreement —
but "the attacker can only shut down your generator during an outage" is
still a bad day for a site that runs on it.

Treat console access the way you'd treat a key to the generator room.

---

## 1. Accounts

Every operator gets a named account with their own password. There is no
shared login.

```bash
sudo -u genwatch genwatch useradd alice --role admin
sudo -u genwatch genwatch useradd dave  --role operator
sudo -u genwatch genwatch useradd noc   --role viewer
sudo -u genwatch genwatch userlist
```

| Role | Can do |
|---|---|
| `viewer` | Live view, history, events. Read-only. |
| `operator` | The above, plus start / stop / exercise / transfer and alarm ack — everything that moves hardware. |
| `admin` | The above, plus configuration, register map, and user management. |

Give people the lowest role that lets them do their job. A monitoring
dashboard on a wall does not need to be able to stop the generator.

Admins can also manage accounts from **Settings → Users**. Both paths go
through the same service layer, so the same guard rails apply: you cannot
delete, disable, or demote the last enabled admin from either one. That
restriction exists because the recovery path for "nobody can log in" is a
drive to the site.

### Upgrading from the single shared password

Deployments that predate named accounts authenticate against
`auth.admin_password_hash` in `config.yaml`. On first boot after the
upgrade, that hash is migrated into a real account — username from
`auth.bootstrap_username` (default `admin`), role `admin`. **Your existing
password keeps working; you now type a username with it.**

Afterwards, create per-person accounts and clear `admin_password_hash` from
`config.yaml`. It is never consulted again once accounts exist, so leaving
it there just means a stale credential sitting on disk.

### If you lock yourself out

The CLI talks to the database directly and does not need a login:

```bash
sudo -u genwatch genwatch userunlock alice     # clear a failed-login lockout
sudo -u genwatch genwatch userpasswd alice     # set a new password
sudo -u genwatch genwatch usertotp-off alice   # lost phone: clear 2FA
sudo -u genwatch genwatch useradd rescue --role admin
```

---

## 2. Passwords

New passwords must be at least 12 characters (`auth.password_min_length`)
and mix three of lowercase / uppercase / digits / symbols — or be 20+
characters, in which case the character-class rule is waived, because a
long passphrase beats a short scrambled string and is far likelier to be
typed correctly at 2 a.m. The policy also rejects common passwords,
keyboard runs, and any password containing the username.

Hashing is bcrypt at cost 12 (~250 ms on a Pi 5). That is deliberately slow
enough to make an offline attack on a stolen database expensive, and fast
enough that the rate limiter — not the hash — is what bounds online
guessing.

When an admin resets someone's password, the account is flagged
`must_change_password` by default. Until it's replaced, **every** endpoint
except the change-password one returns 403. A password an admin picked and
sent over chat is a shared secret; this makes replacing it mandatory rather
than optional.

---

## 3. Brute force

Two independent layers, because each covers the other's blind spot:

- **Per-IP token bucket** — `auth.login_rate_burst` attempts, then one more
  every `auth.login_rate_refill_seconds`. Stops a single host hammering the
  endpoint. Sidesteppable by a botnet.
- **Per-account lockout** — after `auth.lockout_threshold` failures the
  account locks for `auth.lockout_seconds`, doubling on each further
  lockout up to `auth.lockout_max_seconds`. This follows the *target*, so
  rotating source addresses doesn't help.

Escalation matters in both directions: a fixed window is waited out by a
patient script, and an unbounded one becomes a denial of service against
the operator during an outage — which for a generator console is its own
safety problem. Hence doubling with a cap.

Failed second factors count toward the lockout too. Without that, someone
holding a stolen password gets unlimited attempts at six digits.

### Account enumeration

Unknown username, wrong password, and disabled account all return exactly
the same response: `401 {"code": "invalid_credentials", "message":
"invalid username or password"}`. The unknown-username path also burns a
real bcrypt verify against a throwaway hash, so it takes the same wall
time as a real one — otherwise the timing leaks precisely what the response
body refuses to.

The "account locked" message is only shown to a request that already
supplied the *correct* password. A guesser gets the generic failure.

---

## 4. Two-factor authentication

The single control that still holds when a password is reused, phished, or
leaked in someone else's breach. On a public address, turn it on.

```bash
# per account, interactively — prints a QR-scannable otpauth:// link
sudo -u genwatch genwatch usertotp alice
```

or from the console: **Settings → My Account → Set up two-factor**.

Standard TOTP (RFC 6238, SHA-1, 6 digits, 30 s) so every authenticator app
works — Google Authenticator, 1Password, Aegis, Authy, Yubico, Bitwarden.
Enrollment isn't active until you confirm with a live code, so a QR scan
that silently failed can't lock you out.

Codes are single-use: the counter that matched is recorded, and anything at
or below it is refused. A code read over your shoulder or captured by a
phishing proxy is dead the moment you use it.

**Recovery codes.** Ten single-use codes are shown once at enrollment and
stored only as hashes. Print them or put them in the site binder. A lost
phone with no recovery code means an admin clearing 2FA on the server,
which means shell access, which during an outage means a drive.

Once everyone is enrolled:

```yaml
auth:
  require_totp: true
```

Accounts that haven't enrolled are then **refused**, not exempted — a
policy that silently waves through the accounts which ignored it isn't a
policy. Enroll first, flip second.

---

## 5. Sessions

Sessions are recorded server-side and keyed by the token's `jti`. That
buys three things a bare signed token cannot:

- **Logout actually revokes.** The cookie stops working immediately; it
  isn't merely dropped by the browser and trusted to disappear.
- **Idle timeout.** `auth.idle_timeout_minutes` (default 60) on top of the
  absolute `auth.session_hours`. "Idle" means the *operator* is idle, not
  the socket: the live WebSocket streaming telemetry into an unattended
  tab deliberately does **not** refresh the window, or the Live view —
  the one page people leave open — would keep a session alive
  indefinitely. The console sends a keepalive on real user input
  (pointer, keyboard, tab focus), throttled to one request per five
  minutes.
- **Visible, killable sessions.** Each operator sees their own in
  **Settings → My Account**; an admin can sign an account out everywhere
  from **Settings → Users** — the lost-laptop and departing-contractor
  paths.

A password change bumps the account's token epoch, which invalidates every
existing session for that account instantly, including the WebSocket feed.
Disabling an account does the same.

The session cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` whenever
the request arrived over HTTPS. Over HTTPS it also gets the `__Host-`
name prefix, which browsers enforce as "Secure, Path=/, no Domain" — that
stops a sibling hostname (or an attacker with a foothold on a subdomain)
from planting a session cookie on the console.

---

## 6. Transport

**Do not expose plain HTTP.** Terminate TLS in front of the service. Two
setups that work with no further configuration, because both forward
`X-Forwarded-Proto` from loopback, which uvicorn already trusts:

```bash
# Tailscale — private by default, valid cert, no port forwarding at all.
# The best option if everyone who needs access can run Tailscale.
sudo tailscale serve --bg 8000

# Caddy — public hostname with an automatic Let's Encrypt certificate.
# /etc/caddy/Caddyfile:
#   genwatch.example.com {
#       reverse_proxy 127.0.0.1:8000
#   }
```

Then:

```yaml
security:
  public_exposure: true    # implies require_https
```

With HTTPS enforced, GETs redirect (308) and writes are refused outright —
a password must never be re-sent over a cleartext connection just because
the client got the scheme wrong.

If your proxy is not on loopback, set `GENWATCH_TRUSTED_PROXIES` in the
systemd unit to its address. Otherwise `X-Forwarded-For` is ignored, and
every request appears to come from the proxy — which silently defeats the
per-IP rate limiter, the audit source IP, and the allowlist below.

---

## 7. `security.public_exposure`

Setting this to `true` is you telling the service it is reachable from the
internet. That claim changes what counts as a misconfiguration, so the
service **refuses to start** on:

- `mock: true` (publishing synthesized telemetry as if it were real)
- `auth.cookie_secure: false` (session cookie in the clear)
- `cors_origins` containing `*` with credentialed CORS
- `auth.lockout_seconds: 0` (account lockout disabled)
- no enabled admin account

and warns loudly about: HTTPS not enforced, 2FA not required, no idle
timeout, long sessions, response headers disabled, and a legacy shared
password still sitting in `config.yaml`.

Errors are the settings that would be actively dangerous. The rest are
warnings, because refusing to boot a generator monitor is its own hazard —
the failure mode of "won't start" is an operator with no visibility during
an outage.

`genwatch doctor` prints the same posture without starting the service.

---

## 8. Response hardening

With `security.headers_enabled` (default on), every response carries:

| Header | Why |
|---|---|
| `Content-Security-Policy` | `frame-ancestors 'none'`, `script-src 'self'`, no `object-src`. Blocks clickjacking and script injection. `connect-src` is built per-request from the Host header so the live WebSocket works without opening it to every host on the internet. |
| `X-Frame-Options: DENY` | Clickjacking, for anything that predates CSP. |
| `X-Content-Type-Options: nosniff` | No MIME guessing. |
| `Referrer-Policy: no-referrer` | Console URLs never leak to third parties. |
| `Permissions-Policy` | Camera, mic, geolocation, USB all off. |
| `Cross-Origin-Opener-Policy` / `-Resource-Policy` | Cross-origin isolation. |
| `X-Robots-Tag: noindex` | A public deployment *will* be crawled. |
| `Cache-Control: no-store` on `/api/*` | No intermediary holds telemetry or session state. |
| `Strict-Transport-Security` | HTTPS requests only, so it can never brick a plain-HTTP LAN deployment. |

CSRF is defended separately and was already in place: `SameSite=Strict`
cookies, an Origin/Referer check on every non-safe `/api/*` request, a
required `X-Requested-With` header when both are absent, and two-step
confirm tokens on every control command.

### Optional IP allowlist

```yaml
security:
  ip_allowlist:
    - 203.0.113.4
    - 198.51.100.0/24
```

Requests from anywhere else are refused before any handler runs. Loopback
always passes, so a typo here costs you a `ssh` session rather than a site
visit. Only meaningful if the real client IP reaches the service — see the
trusted-proxy note above.

---

## 9. Know when someone is trying

Turn on the sign-in alerts (**Settings → Alerts · Slack**, or
`config.yaml`):

```yaml
slack:
  alert_on_login_failure: true
  alert_on_account_lockout: true
  alert_on_user_change: true
  channel_security: "#genwatch-security"   # optional, admin-only channel
```

Failed sign-ins are deduplicated per account per minute, so a spray attempt
can't flood the channel or push real generator alarms out of the send
queue; the lockout alert is the one that carries the signal. Account
changes — created, deleted, role changed, password reset, 2FA turned on or
off — post as they happen.

Everything is in the audit log regardless (`operator`, `action`, `detail`,
`result`, source IP), retained per `retention.audit_days` (default: forever).

---

## 10. The rest of the box

The console is one way in; don't leave an easier one beside it.

- SSH: keys only, no password auth, ideally not exposed at all.
- Keep the Pi patched (`unattended-upgrades`).
- The service already runs as an unprivileged `genwatch` user with systemd
  hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, all
  capabilities dropped) — don't relax it.
- `/etc/genwatch/config.yaml` is mode 0640 and holds the JWT secret, the
  Slack bot token, and the MQTT password. Back it up somewhere encrypted.
- Rotating `auth.jwt_secret` invalidates every session immediately. That's
  the emergency stop if you think a token has leaked.

---

## Reporting a problem

Found a security issue in this software? Please report it privately to the
repository owner rather than opening a public issue.
