"""Account service: named users, the login decision, and its guard rails.

This module owns everything that decides *whether a login succeeds* and
what an admin is allowed to do to an account. The database layer (db.py)
is a dumb store; the API layer (api/auth.py, api/users.py) turns the
results below into HTTP. Keeping the decision here means the rules are
unit-testable without a web server, and there is exactly one place to
read when asking "what happens on a bad password?".

Design notes for an internet-exposed login
------------------------------------------
* **One generic failure.** Unknown username, wrong password, and disabled
  account all return the same ``invalid_credentials`` result. An attacker
  who can tell "no such user" from "wrong password" gets a free list of
  valid accounts to spray.
* **Constant-ish timing.** The unknown-user path burns a real bcrypt
  verify (:func:`~genwatch.services.auth.burn_dummy_verify`) so the fast
  "no such user" return doesn't leak the same information timing-wise
  that we just refused to leak in the response body.
* **Lockout after the password, not before.** A locked account still runs
  the password check, and only tells the caller it is locked when the
  password was *right*. A wrong password against a locked account gets
  the same generic failure as any other wrong password — otherwise the
  lockout message itself becomes the enumeration oracle.
* **Escalating lockout.** Each additional lockout doubles the wait, up to
  a cap. Fixed-window lockouts are trivially waited out by a patient
  script; unbounded ones turn into a denial of service against the
  operator during an outage, which for a generator console is its own
  safety problem.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from . import totp as totp_mod
from .auth import (
    ROLES,
    PasswordPolicyError,
    burn_dummy_verify,
    hash_password,
    is_bcrypt_hash,
    validate_password_strength,
    verify_password,
)

log = logging.getLogger("genwatch.users")

# Username shape: 3–32 chars, letters/digits and . _ - only, must start
# with a letter or digit. Narrow on purpose — usernames end up in audit
# rows, Slack messages, and log lines, and this keeps all three free of
# anything that needs escaping.
USERNAME_MIN = 3
USERNAME_MAX = 32
_USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class UserError(Exception):
    """An operator-facing account-management error."""

    def __init__(self, message: str, *, code: str = "invalid", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def normalise_username(raw: str) -> str:
    """Lowercase + strip. Raises :class:`UserError` if the shape is wrong."""
    name = (raw or "").strip().lower()
    if not name:
        raise UserError("username is required", code="username_required")
    if not (USERNAME_MIN <= len(name) <= USERNAME_MAX):
        raise UserError(
            f"username must be {USERNAME_MIN}–{USERNAME_MAX} characters",
            code="username_invalid",
        )
    if any(ch not in _USERNAME_OK for ch in name):
        raise UserError(
            "username may only contain letters, digits, dot, underscore and hyphen",
            code="username_invalid",
        )
    if not name[0].isalnum():
        raise UserError("username must start with a letter or digit", code="username_invalid")
    return name


@dataclass
class LoginResult:
    """Outcome of :meth:`UserService.authenticate`.

    ``ok`` is the only field callers should branch on for success. ``code``
    carries the machine-readable reason for everything else, and is safe
    to return to the client *only* for the cases explicitly listed in
    :data:`SAFE_TO_DISCLOSE` — the rest collapse to a generic failure.
    """

    ok: bool
    user: dict[str, Any] | None = None
    code: str = ""
    message: str = ""
    retry_after_s: int = 0
    audit_detail: str = ""
    used_recovery_code: bool = False
    recovery_codes_remaining: int = 0


# Reasons the client is allowed to see. Everything else is flattened to
# `invalid_credentials` so the response can't be used to enumerate
# accounts or probe their state.
SAFE_TO_DISCLOSE = frozenset(
    {
        "invalid_credentials",
        "totp_required",
        "totp_invalid",
        "totp_enrollment_required",
        "account_locked",
        "password_change_required",
    }
)


class UserService:
    """Account operations, bound to one :class:`~genwatch.db.Database`."""

    def __init__(self, db: Database, cfg):
        self.db = db
        self.cfg = cfg  # AuthConfig — re-read on every call so PUT /api/config applies live

    def update_config(self, cfg) -> None:
        self.cfg = cfg

    # ── lookup ───────────────────────────────────────────────────────
    def get(self, username: str) -> dict | None:
        return self.db.user_get((username or "").strip().lower())

    def get_or_raise(self, username: str) -> dict:
        u = self.get(username)
        if u is None:
            raise UserError(f"no such user: {username}", code="not_found", http_status=404)
        return u

    def list_users(self) -> list[dict]:
        return self.db.user_list()

    # ── creation / mutation ──────────────────────────────────────────
    def create(
        self,
        *,
        username: str,
        password: str,
        role: str = "operator",
        must_change_password: bool = False,
        disabled: bool = False,
        created_by: str = "",
        enforce_policy: bool = True,
    ) -> dict:
        name = normalise_username(username)
        if role not in ROLES:
            raise UserError(f"role must be one of {', '.join(ROLES)}", code="role_invalid")
        if self.db.user_get(name) is not None:
            raise UserError(f"user {name!r} already exists", code="duplicate", http_status=409)
        if enforce_policy:
            self._check_password(password, username=name)
        self.db.user_create(
            username=name,
            password_hash=hash_password(password),
            role=role,
            must_change_password=must_change_password,
            disabled=disabled,
            created_by=created_by,
        )
        log.info("user created: %s (role=%s, by=%s)", name, role, created_by or "system")
        return self.get_or_raise(name)

    def set_password(
        self,
        username: str,
        new_password: str,
        *,
        must_change: bool = False,
        enforce_policy: bool = True,
    ) -> dict:
        u = self.get_or_raise(username)
        if enforce_policy:
            self._check_password(new_password, username=u["username"])
        self.db.user_set_password(u["id"], hash_password(new_password), must_change=must_change)
        # A password change is also a session revocation: token_epoch was
        # bumped by user_set_password, and we mark the rows so the
        # sessions list reflects reality rather than showing zombies.
        self.db.session_revoke_all_for_user(u["id"])
        log.info("password changed for %s (all sessions revoked)", u["username"])
        return self.get_or_raise(u["username"])

    def set_role(self, username: str, role: str, *, actor: str = "") -> dict:
        if role not in ROLES:
            raise UserError(f"role must be one of {', '.join(ROLES)}", code="role_invalid")
        u = self.get_or_raise(username)
        if u["role"] == "admin" and role != "admin":
            self._assert_not_last_admin(u, action="demote")
        self.db.user_update(u["id"], role=role)
        log.info("role for %s set to %s by %s", u["username"], role, actor or "system")
        return self.get_or_raise(u["username"])

    def set_disabled(self, username: str, disabled: bool, *, actor: str = "") -> dict:
        u = self.get_or_raise(username)
        if disabled:
            self._assert_not_last_admin(u, action="disable")
        self.db.user_update(u["id"], disabled=int(disabled))
        if disabled:
            # Disabling has to end live sessions too, or the account stays
            # usable until its token expires — up to session_hours later.
            self.db.session_revoke_all_for_user(u["id"])
            self.db.user_bump_token_epoch(u["id"])
        log.info("user %s %s by %s", u["username"], "disabled" if disabled else "enabled", actor or "system")
        return self.get_or_raise(u["username"])

    def delete(self, username: str, *, actor: str = "") -> None:
        u = self.get_or_raise(username)
        self._assert_not_last_admin(u, action="delete")
        if actor and actor.lower() == u["username"].lower():
            raise UserError("you cannot delete your own account", code="self_delete")
        self.db.user_delete(u["id"])
        log.info("user %s deleted by %s", u["username"], actor or "system")

    def unlock(self, username: str) -> dict:
        u = self.get_or_raise(username)
        self.db.user_update(u["id"], failed_attempts=0, locked_until=0.0)
        return self.get_or_raise(u["username"])

    def revoke_sessions(self, username: str, *, except_jti: str | None = None) -> int:
        u = self.get_or_raise(username)
        n = self.db.session_revoke_all_for_user(u["id"], except_jti=except_jti)
        if except_jti is None:
            self.db.user_bump_token_epoch(u["id"])
        return n

    def _assert_not_last_admin(self, user: dict, *, action: str) -> None:
        if user["role"] != "admin" or user["disabled"]:
            return
        if self.db.count_enabled_admins(excluding_id=user["id"]) == 0:
            raise UserError(
                f"cannot {action} the last enabled admin — create another admin first "
                "(otherwise nobody can reach the console without a site visit)",
                code="last_admin",
                http_status=409,
            )

    def _check_password(self, password: str, *, username: str) -> None:
        try:
            validate_password_strength(
                password,
                username=username,
                min_length=getattr(self.cfg, "password_min_length", 12),
            )
        except PasswordPolicyError as e:
            raise UserError(str(e), code="weak_password") from e

    # ── TOTP ─────────────────────────────────────────────────────────
    def begin_totp_enrollment(self, username: str, *, issuer: str) -> dict:
        """Generate (but do not yet activate) a TOTP secret.

        The secret is stored immediately with ``totp_enabled = 0`` so the
        confirm step can verify against it; enrollment only takes effect
        once the operator proves they can produce a code. That ordering
        matters — activating on generation would lock out anyone whose QR
        scan silently failed.
        """
        u = self.get_or_raise(username)
        secret = totp_mod.generate_secret()
        self.db.user_update(u["id"], totp_secret=secret, totp_enabled=0, totp_last_counter=0)
        return {
            "secret": secret,
            "uri": totp_mod.provisioning_uri(
                secret_b32=secret, username=u["username"], issuer=issuer
            ),
        }

    def confirm_totp_enrollment(self, username: str, code: str) -> list[str]:
        """Activate TOTP after verifying a code. Returns recovery codes."""
        u = self.get_or_raise(username)
        if not u["totp_secret"]:
            raise UserError("no pending TOTP enrollment — start one first", code="totp_no_pending")
        counter = totp_mod.verify(u["totp_secret"], code, last_counter=int(u["totp_last_counter"] or 0))
        if counter is None:
            raise UserError(
                "that code didn't match — check the phone's clock and try the next one",
                code="totp_invalid",
                http_status=400,
            )
        codes = totp_mod.generate_recovery_codes()
        self.db.recovery_codes_replace(u["id"], [totp_mod.hash_recovery_code(c) for c in codes])
        self.db.user_update(u["id"], totp_enabled=1, totp_last_counter=counter)
        log.info("TOTP enabled for %s", u["username"])
        return codes

    def disable_totp(self, username: str, *, actor: str = "") -> dict:
        u = self.get_or_raise(username)
        self.db.user_update(u["id"], totp_secret="", totp_enabled=0, totp_last_counter=0)
        self.db.recovery_codes_replace(u["id"], [])
        log.warning("TOTP disabled for %s by %s", u["username"], actor or "system")
        return self.get_or_raise(u["username"])

    def totp_required_for(self, user: dict) -> bool:
        return bool(user["totp_enabled"]) or bool(getattr(self.cfg, "require_totp", False))

    # ── the login decision ───────────────────────────────────────────
    def authenticate(
        self,
        *,
        username: str,
        password: str,
        totp_code: str | None = None,
        now: float | None = None,
    ) -> LoginResult:
        now = now if now is not None else time.time()
        generic = LoginResult(
            ok=False,
            code="invalid_credentials",
            message="invalid username or password",
        )

        try:
            name = normalise_username(username)
        except UserError:
            # Malformed username: still burn the bcrypt time so "not even a
            # valid username" isn't faster than "valid but wrong".
            burn_dummy_verify(password)
            return LoginResult(**{**generic.__dict__, "audit_detail": "malformed_username"})

        user = self.db.user_get(name)
        if user is None:
            burn_dummy_verify(password)
            return LoginResult(**{**generic.__dict__, "audit_detail": "unknown_user"})

        password_ok = verify_password(password, user["password_hash"])

        locked_until = float(user["locked_until"] or 0)
        if locked_until > now:
            # Only an otherwise-correct password earns the specific
            # "locked" answer; anything else looks like a normal failure.
            if password_ok:
                return LoginResult(
                    ok=False,
                    code="account_locked",
                    message=(
                        "account temporarily locked after repeated failed sign-ins — "
                        "wait for the lockout to expire or ask an admin to unlock it"
                    ),
                    retry_after_s=max(1, int(locked_until - now)),
                    audit_detail=f"locked_until={locked_until:.0f}",
                )
            self._record_failure(user, now)
            return LoginResult(**{**generic.__dict__, "audit_detail": "bad_password_while_locked"})

        if not password_ok:
            attempts, lock_for = self._record_failure(user, now)
            detail = f"bad_password attempts={attempts}"
            if lock_for:
                detail += f" locked_for={lock_for:.0f}s"
            return LoginResult(**{**generic.__dict__, "audit_detail": detail})

        if user["disabled"]:
            # Correct password on a disabled account: still generic. The
            # account may have been disabled *because* the password leaked.
            return LoginResult(**{**generic.__dict__, "audit_detail": "account_disabled"})

        # ── second factor ────────────────────────────────────────────
        used_recovery = False
        if self.totp_required_for(user):
            if not user["totp_enabled"]:
                # require_totp is on but this account never enrolled. Refuse
                # rather than waving them through: a policy that silently
                # exempts the accounts that ignored it is not a policy.
                return LoginResult(
                    ok=False,
                    code="totp_enrollment_required",
                    message=(
                        "two-factor authentication is required but this account has not "
                        "enrolled. An admin can enroll it with `genwatch usertotp "
                        f"{user['username']}`."
                    ),
                    audit_detail="totp_not_enrolled",
                )
            if not totp_code:
                return LoginResult(
                    ok=False,
                    code="totp_required",
                    message="enter the 6-digit code from your authenticator app",
                    audit_detail="totp_prompt",
                )
            counter = totp_mod.verify(
                user["totp_secret"],
                totp_code,
                now=now,
                last_counter=int(user["totp_last_counter"] or 0),
            )
            if counter is not None:
                self.db.user_update(user["id"], totp_last_counter=counter)
            elif self.db.recovery_code_consume(user["id"], totp_mod.hash_recovery_code(totp_code)):
                used_recovery = True
                log.warning(
                    "%s signed in with a TOTP recovery code (%d remaining)",
                    user["username"], self.db.recovery_codes_remaining(user["id"]),
                )
            else:
                # A wrong second factor counts toward lockout: without it,
                # someone holding a stolen password gets unlimited tries at
                # the 6 digits.
                attempts, lock_for = self._record_failure(user, now)
                return LoginResult(
                    ok=False,
                    code="totp_invalid",
                    message="that code isn't valid — try the next one from your app",
                    audit_detail=f"bad_totp attempts={attempts}",
                )

        return LoginResult(
            ok=True,
            user=self.db.user_get_by_id(user["id"]),
            used_recovery_code=used_recovery,
            recovery_codes_remaining=self.db.recovery_codes_remaining(user["id"]),
        )

    def _record_failure(self, user: dict, now: float) -> tuple[int, float]:
        """Count a failed attempt and arm the lockout when the threshold
        is crossed. Returns ``(attempts, lock_seconds_applied)``."""
        threshold = max(1, int(getattr(self.cfg, "lockout_threshold", 5)))
        base = max(0, int(getattr(self.cfg, "lockout_seconds", 900)))
        cap = max(base, int(getattr(self.cfg, "lockout_max_seconds", 3600)))
        attempts = int(user["failed_attempts"] or 0) + 1
        lock_for = 0.0
        if base and attempts >= threshold:
            # Escalate: 1× base at the threshold, then 2×, 4×, … capped.
            over = attempts - threshold
            lock_for = float(min(cap, base * (2 ** min(over, 10))))
        self.db.user_record_login_fail(user["id"], locked_until=(now + lock_for) if lock_for else 0.0)
        if lock_for:
            log.warning(
                "account %s locked for %.0fs after %d failed attempts",
                user["username"], lock_for, attempts,
            )
        return attempts, lock_for


# ─── Bootstrap ────────────────────────────────────────────────────────────

@dataclass
class BootstrapResult:
    created: bool = False
    username: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)


def ensure_bootstrap_user(db: Database, auth_cfg) -> BootstrapResult:
    """Seed the first admin account so an upgrade doesn't lock anyone out.

    Existing deployments authenticate against a single shared password in
    ``auth.admin_password_hash``. On first boot after the multi-user
    upgrade the ``users`` table is empty, so we migrate that hash into a
    real account (username from ``auth.bootstrap_username``, default
    ``admin``). The operator's existing password keeps working — they just
    now have a username to type with it.

    Does nothing once any account exists, so an admin who later removes
    the config hash doesn't get a ghost account resurrected on restart.
    """
    result = BootstrapResult()
    if db.user_count() > 0:
        return result

    pw_hash = (getattr(auth_cfg, "admin_password_hash", "") or "").strip()
    if not is_bcrypt_hash(pw_hash):
        return result

    username = (getattr(auth_cfg, "bootstrap_username", "") or "admin").strip().lower()
    try:
        username = normalise_username(username)
    except UserError:
        result.warnings.append(
            f"auth.bootstrap_username {username!r} is not a valid username — using 'admin'"
        )
        username = "admin"

    db.user_create(
        username=username,
        password_hash=pw_hash,
        role="admin",
        must_change_password=False,
        created_by="bootstrap",
    )
    result.created = True
    result.username = username
    result.source = "auth.admin_password_hash"
    log.warning(
        "Seeded admin account %r from auth.admin_password_hash. Sign in with that "
        "username and your existing password, then create per-operator accounts "
        "(Settings → Users, or `genwatch useradd`). auth.admin_password_hash is no "
        "longer consulted at login once accounts exist — you can clear it.",
        username,
    )
    return result
