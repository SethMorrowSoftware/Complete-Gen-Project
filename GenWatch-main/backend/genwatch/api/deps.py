"""Shared dependencies: app state, auth, role gates."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from ..services import session as session_svc
from ..services.auth import AuthError


class Principal:
    def __init__(self, *, operator: str, role: str, uid: int = 0, jti: str = "",
                 must_change_password: bool = False, remember: bool = False):
        self.operator = operator
        self.role = role
        self.uid = uid
        self.jti = jti
        self.must_change_password = must_change_password
        # True when this request rode a "keep me signed in" session.
        self.remember = remember


def get_app_state(request: Request):
    return request.app.state


def _resolve(request: Request) -> Principal:
    """Read auth from cookie *or* Authorization header, then validate the
    session server-side.

    Cookie is the normal path for the browser UI; the bearer header is for
    CLI / automation use. Both carry the same token and get the same
    treatment — a revoked session is revoked for `curl` too.
    """
    token = session_svc.read_cookie(request.cookies)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1]
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")

    try:
        sp = session_svc.validate(
            db=request.app.state.db, settings=request.app.state.settings, token=token
        )
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))

    return Principal(
        operator=sp.operator,
        role=sp.role,
        uid=sp.uid,
        jti=sp.jti,
        must_change_password=sp.must_change_password,
        remember=sp.remember,
    )


def get_principal_allow_password_change(request: Request) -> Principal:
    """Authenticated, even when the account still owes a password change.

    Only the endpoints that let an operator *fix* that state use this —
    /api/auth/password, /api/auth/me, logout, TOTP enrollment.
    """
    return _resolve(request)


def get_principal(request: Request) -> Principal:
    p = _resolve(request)
    if p.must_change_password:
        # A temporary password handed out by an admin is a credential that
        # has, by definition, been transmitted through some side channel
        # (chat, phone, a sticky note). Everything except changing it is
        # blocked until it's replaced — otherwise "must change password"
        # is a suggestion, and a leaked temp password is a live account.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "password_change_required",
                "message": "you must set a new password before using the console",
            },
        )
    return p


# Role model — three roles, enforced against the account's stored role.
#
#   viewer    read-only: live view, history, events
#   operator  the above + control commands (start/stop/exercise/transfer,
#             alarm ack) — everything that can move hardware
#   admin     the above + configuration, register map, and user management
#
# Note the ordering assumption: require_operator admits {operator, admin},
# require_admin admits {admin} only. Pick the gate that documents intent —
# anything secret-handling or account-touching is admin; anything
# operational is operator; plain reads need only an authenticated session.
def require_viewer(p: Principal = Depends(get_principal)) -> Principal:
    return p


def require_operator(p: Principal = Depends(get_principal)) -> Principal:
    if p.role not in ("operator", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
    return p


def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return p
