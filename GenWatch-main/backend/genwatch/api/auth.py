"""Login, logout, and self-service account endpoints.

The login path is the single most exposed surface on this service — it is
the one endpoint an unauthenticated stranger can reach — so the defences
are layered rather than singular:

  * per-IP token bucket (burst then trickle) → 429
  * per-account escalating lockout, counted across IPs → 429
  * one generic failure message for unknown user / wrong password /
    disabled account, with a real bcrypt verify burned on the unknown-user
    path so timing doesn't leak what the message won't
  * optional TOTP second factor, with the code counted toward lockout
  * server-side session records, so logout revokes rather than suggests

Everything security-relevant is written to the audit log and, when
configured, pushed to Slack (see services/slack.py alert_login_* and
config `slack.alert_on_login_failure` / `alert_on_account_lockout`).
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..services import session as session_svc
from ..services.auth import AuthError, verify_password
from ..services.users import SAFE_TO_DISCLOSE, UserError, UserService
from .deps import Principal, get_principal_allow_password_change

log = logging.getLogger("genwatch.api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""
    # 6-digit TOTP code, or a recovery code. Only consulted when the
    # account (or the global policy) requires a second factor.
    totp: str | None = None


class PasswordChangeBody(BaseModel):
    current_password: str = Field(default="")
    new_password: str = Field(default="")


class TotpConfirmBody(BaseModel):
    code: str = ""


class TotpDisableBody(BaseModel):
    password: str = ""


def _client_ip(request: Request) -> str:
    # request.client.host is authoritative because uvicorn runs with
    # proxy_headers=True + forwarded_allow_ips (see __main__.py): it rewrites
    # request.client from X-Forwarded-For ONLY when the immediate peer is a
    # trusted proxy (loopback by default), and otherwise uses the real socket
    # peer. So a direct LAN client can't spoof XFF to forge the rate-limit /
    # audit source, and a same-host Caddy/tailscale-serve proxy that forwards
    # XFF yields the real client IP. CAVEAT: a proxy that does NOT forward XFF
    # collapses every client to the proxy's loopback address — ensure the
    # deployed proxy sets X-Forwarded-For (Caddy does by default).
    return request.client.host if request.client else "unknown"


def _users(request: Request) -> UserService:
    svc = getattr(request.app.state, "users", None)
    if svc is None:
        raise HTTPException(503, "account service not initialised")
    return svc


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    settings = request.app.state.settings
    secure = session_svc.resolve_cookie_secure(request.url.scheme, settings.auth)
    response.set_cookie(
        session_svc.cookie_name(secure),
        token,
        max_age=settings.auth.session_hours * 3600,
        httponly=True,
        samesite=settings.auth.cookie_samesite,
        secure=secure,
        path="/",
    )


def _clear_session_cookie(request: Request, response: Response) -> None:
    # Clear BOTH names: an operator who enabled TLS mid-session may hold
    # the plain-named cookie while we would now issue the __Host- one, and
    # a delete that doesn't match the stored name leaves it in place.
    settings = request.app.state.settings
    secure = session_svc.resolve_cookie_secure(request.url.scheme, settings.auth)
    for name in session_svc.COOKIE_NAMES:
        response.delete_cookie(
            name,
            path="/",
            samesite=settings.auth.cookie_samesite,
            secure=secure if name == session_svc.cookie_name(secure) else True,
        )


async def _slack(request: Request):
    return getattr(request.app.state, "slack", None)


@router.post("/login")
async def login(request: Request, body: LoginBody, response: Response) -> dict:
    settings = request.app.state.settings
    db = request.app.state.db
    users = _users(request)
    ip = _client_ip(request)

    if db.user_count() == 0:
        raise HTTPException(
            503,
            detail={
                "code": "no_accounts",
                "message": (
                    "no operator accounts exist yet — create one on the server with "
                    "`sudo -u genwatch genwatch useradd <username>`"
                ),
            },
        )

    # ── Per-IP throttle ──────────────────────────────────────────────
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is not None and not limiter.check(ip):
        retry = limiter.retry_after_s(ip)
        db.write_audit("anonymous", "auth.login", f"ip={ip}", "", "rate_limited")
        raise HTTPException(
            429,
            detail={"code": "rate_limited", "message": f"too many attempts; retry in {retry}s"},
            headers={"Retry-After": str(retry)},
        )

    # ── Per-account throttle ─────────────────────────────────────────
    # Layered on top of the IP bucket: a botnet rotating source addresses
    # slips past per-IP limits entirely, but every one of those attempts
    # still lands on the same account.
    user_limiter = getattr(request.app.state, "login_user_limiter", None)
    account_key = (body.username or "").strip().lower()
    if user_limiter is not None and account_key and not user_limiter.check(account_key):
        retry = user_limiter.retry_after_s(account_key)
        db.write_audit(account_key, "auth.login", f"ip={ip} per-account", "", "rate_limited")
        raise HTTPException(
            429,
            detail={"code": "rate_limited", "message": f"too many attempts; retry in {retry}s"},
            headers={"Retry-After": str(retry)},
        )

    result = users.authenticate(
        username=body.username, password=body.password, totp_code=body.totp
    )

    if not result.ok:
        code = result.code if result.code in SAFE_TO_DISCLOSE else "invalid_credentials"
        db.write_audit(
            account_key or "anonymous",
            "auth.login",
            f"ip={ip} {result.audit_detail or code}",
            "",
            "denied",
        )
        slack = await _slack(request)
        if slack is not None and slack.is_enabled():
            try:
                # A lockout that just tripped is the interesting event;
                # report it as such rather than as one more failure.
                user_row = users.get(account_key) if account_key else None
                locked_for = 0.0
                if user_row is not None:
                    locked_for = max(0.0, float(user_row["locked_until"] or 0) - time.time())
                if locked_for > 0 and "locked_for" in (result.audit_detail or ""):
                    await slack.alert_account_lockout(
                        username=account_key, ip=ip, seconds=locked_for
                    )
                else:
                    await slack.alert_login_failure(
                        username=account_key,
                        ip=ip,
                        reason=code.replace("_", " "),
                        attempts=int(user_row["failed_attempts"]) if user_row else 0,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("slack security alert failed: %s", e)

        status = 429 if code == "account_locked" else 401
        headers = {"Retry-After": str(result.retry_after_s)} if result.retry_after_s else None
        raise HTTPException(
            status,
            detail={
                "code": code,
                "message": result.message or "invalid username or password",
            },
            headers=headers,
        )

    user = result.user or {}
    if limiter is not None:
        limiter.reset(ip)
    if user_limiter is not None and account_key:
        user_limiter.reset(account_key)

    token, jti, _expires = session_svc.start_session(
        db=db,
        auth_cfg=settings.auth,
        user=user,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    db.user_record_login_ok(int(user["id"]), ip)
    _set_session_cookie(request, response, token)
    db.write_audit(
        user["username"],
        "auth.login",
        f"ip={ip}" + (" via-recovery-code" if result.used_recovery_code else ""),
        jti[:8] + "...",
        "ok",
    )

    slack = await _slack(request)
    if slack is not None and slack.is_enabled():
        try:
            await slack.alert_login_success(
                username=user["username"],
                ip=ip,
                method="recovery code" if result.used_recovery_code else (
                    "password + TOTP" if user["totp_enabled"] else "password"
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("slack login alert failed: %s", e)

    return {
        "ok": True,
        "operator": user["username"],
        "role": user["role"],
        "mustChangePassword": bool(user["must_change_password"]),
        "totpEnabled": bool(user["totp_enabled"]),
        "usedRecoveryCode": result.used_recovery_code,
        "recoveryCodesRemaining": result.recovery_codes_remaining,
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """End the session server-side, then clear the cookie.

    Order matters: revoking first means a logout still takes effect even
    if the browser ignores the Set-Cookie (or the response never arrives).
    """
    settings = request.app.state.settings
    db = request.app.state.db
    token = session_svc.read_cookie(request.cookies)
    if token:
        try:
            principal = session_svc.validate(db=db, settings=settings, token=token)
            db.session_revoke(principal.jti)
            db.write_audit(principal.operator, "auth.logout", "", principal.jti[:8] + "...", "ok")
        except AuthError:
            pass  # already invalid — clearing the cookie is all that's left
    _clear_session_cookie(request, response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    # Light-weight identity check used by the UI shell. Returns 200 even
    # when unauthenticated so the UI can redirect to login.
    token = session_svc.read_cookie(request.cookies)
    if not token:
        return {"authenticated": False}
    try:
        p = session_svc.validate(
            db=request.app.state.db, settings=request.app.state.settings, token=token
        )
    except AuthError:
        return {"authenticated": False}
    user = request.app.state.db.user_get_by_id(p.uid) or {}
    return {
        "authenticated": True,
        "operator": p.operator,
        "role": p.role,
        "mustChangePassword": p.must_change_password,
        "totpEnabled": bool(user.get("totp_enabled")),
        "totpRequired": bool(request.app.state.settings.auth.require_totp),
        "recoveryCodesRemaining": request.app.state.db.recovery_codes_remaining(p.uid),
    }


# ─── Self-service account management ──────────────────────────────────────
# These use `get_principal_allow_password_change` rather than the normal
# gate so an operator flagged "must change password" can actually reach
# the endpoint that clears the flag.


@router.post("/password")
async def change_password(
    request: Request,
    body: PasswordChangeBody,
    response: Response,
    p: Principal = Depends(get_principal_allow_password_change),
) -> dict:
    db = request.app.state.db
    users = _users(request)
    user = db.user_get_by_id(p.uid)
    if user is None:
        raise HTTPException(401, "account no longer exists")

    if not verify_password(body.current_password, user["password_hash"]):
        db.write_audit(p.operator, "auth.password", "wrong current password", "", "denied")
        raise HTTPException(
            403, detail={"code": "invalid_credentials", "message": "current password is incorrect"}
        )
    if body.new_password == body.current_password:
        raise HTTPException(
            400,
            detail={"code": "weak_password", "message": "new password must differ from the current one"},
        )
    try:
        users.set_password(p.operator, body.new_password)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})

    # set_password revoked every session including this one. Issue a fresh
    # session for the operator who just did the right thing rather than
    # bouncing them to the login screen.
    fresh = db.user_get_by_id(p.uid) or {}
    token, jti, _ = session_svc.start_session(
        db=db,
        auth_cfg=request.app.state.settings.auth,
        user=fresh,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_session_cookie(request, response, token)
    db.write_audit(p.operator, "auth.password", "changed own password", jti[:8] + "...", "ok")

    slack = await _slack(request)
    if slack is not None and slack.is_enabled():
        try:
            await slack.alert_user_change(
                action="password changed", target=p.operator, actor=p.operator,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("slack account alert failed: %s", e)
    return {"ok": True, "sessionsRevoked": True}


@router.get("/sessions")
async def list_sessions(
    request: Request, p: Principal = Depends(get_principal_allow_password_change)
) -> dict:
    rows = request.app.state.db.sessions_for_user(p.uid)
    return {
        "sessions": [
            {
                "id": r["jti"][:8],
                "current": r["jti"] == p.jti,
                "createdAt": r["created_at"],
                "lastSeenAt": r["last_seen_at"],
                "expiresAt": r["expires_at"],
                "ip": r["ip"],
                "userAgent": r["user_agent"],
            }
            for r in rows
        ]
    }


@router.delete("/sessions")
async def revoke_other_sessions(
    request: Request, p: Principal = Depends(get_principal_allow_password_change)
) -> dict:
    """Sign out everywhere else — keeps the caller's own session."""
    n = request.app.state.db.session_revoke_all_for_user(p.uid, except_jti=p.jti)
    request.app.state.db.write_audit(
        p.operator, "auth.sessions.revoke", f"revoked={n}", "", "ok"
    )
    return {"ok": True, "revoked": n}


