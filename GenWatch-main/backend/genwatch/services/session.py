"""Session cookie handling and server-side session validation.

Shared by the HTTP dependency (api/deps.py), the login/logout endpoints
(api/auth.py) and the live WebSocket (api/ws.py) so all three agree on
exactly what a valid session is. Before this existed, "is this request
authenticated?" was answered by a JWT signature check alone, which meant
logout was advisory and a leaked cookie stayed good until it expired.

A request is authenticated only when *all* of these hold:

  1. the JWT verifies against ``auth.jwt_secret`` and hasn't expired;
  2. it carries a ``jti`` that matches a session row that is neither
     revoked nor past its absolute expiry;
  3. the account still exists, is not disabled, and its ``token_epoch``
     still matches the one baked into the token (a password change or an
     admin revoke bumps it);
  4. the session has been used within ``auth.idle_timeout_minutes``.

Any failure is a 401 and the caller is sent back to the login screen.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..db import Database
from .auth import AuthError, decode_token, issue_token, new_jti

log = logging.getLogger("genwatch.session")

# Cookie names. The ``__Host-`` prefix is a browser-enforced guarantee:
# the cookie must be Secure, Path=/, and carry no Domain attribute, which
# stops a sibling host (or an attacker with a foothold on a subdomain)
# from planting a session cookie on us. We can only use it over HTTPS, so
# plain-HTTP LAN deployments keep the plain name and both are accepted on
# read — an operator flipping TLS on doesn't get logged out mid-shift.
COOKIE_NAME = "genwatch_session"
SECURE_COOKIE_NAME = "__Host-genwatch_session"
COOKIE_NAMES = (SECURE_COOKIE_NAME, COOKIE_NAME)

# Sessions are "touched" (last_seen_at bumped) at most this often. The
# idle timeout only needs minute-ish resolution, and this keeps a busy
# dashboard — which polls status and streams a WebSocket — from turning
# every request into a SQLite write.
TOUCH_INTERVAL_S = 60.0


@dataclass
class SessionPrincipal:
    operator: str
    role: str
    uid: int
    jti: str
    must_change_password: bool = False


def cookie_name(secure: bool) -> str:
    return SECURE_COOKIE_NAME if secure else COOKIE_NAME


def read_cookie(cookies) -> str | None:
    """First session cookie present, preferring the ``__Host-`` one."""
    for name in COOKIE_NAMES:
        value = cookies.get(name)
        if value:
            return value
    return None


def resolve_cookie_secure(scheme: str, auth_cfg) -> bool:
    """Decide the ``Secure`` attribute for the session cookie.

    - Explicit override in config wins (``cookie_secure: true|false``).
    - Otherwise auto-detect from the request scheme. uvicorn honors
      ``Forwarded`` / ``X-Forwarded-Proto`` from trusted upstream hops
      (default: 127.0.0.1) and updates ``request.url.scheme``, so a Caddy
      / Tailscale-serve / nginx deployment terminating TLS on the same
      host gets Secure automatically while plain-HTTP LAN keeps working.

    The auto-detect path is intentionally lenient: someone who can spoof
    X-Forwarded-Proto can at worst make us issue a Secure cookie on an
    HTTP response, which the browser then refuses to send back over HTTP
    — a self-inflicted denial of service, not an escalation.
    """
    if getattr(auth_cfg, "cookie_secure", None) is not None:
        return bool(auth_cfg.cookie_secure)
    return scheme == "https"


def start_session(
    *,
    db: Database,
    auth_cfg,
    user: dict,
    ip: str = "",
    user_agent: str = "",
) -> tuple[str, str, float]:
    """Mint a token and record the matching session row.

    Returns ``(token, jti, expires_at)``.
    """
    jti = new_jti()
    token, jti, expires_at = issue_token(
        secret=auth_cfg.jwt_secret,
        operator=user["username"],
        role=user["role"],
        hours=auth_cfg.session_hours,
        uid=int(user["id"]),
        jti=jti,
        epoch=int(user["token_epoch"]),
    )
    db.session_create(
        jti=jti,
        user_id=int(user["id"]),
        username=user["username"],
        expires_at=expires_at,
        ip=ip,
        user_agent=user_agent,
    )
    # Opportunistic housekeeping: drop rows for sessions that expired over
    # a week ago. Login is the natural place — it's rare, already does a
    # write, and never runs on the polling hot path.
    try:
        db.sessions_prune()
    except Exception as e:  # noqa: BLE001
        log.debug("session prune skipped: %s", e)
    return token, jti, expires_at


def validate(
    *, db: Database, settings, token: str, touch: bool = True
) -> SessionPrincipal:
    """Validate a raw token. Raises :class:`AuthError` with a reason.

    ``touch=False`` checks the session without counting as activity. The
    live WebSocket uses it: a socket streaming telemetry into an
    abandoned browser tab is the machine being busy, not the operator
    being present, and letting it refresh ``last_seen_at`` would mean the
    idle timeout never fires for the one view people leave open. The
    frontend sends a real keepalive on actual user input instead.
    """
    payload = decode_token(secret=settings.auth.jwt_secret, token=token)

    jti = payload.get("jti")
    if not jti:
        # Pre-upgrade token: signed correctly but with no server-side
        # session behind it, so none of the revocation guarantees hold.
        # Refusing forces one re-login at upgrade time, which is the
        # right trade for making logout mean something.
        raise AuthError("session token predates the account upgrade — sign in again")

    row = db.session_get(jti)
    if row is None:
        raise AuthError("session not found")
    if row["revoked_at"] is not None:
        raise AuthError("session revoked")
    now = time.time()
    if float(row["expires_at"]) <= now:
        raise AuthError("session expired")

    user = db.user_get_by_id(int(row["user_id"]))
    if user is None:
        raise AuthError("account no longer exists")
    if user["disabled"]:
        raise AuthError("account disabled")
    if int(payload.get("epoch", 0)) != int(user["token_epoch"]):
        raise AuthError("session invalidated (password changed or sessions revoked)")

    idle_minutes = int(getattr(settings.auth, "idle_timeout_minutes", 0) or 0)
    if idle_minutes > 0 and (now - float(row["last_seen_at"])) > idle_minutes * 60:
        # Mark it revoked so the sessions list shows the truth and the
        # row can't be resurrected by a later request.
        db.session_revoke(jti)
        raise AuthError(f"session idle for over {idle_minutes} minutes — sign in again")

    if touch and (now - float(row["last_seen_at"])) >= TOUCH_INTERVAL_S:
        try:
            db.session_touch(jti)
        except Exception as e:  # noqa: BLE001
            log.debug("session touch failed: %s", e)

    return SessionPrincipal(
        operator=user["username"],
        role=user["role"],
        uid=int(user["id"]),
        jti=jti,
        must_change_password=bool(user["must_change_password"]),
    )
