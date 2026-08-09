"""RFC 6238 TOTP — time-based second factor for the login.

Implemented against the stdlib (``hmac`` / ``hashlib`` / ``base64``)
rather than pulling in ``pyotp``. The algorithm is ~30 lines and the
install is hash-pinned and offline (see ``backend/requirements.lock``),
so a new runtime dependency has a real cost here; the maths does not.

Interoperability: SHA-1, 6 digits, 30-second period — the defaults every
authenticator app (Google Authenticator, Authy, 1Password, Aegis, Yubico
Authenticator, Bitwarden) assumes. SHA-1 is correct here and not a
weakness: HMAC-SHA1 is unbroken, and the RFC 6238 default is what the
apps actually implement.

Replay: ``verify`` returns the *counter* it matched so the caller can
persist it and refuse anything at or below it. Without that, a code
shoulder-surfed (or captured from a phished form) stays valid for the
rest of its 30-second step plus the drift window.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

# 160-bit secret, the RFC 4226 recommendation and what the apps expect.
SECRET_BYTES = 20
DIGITS = 6
PERIOD_S = 30
# How many 30-second steps either side of "now" are accepted. 1 → the
# code is good for ~90 s total, covering clock drift on a phone without
# meaningfully widening the guessing window (still 1-in-10^6 per try,
# and the login is rate-limited and lockout-protected).
DEFAULT_DRIFT_STEPS = 1


def generate_secret() -> str:
    """A fresh base32 secret, unpadded — the format otpauth:// URIs use."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    # Authenticator apps show secrets without padding and often with
    # spaces/lowercase when a human retypes them. Normalise all three.
    s = secret_b32.strip().replace(" ", "").upper()
    pad = "=" * (-len(s) % 8)
    return base64.b32decode(s + pad, casefold=True)


def code_at(secret_b32: str, counter: int) -> str:
    """The 6-digit code for a given 30-second counter value."""
    key = _decode_secret(secret_b32)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def counter_for(ts: float | None = None) -> int:
    return int((ts if ts is not None else time.time()) // PERIOD_S)


def verify(
    secret_b32: str,
    code: str,
    *,
    now: float | None = None,
    drift_steps: int = DEFAULT_DRIFT_STEPS,
    last_counter: int = 0,
) -> int | None:
    """Check ``code``. Returns the matched counter, or None.

    ``last_counter`` is the highest counter this account has already
    spent; anything at or below it is refused even when the maths checks
    out, which is what makes a captured code single-use.
    """
    if not secret_b32 or not code:
        return None
    cleaned = "".join(ch for ch in code if ch.isdigit())
    if len(cleaned) != DIGITS:
        return None
    try:
        _decode_secret(secret_b32)
    except Exception:  # noqa: BLE001 — malformed secret in the DB
        return None
    center = counter_for(now)
    for step in range(-drift_steps, drift_steps + 1):
        counter = center + step
        if counter <= last_counter:
            continue  # already spent — replay
        # compare_digest even though both sides are public-length digit
        # strings: costs nothing and keeps the comparison uniform.
        if hmac.compare_digest(code_at(secret_b32, counter), cleaned):
            return counter
    return None


def provisioning_uri(*, secret_b32: str, username: str, issuer: str) -> str:
    """The ``otpauth://`` URI an authenticator app scans as a QR code.

    We hand the operator this string rather than rendering a QR server-
    side: no image dependency, and every app accepts manual entry of the
    secret as a fallback.
    """
    label = quote(f"{issuer}:{username}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_S}"
    )


# ─── Recovery codes ───────────────────────────────────────────────────────
# Printed once at enrollment and stored only as SHA-256. These exist so a
# lost phone doesn't mean a site visit to a generator controller — the
# operational risk of being locked out of the console during an outage is
# real, and worse than the marginal risk of a 128-bit code.

RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"  # no l/o/0/1 — hand-transcribable


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(20))
        codes.append(f"{raw[:5]}-{raw[5:10]}-{raw[10:15]}-{raw[15:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """SHA-256 of the normalised code.

    A full-entropy 100-bit random string has no dictionary to attack, so
    a slow KDF would only cost enrollment time. Normalising (strip dashes,
    lowercase) means the operator can retype it however they like.
    """
    normalised = "".join(ch for ch in code.lower() if ch.isalnum())
    return hashlib.sha256(normalised.encode("ascii", errors="ignore")).hexdigest()
