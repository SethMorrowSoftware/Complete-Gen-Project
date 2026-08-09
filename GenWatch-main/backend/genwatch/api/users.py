"""Admin user management: /api/users.

Every route here is admin-only and every mutation is audited. The guard
rails that matter live in services/users.py (last-admin protection,
password policy, role validation) so they apply identically to the CLI —
`genwatch userdel` cannot strand a site any more than this API can.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..services.users import UserError, UserService
from .deps import Principal, require_admin

log = logging.getLogger("genwatch.api.users")

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreateBody(BaseModel):
    username: str
    password: str
    role: str = "operator"
    must_change_password: bool = True
    disabled: bool = False


class UserPatchBody(BaseModel):
    role: str | None = None
    disabled: bool | None = None


class PasswordResetBody(BaseModel):
    new_password: str
    must_change_password: bool = True


def _users(request: Request) -> UserService:
    svc = getattr(request.app.state, "users", None)
    if svc is None:
        raise HTTPException(503, "account service not initialised")
    return svc


def _public(u: dict, *, db) -> dict:
    """Shape a user row for the API. Never includes the hash or the TOTP
    secret — those exist only to be compared against, never to be read."""
    return {
        "username": u["username"],
        "role": u["role"],
        "disabled": bool(u["disabled"]),
        "mustChangePassword": bool(u["must_change_password"]),
        "totpEnabled": bool(u["totp_enabled"]),
        "lockedUntil": float(u["locked_until"] or 0),
        "failedAttempts": int(u["failed_attempts"] or 0),
        "lastLoginAt": u["last_login_at"],
        "lastLoginIp": u["last_login_ip"],
        "passwordChangedAt": u["password_changed_at"],
        "createdAt": u["created_at"],
        "createdBy": u["created_by"],
        "activeSessions": len(db.sessions_for_user(int(u["id"]))),
        "recoveryCodesRemaining": db.recovery_codes_remaining(int(u["id"])),
    }


async def _notify(request: Request, *, action: str, target: str, actor: str, detail: str = "") -> None:
    slack = getattr(request.app.state, "slack", None)
    if slack is None or not slack.is_enabled():
        return
    try:
        await slack.alert_user_change(action=action, target=target, actor=actor, detail=detail)
    except Exception as e:  # noqa: BLE001
        log.warning("slack account alert failed: %s", e)


@router.get("")
async def list_users(request: Request, p: Principal = Depends(require_admin)) -> dict:
    db = request.app.state.db
    return {"users": [_public(u, db=db) for u in _users(request).list_users()]}


@router.post("")
async def create_user(
    request: Request, body: UserCreateBody, p: Principal = Depends(require_admin)
) -> dict:
    db = request.app.state.db
    try:
        u = _users(request).create(
            username=body.username,
            password=body.password,
            role=body.role,
            must_change_password=body.must_change_password,
            disabled=body.disabled,
            created_by=p.operator,
        )
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    db.write_audit(p.operator, "users.create", f"{u['username']} role={u['role']}", "", "ok")
    await _notify(request, action="created", target=u["username"], actor=p.operator,
                  detail=f"role {u['role']}")
    return {"ok": True, "user": _public(u, db=db)}


@router.patch("/{username}")
async def patch_user(
    request: Request, username: str, body: UserPatchBody, p: Principal = Depends(require_admin)
) -> dict:
    db = request.app.state.db
    svc = _users(request)
    changes: list[str] = []
    try:
        u = svc.get_or_raise(username)
        if body.role is not None and body.role != u["role"]:
            if u["username"].lower() == p.operator.lower() and body.role != "admin":
                # Self-demotion is how an admin accidentally locks the
                # console's own configuration away from themselves.
                raise UserError("you cannot change your own role", code="self_role_change")
            u = svc.set_role(username, body.role, actor=p.operator)
            changes.append(f"role={body.role}")
        if body.disabled is not None and bool(body.disabled) != bool(u["disabled"]):
            if u["username"].lower() == p.operator.lower() and body.disabled:
                raise UserError("you cannot disable your own account", code="self_disable")
            u = svc.set_disabled(username, bool(body.disabled), actor=p.operator)
            changes.append("disabled" if body.disabled else "enabled")
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    if changes:
        detail = ", ".join(changes)
        db.write_audit(p.operator, "users.update", f"{u['username']} {detail}", "", "ok")
        await _notify(request, action="updated", target=u["username"], actor=p.operator, detail=detail)
    return {"ok": True, "user": _public(u, db=db)}


@router.post("/{username}/password")
async def reset_password(
    request: Request, username: str, body: PasswordResetBody, p: Principal = Depends(require_admin)
) -> dict:
    """Admin password reset.

    Defaults to flagging must_change_password: a password an admin picked
    and sent over chat is a shared secret until the operator replaces it,
    and the gate in deps.get_principal makes that replacement mandatory
    before the account can do anything else.
    """
    db = request.app.state.db
    try:
        u = _users(request).set_password(
            username, body.new_password, must_change=body.must_change_password
        )
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    db.write_audit(p.operator, "users.password_reset", u["username"], "", "ok")
    await _notify(request, action="password reset", target=u["username"], actor=p.operator)
    return {"ok": True, "user": _public(u, db=db)}


@router.post("/{username}/unlock")
async def unlock_user(
    request: Request, username: str, p: Principal = Depends(require_admin)
) -> dict:
    try:
        u = _users(request).unlock(username)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    request.app.state.db.write_audit(p.operator, "users.unlock", u["username"], "", "ok")
    return {"ok": True, "user": _public(u, db=request.app.state.db)}


@router.post("/{username}/revoke-sessions")
async def revoke_sessions(
    request: Request, username: str, p: Principal = Depends(require_admin)
) -> dict:
    """Kill every live session for an account (lost laptop, departure)."""
    try:
        n = _users(request).revoke_sessions(username)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    request.app.state.db.write_audit(
        p.operator, "users.revoke_sessions", f"{username} revoked={n}", "", "ok"
    )
    return {"ok": True, "revoked": n}


@router.post("/{username}/totp/disable")
async def admin_disable_totp(
    request: Request, username: str, p: Principal = Depends(require_admin)
) -> dict:
    """Clear an account's second factor — the lost-phone path.

    Deliberately loud: it lowers the account's protection, so it is
    audited and pushed to Slack like any other account change.
    """
    try:
        u = _users(request).disable_totp(username, actor=p.operator)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    request.app.state.db.write_audit(p.operator, "users.totp_disable", u["username"], "", "ok")
    await _notify(request, action="two-factor DISABLED", target=u["username"], actor=p.operator)
    return {"ok": True, "user": _public(u, db=request.app.state.db)}


@router.delete("/{username}")
async def delete_user(
    request: Request, username: str, p: Principal = Depends(require_admin)
) -> dict:
    try:
        _users(request).delete(username, actor=p.operator)
    except UserError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})
    request.app.state.db.write_audit(p.operator, "users.delete", username, "", "ok")
    await _notify(request, action="deleted", target=username.lower(), actor=p.operator)
    return {"ok": True}
