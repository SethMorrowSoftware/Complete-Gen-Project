"""Password hashing, session-token minting, and password policy.

Identity model
--------------
Named accounts in the ``users`` table (see db.py / services/users.py),
each with its own password, role, and optional TOTP second factor. The
original single shared ``auth.admin_password_hash`` is retained only to
seed the first admin when an existing deployment upgrades — after that
it is unused for login.

Session tokens are HS256 JWTs carrying, besides the usual claims:

  ``uid``    the account id, so a rename can't hand over someone's session
  ``jti``    a random session id, recorded server-side in ``sessions`` —
             this is what makes logout a real revocation rather than
             "drop the cookie and hope the holder is honest"
  ``epoch``  the account's ``token_epoch`` at mint time. A password change
             or an admin revoke bumps the epoch, and every token minted
             under the old one stops validating on its next request.

A stateless JWT alone cannot do any of that, and an internet-exposed
console needs all three: the whole point of a logout button on a public
endpoint is that it ends the session for whoever holds the cookie.
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from typing import Literal

import bcrypt
import jwt

log = logging.getLogger("genwatch.auth")

Role = Literal["viewer", "operator", "admin"]
ROLES: tuple[str, ...] = ("viewer", "operator", "admin")

# bcrypt's 72-byte secret limit — silently truncate to match the standard
# Python bcrypt binding behavior. Operators rarely pick passwords this
# long, but if they do, paste-from-keepass shouldn't crash login.
_BCRYPT_MAX = 72

# Cost factor. 12 is ~250 ms on a Pi 5 — slow enough that an offline
# attack on a stolen database is expensive, fast enough that a login
# doesn't feel broken and the rate limiter (not bcrypt) is what bounds
# online guessing.
BCRYPT_ROUNDS = 12

# A real bcrypt hash of a random string, computed once at import. Used to
# burn the same ~250 ms when the *username* doesn't exist as when it does,
# so response timing can't be used to enumerate accounts on a login
# endpoint that is reachable from the open internet.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


class AuthError(Exception):
    pass


class PasswordPolicyError(ValueError):
    """Raised when a proposed password fails the strength policy."""


def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")[:_BCRYPT_MAX]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed or not plain:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_BCRYPT_MAX], hashed.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False


def burn_dummy_verify(plain: str = "") -> None:
    """Spend a bcrypt verify against a throwaway hash.

    Called on the "no such user" path so an unknown username takes the
    same wall-clock time as a known one. Without it, the login endpoint
    is a free account-enumeration oracle for anyone who can time an HTTP
    request — which, on the open web, is everyone.
    """
    try:
        bcrypt.checkpw((plain or "x").encode("utf-8")[:_BCRYPT_MAX], _DUMMY_HASH)
    except Exception:  # noqa: BLE001
        pass


def is_bcrypt_hash(value: str) -> bool:
    return bool(value) and value.startswith(("$2a$", "$2b$", "$2y$")) and len(value) >= 55


# ─── Session tokens ───────────────────────────────────────────────────────

def new_jti() -> str:
    return secrets.token_urlsafe(24)


def issue_token(
    *,
    secret: str,
    operator: str,
    role: Role = "admin",
    hours: int = 12,
    uid: int | None = None,
    jti: str | None = None,
    epoch: int = 1,
) -> tuple[str, str, float]:
    """Mint a session JWT. Returns ``(token, jti, expires_at)``."""
    if not secret:
        raise AuthError("auth not configured (missing jwt_secret)")
    now = int(time.time())
    exp = now + int(hours * 3600)
    token_id = jti or new_jti()
    payload = {
        "sub": operator,
        "role": role,
        "iat": now,
        "exp": exp,
        "jti": token_id,
        "epoch": int(epoch),
    }
    if uid is not None:
        payload["uid"] = int(uid)
    return jwt.encode(payload, secret, algorithm="HS256"), token_id, float(exp)


def decode_token(*, secret: str, token: str) -> dict:
    if not secret:
        raise AuthError("auth not configured")
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"invalid token: {e}")


# ─── Password policy ──────────────────────────────────────────────────────
# Deliberately short and explainable. The goal is to stop the passwords
# that actually get popped on an internet-facing login — reused site
# passwords, the site name, keyboard walks — not to enforce a compliance
# checkbox that pushes operators toward "Summer2026!" on a sticky note.

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
# Above this length we stop asking for character classes: a long
# passphrase beats a short scrambled string, and demanding punctuation in
# a 20-character phrase just makes it harder to type on a phone at 2 a.m.
PASSPHRASE_EXEMPT_LENGTH = 20

# Not a breach corpus — a short list of what actually gets sprayed at a
# login endpoint, plus the terms specific to this product that an
# operator might reach for. A real deployment should also be behind the
# rate limiter and lockout; this list only catches the worst choices.
_COMMON_PASSWORDS = frozenset(
    """
    password password1 password123 passw0rd p@ssw0rd p@ssword123 123456 1234567
    12345678 123456789 1234567890 qwerty qwerty123 qwertyuiop asdfghjkl zxcvbnm
    letmein welcome welcome1 admin admin123 administrator root toor changeme
    change-me changeit default secret abc123 iloveyou monkey dragon sunshine
    princess football baseball trustno1 master shadow superman batman starwars
    genwatch genwatch123 generator generac castle castle123 generator123
    h100 h-100 operator operator123 maintenance service technician
    """.split()
)

_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl", "zxcvbnm")


def _has_run(pw: str, length: int = 6) -> bool:
    """True if the password is largely a keyboard/alphabet run."""
    low = pw.lower()
    for seq in _SEQUENCES:
        for i in range(len(seq) - length + 1):
            chunk = seq[i : i + length]
            if chunk in low or chunk[::-1] in low:
                return True
    return False


def validate_password_strength(
    password: str,
    *,
    username: str = "",
    min_length: int = MIN_PASSWORD_LENGTH,
) -> None:
    """Raise :class:`PasswordPolicyError` if the password is too weak.

    Each failure names exactly what to fix — a vague "password does not
    meet requirements" just produces another guess.
    """
    if not password:
        raise PasswordPolicyError("password cannot be empty")
    floor = max(8, int(min_length))
    if len(password) < floor:
        raise PasswordPolicyError(f"password must be at least {floor} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.strip() != password:
        raise PasswordPolicyError("password cannot start or end with whitespace")

    low = password.lower()
    if low in _COMMON_PASSWORDS:
        raise PasswordPolicyError("that password is on the common-password list — pick another")
    # Strip trailing digits/punctuation before re-checking: "password1!" is
    # the same guess as "password" to anyone spraying a wordlist.
    stem = re.sub(r"[\W_]+$", "", re.sub(r"\d+$", "", low))
    if stem and stem in _COMMON_PASSWORDS:
        raise PasswordPolicyError(
            "that is a common password with digits appended — pick something unrelated"
        )
    if username and len(username) >= 3 and username.lower() in low:
        raise PasswordPolicyError("password must not contain the username")
    if len(set(password)) < 5:
        raise PasswordPolicyError("password repeats too few distinct characters")
    if _has_run(password):
        raise PasswordPolicyError("password contains a long keyboard or alphabet run")

    if len(password) < PASSPHRASE_EXEMPT_LENGTH:
        classes = sum(
            bool(rx.search(password))
            for rx in (
                re.compile(r"[a-z]"),
                re.compile(r"[A-Z]"),
                re.compile(r"\d"),
                re.compile(r"[^A-Za-z0-9]"),
            )
        )
        if classes < 3:
            raise PasswordPolicyError(
                "password must mix at least three of: lowercase, uppercase, "
                f"digits, symbols — or be {PASSPHRASE_EXEMPT_LENGTH}+ characters long"
            )


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")