# ─── TOTP enrollment (self-service) ───────────────────────────────────────


@router.post("/totp/enroll")
async def totp_enroll(
    request: Request, p: Principal = Depends(get_principal_allow_password_change)
) -> dict:
    """Start enrollment: returns a secret + otpauth:// URI to scan.

    Not active until /totp/confirm succeeds, so a failed QR scan can't
    lock the operator out of their own console.
    """
    users = _users(request)
    settings = request.app.state.settings
    try:
        out = users.begin_totp_enrollment(p.operator, issuer=settings.auth.totp_issuer)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    request.app.state.db.write_audit(p.operator, "auth.totp.enroll", "started", "", "ok")
    return {"ok": True, "secret": out["secret"], "uri": out["uri"]}


@router.post("/totp/confirm")
async def totp_confirm(
    request: Request,
    body: TotpConfirmBody,
    p: Principal = Depends(get_principal_allow_password_change),
) -> dict:
    users = _users(request)
    try:
        codes = users.confirm_totp_enrollment(p.operator, body.code)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    request.app.state.db.write_audit(p.operator, "auth.totp.confirm", "enabled", "", "ok")
    slack = await _slack(request)
    if slack is not None and slack.is_enabled():
        try:
            await slack.alert_user_change(
                action="two-factor enabled", target=p.operator, actor=p.operator,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("slack account alert failed: %s", e)
    # Recovery codes are returned exactly once — we only keep hashes.
    return {"ok": True, "recoveryCodes": codes}


@router.post("/totp/disable")
async def totp_disable(
    request: Request,
    body: TotpDisableBody,
    p: Principal = Depends(get_principal_allow_password_change),
) -> dict:
    """Turn off the second factor. Requires the account password —
    otherwise a hijacked session could quietly strip 2FA."""
    db = request.app.state.db
    users = _users(request)
    user = db.user_get_by_id(p.uid)
    if user is None:
        raise HTTPException(401, "account no longer exists")
    if not verify_password(body.password, user["password_hash"]):
        db.write_audit(p.operator, "auth.totp.disable", "wrong password", "", "denied")
        raise HTTPException(
            403, detail={"code": "invalid_credentials", "message": "password is incorrect"}
        )
    if request.app.state.settings.auth.require_totp:
        raise HTTPException(
            409,
            detail={
                "code": "totp_required_by_policy",
                "message": (
                    "two-factor is required by auth.require_totp — an admin must turn "
                    "that policy off before it can be disabled for an account"
                ),
            },
        )
    try:
        users.disable_totp(p.operator, actor=p.operator)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    db.write_audit(p.operator, "auth.totp.disable", "disabled", "", "ok")
    slack = await _slack(request)
    if slack is not None and slack.is_enabled():
        try:
            await slack.alert_user_change(
                action="two-factor DISABLED", target=p.operator, actor=p.operator,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("slack account alert failed: %s", e)
    return {"ok": True}
