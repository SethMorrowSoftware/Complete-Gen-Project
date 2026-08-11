"""Named accounts, the login decision, and the internet-facing hardening.

Covers, in order:
  - password policy and username validation
  - the UserService guard rails (last admin, duplicate, roles)
  - the login decision: enumeration resistance, lockout, TOTP, recovery codes
  - TOTP against the RFC 6238 published test vectors
  - the HTTP surface: login/logout/session revocation/role gates/user CRUD
  - the response-hardening middleware (CSP, HSTS, HTTPS, IP allowlist)
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

from genwatch.config import AuthConfig
from genwatch.db import Database
from genwatch.main import create_app
from genwatch.services import totp as totp_mod
from genwatch.services.auth import (
    PasswordPolicyError,
    hash_password,
    validate_password_strength,
)
from genwatch.services.users import (
    UserError,
    UserService,
    ensure_bootstrap_user,
    normalise_username,
)

GOOD_PW = "Trebuchet-Ferry-91"
OTHER_PW = "Windlass-Quarry-42"


@pytest.fixture
def svc(tmp_path):
    db = Database(tmp_path / "users.sqlite")
    yield UserService(db, AuthConfig(jwt_secret="x" * 64))
    db.close()


# ─── Password policy ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pw, why",
    [
        ("short1!", "under the length floor"),
        ("password123", "on the common list"),
        ("Password1!", "common word with digits/symbol appended"),
        ("aaaaaaaaaaaaaaa", "too few distinct characters"),
        ("abcdefghijklmno", "alphabet run"),
        ("qwertyuiopasdfg", "keyboard run"),
        ("alllowercaseonly", "only one character class under the passphrase length"),
        ("  Trebuchet-Ferry-91  ", "surrounding whitespace"),
    ],
)
def test_password_policy_rejects(pw, why):
    with pytest.raises(PasswordPolicyError):
        validate_password_strength(pw)


@pytest.mark.parametrize(
    "pw",
    [
        GOOD_PW,
        "correct horse battery staple",  # long passphrase, one class — allowed
        "Zx9$mQ2!vB7w",
    ],
)
def test_password_policy_accepts(pw):
    validate_password_strength(pw)


def test_password_policy_rejects_password_containing_username():
    with pytest.raises(PasswordPolicyError, match="username"):
        validate_password_strength("Ferry-alice-9912", username="alice")


def test_password_policy_honours_configured_minimum(svc):
    svc.cfg = AuthConfig(password_min_length=24)
    with pytest.raises(UserError, match="24 characters"):
        svc.create(username="bob", password=GOOD_PW)


def test_password_min_length_has_a_floor():
    """An operator setting password_min_length: 1 must not get a 1-char
    password — the floor is 8 regardless of config."""
    with pytest.raises(PasswordPolicyError):
        validate_password_strength("Ab1!", min_length=1)


# ─── Usernames ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expect", [("Alice", "alice"), ("  BOB  ", "bob"), ("a.b_c-1", "a.b_c-1")])
def test_username_normalised(raw, expect):
    assert normalise_username(raw) == expect


@pytest.mark.parametrize("raw", ["", "ab", "x" * 33, "has space", "bad!char", "_leading", "über"])
def test_username_rejected(raw):
    with pytest.raises(UserError):
        normalise_username(raw)


def test_usernames_are_case_insensitive(svc):
    svc.create(username="Alice", password=GOOD_PW, role="admin")
    with pytest.raises(UserError, match="already exists"):
        svc.create(username="ALICE", password=OTHER_PW)
    assert svc.get("aLiCe")["username"] == "alice"


# ─── Guard rails ──────────────────────────────────────────────────────────


def test_cannot_delete_disable_or_demote_the_last_admin(svc):
    svc.create(username="admin", password=GOOD_PW, role="admin")
    svc.create(username="viewer1", password=OTHER_PW, role="viewer")
    for action in (
        lambda: svc.delete("admin"),
        lambda: svc.set_disabled("admin", True),
        lambda: svc.set_role("admin", "operator"),
    ):
        with pytest.raises(UserError, match="last enabled admin"):
            action()
    # With a second admin present, all three become legal.
    svc.create(username="admin2", password="Bulwark-Cinder-77", role="admin")
    svc.set_role("admin", "operator")
    assert svc.get("admin")["role"] == "operator"


def test_disabling_a_user_revokes_their_sessions(svc):
    u = svc.create(username="bob", password=GOOD_PW, role="operator")
    svc.db.session_create(
        jti="j1", user_id=u["id"], username="bob", expires_at=time.time() + 3600
    )
    assert len(svc.db.sessions_for_user(u["id"])) == 1
    svc.create(username="admin", password=OTHER_PW, role="admin")
    svc.set_disabled("bob", True)
    assert svc.db.sessions_for_user(u["id"]) == []


def test_password_change_bumps_token_epoch(svc):
    u = svc.create(username="bob", password=GOOD_PW, role="operator")
    before = u["token_epoch"]
    svc.set_password("bob", OTHER_PW)
    assert svc.get("bob")["token_epoch"] == before + 1


def test_invalid_role_rejected(svc):
    with pytest.raises(UserError, match="role must be"):
        svc.create(username="bob", password=GOOD_PW, role="superuser")


# ─── The login decision ───────────────────────────────────────────────────


def test_unknown_user_and_wrong_password_are_indistinguishable(svc):
    svc.create(username="alice", password=GOOD_PW, role="admin")
    unknown = svc.authenticate(username="nobody", password=GOOD_PW)
    wrong = svc.authenticate(username="alice", password="Wrong-Passphrase-11")
    assert unknown.ok is False and wrong.ok is False
    # Same machine-readable code AND same operator-facing text: anything
    # that differs is an account-enumeration oracle.
    assert unknown.code == wrong.code == "invalid_credentials"
    assert unknown.message == wrong.message
    # The internal audit detail may differ — that's local, not returned.
    assert unknown.audit_detail != wrong.audit_detail


def test_disabled_account_gets_the_generic_failure(svc):
    svc.create(username="admin", password=OTHER_PW, role="admin")
    svc.create(username="bob", password=GOOD_PW, role="operator")
    svc.set_disabled("bob", True)
    r = svc.authenticate(username="bob", password=GOOD_PW)
    assert r.ok is False
    assert r.code == "invalid_credentials"
    assert r.audit_detail == "account_disabled"


def test_successful_login_returns_the_user(svc):
    svc.create(username="alice", password=GOOD_PW, role="admin")
    r = svc.authenticate(username="alice", password=GOOD_PW)
    assert r.ok is True
    assert r.user["username"] == "alice"
    assert r.user["role"] == "admin"


def test_lockout_arms_after_threshold_and_escalates(svc):
    svc.cfg = AuthConfig(lockout_threshold=3, lockout_seconds=60, lockout_max_seconds=600)
    svc.create(username="alice", password=GOOD_PW, role="admin")
    now = time.time()
    for _ in range(2):
        assert svc.authenticate(username="alice", password="nope", now=now).ok is False
    assert float(svc.get("alice")["locked_until"]) == 0.0

    svc.authenticate(username="alice", password="nope", now=now)  # 3rd → locked
    locked_until = float(svc.get("alice")["locked_until"])
    assert 55 <= locked_until - now <= 65

    # A 4th failure doubles the window rather than re-arming the same one.
    svc.authenticate(username="alice", password="nope", now=now)
    assert float(svc.get("alice")["locked_until"]) - now > 100


def test_lockout_is_only_disclosed_to_a_correct_password(svc):
    """The 'account locked' message is itself information. It's only shown
    to someone who already proved they know the password; a guesser gets
    the same generic failure as always."""
    svc.cfg = AuthConfig(lockout_threshold=1, lockout_seconds=300)
    svc.create(username="alice", password=GOOD_PW, role="admin")
    svc.authenticate(username="alice", password="nope")  # locks

    guesser = svc.authenticate(username="alice", password="still-wrong")
    assert guesser.code == "invalid_credentials"

    owner = svc.authenticate(username="alice", password=GOOD_PW)
    assert owner.ok is False
    assert owner.code == "account_locked"
    assert owner.retry_after_s > 0


def test_successful_login_clears_the_failure_counter(svc):
    svc.cfg = AuthConfig(lockout_threshold=5, lockout_seconds=60)
    svc.create(username="alice", password=GOOD_PW, role="admin")
    svc.authenticate(username="alice", password="nope")
    assert svc.get("alice")["failed_attempts"] == 1
    r = svc.authenticate(username="alice", password=GOOD_PW)
    assert r.ok
    svc.db.user_record_login_ok(r.user["id"], "1.2.3.4")
    assert svc.get("alice")["failed_attempts"] == 0


def test_lockout_can_be_cleared_by_an_admin(svc):
    svc.cfg = AuthConfig(lockout_threshold=1, lockout_seconds=300)
    svc.create(username="alice", password=GOOD_PW, role="admin")
    svc.authenticate(username="alice", password="nope")
    assert svc.authenticate(username="alice", password=GOOD_PW).code == "account_locked"
    svc.unlock("alice")
    assert svc.authenticate(username="alice", password=GOOD_PW).ok is True


# ─── TOTP ─────────────────────────────────────────────────────────────────


def test_totp_matches_rfc6238_vectors():
    """RFC 6238 Appendix B, SHA-1 rows. The shared secret there is the
    ASCII string '12345678901234567890'; base32 of that is the value
    below. If this test ever fails, every authenticator app on the
    planet disagrees with us."""
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    for unix_time, expected in [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ]:
        assert totp_mod.code_at(secret, totp_mod.counter_for(unix_time)) == expected


def test_totp_accepts_drift_and_rejects_garbage():
    secret = totp_mod.generate_secret()
    now = 1_700_000_000.0
    counter = totp_mod.counter_for(now)
    # one step early and one step late are accepted (phone clock drift)
    assert totp_mod.verify(secret, totp_mod.code_at(secret, counter - 1), now=now) == counter - 1
    assert totp_mod.verify(secret, totp_mod.code_at(secret, counter + 1), now=now) == counter + 1
    # two steps out is not
    assert totp_mod.verify(secret, totp_mod.code_at(secret, counter + 2), now=now) is None
    assert totp_mod.verify(secret, "000000", now=now) is None
    assert totp_mod.verify(secret, "12345", now=now) is None
    assert totp_mod.verify(secret, "", now=now) is None


def test_totp_code_cannot_be_replayed():
    """A code observed over someone's shoulder (or captured by a phishing
    proxy) must not be reusable inside its own 30-second step."""
    secret = totp_mod.generate_secret()
    now = 1_700_000_000.0
    counter = totp_mod.counter_for(now)
    code = totp_mod.code_at(secret, counter)
    assert totp_mod.verify(secret, code, now=now, last_counter=0) == counter
    assert totp_mod.verify(secret, code, now=now, last_counter=counter) is None


def test_totp_enrollment_requires_a_valid_code_before_activating(svc):
    svc.create(username="alice", password=GOOD_PW, role="admin")
    out = svc.begin_totp_enrollment("alice", issuer="GenWatch")
    assert out["uri"].startswith("otpauth://totp/GenWatch%3Aalice?")
    # Not active yet — a failed QR scan must not lock the operator out.
    assert svc.get("alice")["totp_enabled"] == 0
    with pytest.raises(UserError, match="didn't match"):
        svc.confirm_totp_enrollment("alice", "000000")
    code = totp_mod.code_at(out["secret"], totp_mod.counter_for())
    codes = svc.confirm_totp_enrollment("alice", code)
    assert svc.get("alice")["totp_enabled"] == 1
    assert len(codes) == totp_mod.RECOVERY_CODE_COUNT


def test_login_with_totp_required_prompts_then_accepts(svc):
    svc.create(username="alice", password=GOOD_PW, role="admin")
    out = svc.begin_totp_enrollment("alice", issuer="GenWatch")
    svc.confirm_totp_enrollment("alice", totp_mod.code_at(out["secret"], totp_mod.counter_for()))

    # Right password, no code → prompt (and only after the password was
    # correct, so it isn't an enumeration signal).
    r = svc.authenticate(username="alice", password=GOOD_PW)
    assert r.ok is False and r.code == "totp_required"
    # Wrong password + right code → still the generic failure.
    assert svc.authenticate(username="alice", password="nope", totp_code="000000").code == (
        "invalid_credentials"
    )
    # Right password + wrong code → totp_invalid, and it counts as a failure.
    r = svc.authenticate(username="alice", password=GOOD_PW, totp_code="000000")
    assert r.code == "totp_invalid"
    assert svc.get("alice")["failed_attempts"] >= 1

    # Right password + right code → in. (Enrollment consumed the current
    # step's counter, so use the next one.)
    later = time.time() + totp_mod.PERIOD_S
    good = totp_mod.code_at(out["secret"], totp_mod.counter_for(later))
    assert svc.authenticate(username="alice", password=GOOD_PW, totp_code=good, now=later).ok


def test_recovery_code_works_once(svc):
    svc.create(username="alice", password=GOOD_PW, role="admin")
    out = svc.begin_totp_enrollment("alice", issuer="GenWatch")
    codes = svc.confirm_totp_enrollment(
        "alice", totp_mod.code_at(out["secret"], totp_mod.counter_for())
    )
    r = svc.authenticate(username="alice", password=GOOD_PW, totp_code=codes[0])
    assert r.ok is True and r.used_recovery_code is True
    assert r.recovery_codes_remaining == len(codes) - 1
    # Burned — the same code is now just a wrong second factor.
    assert svc.authenticate(username="alice", password=GOOD_PW, totp_code=codes[0]).code == (
        "totp_invalid"
    )
    # Formatting is forgiving: dashes and case are normalised away.
    messy = codes[1].upper().replace("-", " ")
    assert svc.authenticate(username="alice", password=GOOD_PW, totp_code=messy).ok is True


def test_require_totp_refuses_unenrolled_accounts(svc):
    """A global 2FA policy that silently exempts the accounts which never
    enrolled is not a policy."""
    svc.create(username="alice", password=GOOD_PW, role="admin")
    svc.cfg = AuthConfig(require_totp=True)
    r = svc.authenticate(username="alice", password=GOOD_PW)
    assert r.ok is False
    assert r.code == "totp_enrollment_required"


# ─── Bootstrap ────────────────────────────────────────────────────────────


def test_bootstrap_seeds_admin_from_legacy_hash(tmp_path):
    db = Database(tmp_path / "b.sqlite")
    cfg = AuthConfig(admin_password_hash=hash_password("legacy-password"), jwt_secret="x" * 64)
    res = ensure_bootstrap_user(db, cfg)
    assert res.created is True and res.username == "admin"
    svc = UserService(db, cfg)
    # The operator's existing password still works — they just have a
    # username to go with it now.
    assert svc.authenticate(username="admin", password="legacy-password").ok is True
    assert svc.get("admin")["role"] == "admin"
    db.close()


def test_bootstrap_is_idempotent_and_does_not_resurrect(tmp_path):
    db = Database(tmp_path / "b.sqlite")
    cfg = AuthConfig(admin_password_hash=hash_password("legacy-password"))
    ensure_bootstrap_user(db, cfg)
    UserService(db, cfg).create(username="alice", password=GOOD_PW, role="admin")
    UserService(db, cfg).delete("admin")
    # Restart with the hash still in config: no ghost account comes back,
    # because accounts already exist.
    assert ensure_bootstrap_user(db, cfg).created is False
    assert {u["username"] for u in db.user_list()} == {"alice"}
    db.close()


def test_bootstrap_skips_placeholder_hash(tmp_path):
    db = Database(tmp_path / "b.sqlite")
    assert ensure_bootstrap_user(db, AuthConfig(admin_password_hash="REPLACE_ME")).created is False
    assert db.user_count() == 0
    db.close()


def test_bootstrap_honours_custom_username(tmp_path):
    db = Database(tmp_path / "b.sqlite")
    cfg = AuthConfig(
        admin_password_hash=hash_password("legacy-password"), bootstrap_username="Castle.Ops"
    )
    assert ensure_bootstrap_user(db, cfg).username == "castle.ops"
    db.close()


# ─── HTTP surface ─────────────────────────────────────────────────────────


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test-bootstrap-pw"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("mock: true\n")
    monkeypatch.setenv("GENWATCH_CONFIG_PATH", str(cfg_file))
    yield cfg_file


@pytest.fixture
async def client(app_env):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.15)
            yield c, app


async def _login(c, username="admin", password="test-bootstrap-pw", **extra):
    return await c.post("/api/auth/login", json={"username": username, "password": password, **extra})


async def test_login_requires_a_username(client):
    c, _ = client
    r = await c.post("/api/auth/login", json={"password": "test-bootstrap-pw"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_credentials"


async def test_login_and_me_round_trip(client):
    c, _ = client
    r = await _login(c)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operator"] == "admin" and body["role"] == "admin"
    me = (await c.get("/api/auth/me")).json()
    assert me["authenticated"] is True
    assert me["operator"] == "admin"
    assert me["totpEnabled"] is False


async def test_wrong_password_is_generic_and_audited(client):
    c, app = client
    r = await _login(c, password="definitely-wrong")
    assert r.status_code == 401
    assert r.json()["detail"] == {
        "code": "invalid_credentials",
        "message": "invalid username or password",
    }
    rows = app.state.db.read_events(limit=5)  # events are separate; check audit directly
    assert rows is not None
    with app.state.db._reader() as conn:
        audit = conn.execute(
            "SELECT operator, action, result FROM audit WHERE action='auth.login' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert audit["result"] == "denied"


async def test_logout_revokes_the_session_server_side(client):
    """The real test of logout: the *same cookie*, replayed after logout,
    must not work. Dropping the cookie client-side is not revocation."""
    c, _ = client
    await _login(c)
    cookie = c.cookies.get("genwatch_session")
    assert cookie
    assert (await c.get("/api/status")).status_code == 200

    await c.post("/api/auth/logout")
    assert (await c.get("/api/status")).status_code == 401

    # Replay the captured cookie value as a bearer token — still dead.
    r = await c.get("/api/status", headers={"Authorization": f"Bearer {cookie}"})
    assert r.status_code == 401


async def test_password_change_revokes_sessions_but_keeps_the_caller_signed_in(client):
    c, app = client
    await _login(c)
    # A second, independent session for the same account.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as other:
        await _login(other)
        assert (await other.get("/api/status")).status_code == 200

        r = await c.post(
            "/api/auth/password",
            json={"current_password": "test-bootstrap-pw", "new_password": GOOD_PW},
        )
        assert r.status_code == 200, r.text
        # The other browser is signed out…
        assert (await other.get("/api/status")).status_code == 401
    # …and the caller keeps working on a freshly-issued session.
    assert (await c.get("/api/status")).status_code == 200
    # The new password is what works now.
    assert (await _login(c, password="test-bootstrap-pw")).status_code == 401
    assert (await _login(c, password=GOOD_PW)).status_code == 200


async def test_password_change_requires_the_current_password(client):
    c, _ = client
    await _login(c)
    r = await c.post(
        "/api/auth/password", json={"current_password": "wrong", "new_password": GOOD_PW}
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "invalid_credentials"


async def test_password_change_enforces_the_policy(client):
    c, _ = client
    await _login(c)
    r = await c.post(
        "/api/auth/password",
        json={"current_password": "test-bootstrap-pw", "new_password": "password123"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "weak_password"


async def test_admin_can_create_and_list_users(client):
    c, _ = client
    await _login(c)
    r = await c.post(
        "/api/users",
        json={"username": "bob", "password": GOOD_PW, "role": "operator",
              "must_change_password": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "operator"

    listing = (await c.get("/api/users")).json()["users"]
    names = {u["username"] for u in listing}
    assert names == {"admin", "bob"}
    # No secret material ever crosses the wire.
    blob = str(listing)
    assert "password_hash" not in blob and "totp_secret" not in blob and "$2b$" not in blob


async def test_role_gates_are_enforced(client):
    """viewer reads, operator commands, admin configures."""
    c, app = client
    await _login(c)
    await c.post("/api/users", json={"username": "vic", "password": GOOD_PW, "role": "viewer",
                                     "must_change_password": False})
    await c.post("/api/users", json={"username": "opal", "password": OTHER_PW, "role": "operator",
                                     "must_change_password": False})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as viewer:
        assert (await _login(viewer, "vic", GOOD_PW)).status_code == 200
        assert (await viewer.get("/api/status")).status_code == 200      # can read
        assert (await viewer.get("/api/control/confirm")).status_code == 403  # cannot command
        assert (await viewer.get("/api/users")).status_code == 403       # cannot administer

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as operator:
        assert (await _login(operator, "opal", OTHER_PW)).status_code == 200
        assert (await operator.get("/api/status")).status_code == 200
        assert (await operator.get("/api/control/confirm")).status_code == 200
        assert (await operator.get("/api/users")).status_code == 403
        # Admin-only config write stays closed.
        assert (await operator.put("/api/config", json={"ws_push_ms": 2000})).status_code == 403


async def test_must_change_password_blocks_everything_else(client):
    c, app = client
    await _login(c)
    await c.post("/api/users", json={"username": "temp", "password": GOOD_PW,
                                     "role": "operator", "must_change_password": True})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as t:
        r = await _login(t, "temp", GOOD_PW)
        assert r.status_code == 200
        assert r.json()["mustChangePassword"] is True
        # Console is closed until the temporary password is replaced.
        blocked = await t.get("/api/status")
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "password_change_required"
        # But the fix-it endpoint is reachable.
        r = await t.post(
            "/api/auth/password",
            json={"current_password": GOOD_PW, "new_password": "Palisade-Kettle-58"},
        )
        assert r.status_code == 200, r.text
        assert (await t.get("/api/status")).status_code == 200


async def test_admin_cannot_delete_the_last_admin_over_http(client):
    c, _ = client
    await _login(c)
    r = await c.delete("/api/users/admin")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_admin"


async def test_admin_can_revoke_another_users_sessions(client):
    c, app = client
    await _login(c)
    await c.post("/api/users", json={"username": "bob", "password": GOOD_PW,
                                     "role": "operator", "must_change_password": False})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as bob:
        await _login(bob, "bob", GOOD_PW)
        assert (await bob.get("/api/status")).status_code == 200
        r = await c.post("/api/users/bob/revoke-sessions")
        assert r.status_code == 200 and r.json()["revoked"] == 1
        assert (await bob.get("/api/status")).status_code == 401


async def test_session_idle_timeout_expires_the_session(client):
    c, app = client
    await _login(c)
    assert (await c.get("/api/status")).status_code == 200
    # Backdate last_seen past the idle window rather than sleeping.
    idle = app.state.settings.auth.idle_timeout_minutes
    assert idle > 0
    with app.state.db._writer() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ?", (time.time() - idle * 60 - 5,))
    assert (await c.get("/api/status")).status_code == 401
    # And the row is marked revoked, not left looking live.
    with app.state.db._reader() as conn:
        row = conn.execute("SELECT revoked_at FROM sessions LIMIT 1").fetchone()
    assert row["revoked_at"] is not None


async def test_websocket_traffic_does_not_extend_the_idle_window(client):
    """The Live view is the page people leave open. If its WebSocket
    counted as activity, the idle timeout would never fire for the one
    case it exists to cover."""
    from genwatch.services import session as session_svc

    c, app = client
    await _login(c)
    db = app.state.db
    jti = db.sessions_for_user(db.user_get("admin")["id"])[0]["jti"]
    # Well past the touch interval, comfortably inside the idle window.
    stale = time.time() - 600
    with app.state.db._writer() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE jti = ?", (stale, jti))

    token = c.cookies.get("genwatch_session")
    # What the WebSocket re-validation does: checks, doesn't touch.
    session_svc.validate(db=db, settings=app.state.settings, token=token, touch=False)
    assert db.session_get(jti)["last_seen_at"] == pytest.approx(stale, abs=0.01)

    # An ordinary request (the frontend's activity keepalive) does touch.
    assert (await c.get("/api/auth/me")).status_code == 200
    assert db.session_get(jti)["last_seen_at"] > stale


# ─── "Keep me signed in" (remember me) ────────────────────────────────────


def _session_row(app, username="admin"):
    db = app.state.db
    return db.sessions_for_user(db.user_get(username)["id"])[0]


async def test_remember_me_login_mints_a_long_lived_session(client):
    c, app = client
    r = await _login(c, remember=True)
    assert r.status_code == 200, r.text
    assert r.json()["remembered"] is True

    life_s = app.state.settings.auth.remember_me_days * 86400
    # The browser cookie lives as long as the server-side session does.
    cookie = r.headers["set-cookie"]
    assert f"Max-Age={life_s}" in cookie

    row = _session_row(app)
    assert row["remember"] == 1
    assert row["expires_at"] == pytest.approx(time.time() + life_s, abs=30)

    # Visible for what it is in the sessions list and the audit trail.
    sessions = (await c.get("/api/auth/sessions")).json()["sessions"]
    assert sessions[0]["remember"] is True
    with app.state.db._reader() as conn:
        detail = conn.execute(
            "SELECT detail FROM audit WHERE action='auth.login' "
            "AND result='ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
    assert "remembered" in detail


async def test_ordinary_login_is_unchanged_by_the_feature(client):
    c, app = client
    r = await _login(c)
    assert r.status_code == 200
    assert r.json()["remembered"] is False
    assert f"Max-Age={app.state.settings.auth.session_hours * 3600}" in r.headers["set-cookie"]
    assert _session_row(app)["remember"] == 0


async def test_remembered_session_skips_the_idle_timeout(client):
    """The whole point of the checkbox: stepping away for longer than
    idle_timeout_minutes must not sign the device out."""
    c, app = client
    await _login(c, remember=True)
    idle = app.state.settings.auth.idle_timeout_minutes
    assert idle > 0
    with app.state.db._writer() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ?", (time.time() - idle * 60 - 5,))
    assert (await c.get("/api/status")).status_code == 200
    assert _session_row(app)["revoked_at"] is None


async def test_remember_me_days_zero_disables_the_feature(client):
    """With remember_me_days: 0 the checkbox is ignored server-side —
    the login succeeds but gets a plain session_hours session that the
    idle timeout applies to."""
    c, app = client
    app.state.settings.auth.remember_me_days = 0
    r = await _login(c, remember=True)
    assert r.status_code == 200
    assert r.json()["remembered"] is False
    assert f"Max-Age={app.state.settings.auth.session_hours * 3600}" in r.headers["set-cookie"]
    row = _session_row(app)
    assert row["remember"] == 0
    idle = app.state.settings.auth.idle_timeout_minutes
    with app.state.db._writer() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ?", (time.time() - idle * 60 - 5,))
    assert (await c.get("/api/status")).status_code == 401


async def test_remembered_session_renews_once_half_spent(client):
    """Sliding renewal: /me on an aging remembered session pushes the
    expiry back out to a full lifetime and re-issues the cookie, so a
    device in regular use never reaches the login page again."""
    c, app = client
    await _login(c, remember=True)
    jti = _session_row(app)["jti"]
    life_s = app.state.settings.auth.remember_me_days * 86400

    # Fresh session: plenty of runway, /me must not rewrite anything.
    r = await c.get("/api/auth/me")
    assert "set-cookie" not in r.headers
    assert _session_row(app)["expires_at"] == pytest.approx(time.time() + life_s, abs=30)

    # Age it past the renewal threshold (40% of life left).
    with app.state.db._writer() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE jti = ?",
            (time.time() + life_s * 0.4, jti),
        )
    r = await c.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["authenticated"] is True
    assert f"Max-Age={life_s}" in r.headers["set-cookie"]
    row = _session_row(app)
    assert row["jti"] == jti  # same session, new horizon
    assert row["expires_at"] == pytest.approx(time.time() + life_s, abs=30)

    # And the renewed cookie is genuinely accepted.
    assert (await c.get("/api/status")).status_code == 200


async def test_renewal_never_resurrects_a_revoked_session(client):
    """A revocation racing the renewal must win — renewal extends live
    sessions, it is not a back door past logout or an admin revoke."""
    from genwatch.services import session as session_svc

    c, app = client
    await _login(c, remember=True)
    db = app.state.db
    jti = _session_row(app)["jti"]
    with db._writer() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE jti = ?", (time.time() + 60, jti)
        )
    db.session_revoke(jti)
    assert (
        session_svc.renew_if_due(db=db, auth_cfg=app.state.settings.auth, jti=jti)
        is None
    )
    assert db.session_get(jti)["expires_at"] == pytest.approx(time.time() + 60, abs=30)


async def test_password_change_keeps_the_remembered_flag(client):
    """Rotating a password re-issues the caller's session; doing the
    right thing shouldn't cost the device its keep-me-signed-in status."""
    c, app = client
    await _login(c, remember=True)
    r = await c.post(
        "/api/auth/password",
        json={"current_password": "test-bootstrap-pw", "new_password": "Palisade-Kettle-58"},
    )
    assert r.status_code == 200, r.text
    life_s = app.state.settings.auth.remember_me_days * 86400
    rows = app.state.db.sessions_for_user(app.state.db.user_get("admin")["id"])
    live = [x for x in rows if x["revoked_at"] is None]
    assert len(live) == 1 and live[0]["remember"] == 1
    assert live[0]["expires_at"] == pytest.approx(time.time() + life_s, abs=30)
    assert (await c.get("/api/status")).status_code == 200


def test_sessions_table_migration_adds_remember_column(tmp_path):
    """A database created before the remember column existed gains it on
    open — CREATE TABLE IF NOT EXISTS alone never alters a deployed
    SQLite file, so without the migration every upgraded site would
    crash on the first login."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT);
        CREATE TABLE sessions (
            jti          TEXT    PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            username     TEXT    NOT NULL,
            created_at   REAL    NOT NULL,
            last_seen_at REAL    NOT NULL,
            expires_at   REAL    NOT NULL,
            revoked_at   REAL,
            ip           TEXT    NOT NULL DEFAULT '',
            user_agent   TEXT    NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute("INSERT INTO users (username) VALUES ('old')")
    conn.execute(
        "INSERT INTO sessions (jti, user_id, username, created_at, last_seen_at, expires_at) "
        "VALUES ('old-jti', 1, 'old', 1, 1, 2)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        # Pre-existing row reads as not-remembered; new rows can set it.
        assert db.session_get("old-jti")["remember"] == 0
        db.session_create(
            jti="new-jti", user_id=1, username="old",
            expires_at=time.time() + 60, remember=True,
        )
        assert db.session_get("new-jti")["remember"] == 1
        # Idempotent: a second open must not try to re-add the column.
        db.close()
        Database(path).close()
    finally:
        db.close()


async def test_login_is_rate_limited_per_account_across_addresses(client):
    """The per-IP bucket is porous against a botnet rotating source
    addresses; the per-account bucket follows the target instead.

    Resetting the IP bucket between attempts simulates each guess
    arriving from a fresh address, so what trips here can only be the
    per-account limiter."""
    c, app = client
    ip_limiter = app.state.login_limiter
    # Take the account lockout out of the picture: this test is about the
    # rate limiter, and the lockout has its own test.
    app.state.settings.auth.lockout_seconds = 0

    last = None
    for _ in range(30):
        ip_limiter.reset("127.0.0.1")  # "next attempt, different botnet host"
        last = await _login(c, password="wrong-guess")
        if last.status_code == 429:
            break
    assert last.status_code == 429
    assert last.json()["detail"]["code"] == "rate_limited"
    assert int(last.headers["Retry-After"]) > 0


async def test_locked_account_returns_429_with_retry_after(client):
    c, app = client
    # Trip the account lockout directly (the per-IP limiter would fire
    # first otherwise) and confirm the HTTP shape.
    app.state.db.user_update(
        app.state.db.user_get("admin")["id"], locked_until=time.time() + 300
    )
    r = await _login(c)
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "account_locked"
    assert int(r.headers["Retry-After"]) > 0


# ─── Response hardening ───────────────────────────────────────────────────


async def test_security_headers_are_present(client):
    c, _ = client
    r = await c.get("/api/auth/me")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "X-Robots-Tag" in h
    # API responses must never be cached by an intermediary.
    assert h["Cache-Control"] == "no-store"
    # No HSTS on a plain-HTTP request — that would brick a LAN deployment.
    assert "Strict-Transport-Security" not in h


async def test_csp_allows_the_same_origin_websocket(client):
    """The Live view opens a WS to its own origin. A CSP whose connect-src
    forgets that silently kills live updates."""
    c, _ = client
    csp = (await c.get("/api/auth/me")).headers["Content-Security-Policy"]
    assert "connect-src 'self' ws://test wss://test" in csp
    assert "{ws_self}" not in csp


async def test_hsts_only_on_https(app_env):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test", headers={"X-Requested-With": "pytest"}
    ) as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.1)
            r = await c.get("/api/auth/me")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=31536000")


async def test_require_https_redirects_and_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test-bootstrap-pw"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_SECURITY__REQUIRE_HTTPS", "true")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Requested-With": "pytest"}
    ) as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.1)
            r = await c.get("/api/auth/me")
            assert r.status_code == 308
            assert r.headers["location"].startswith("https://")
            # A POST can't be safely redirected — refuse it outright so a
            # password is never re-sent over plain HTTP.
            r = await _login(c)
            assert r.status_code == 400
            assert r.json()["detail"]["code"] == "https_required"


async def test_ip_allowlist_blocks_strangers_but_never_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test-bootstrap-pw"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_SECURITY__IP_ALLOWLIST", '["198.51.100.0/24"]')

    app = create_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.1)
        # httpx's ASGI transport reports 127.0.0.1 as the client, which
        # must always pass — otherwise a typo'd CIDR means a site visit.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/api/auth/me")).status_code == 200
        # A stranger from outside the allowlist is refused before any
        # handler runs.
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 5555))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/auth/me")
            assert r.status_code == 403
            assert r.json()["detail"]["code"] == "ip_not_allowed"
        # …and an address inside it is not.
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 5555))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/api/auth/me")).status_code == 200


# ─── CLI ──────────────────────────────────────────────────────────────────
# The CLI is the only way to create the *first* account and the only way
# back in after an admin lockout, so it gets the same coverage as the API.


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("GENWATCH_CONFIG_PATH", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    yield tmp_path


def _run_cli(monkeypatch, argv: list[str], answers: list[str] | None = None) -> int:
    import getpass

    from genwatch.__main__ import main

    if answers is not None:
        it = iter(answers)
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(it))
    monkeypatch.setattr("sys.argv", ["genwatch", *argv])
    return main()


def _store(tmp_path):
    return Database(tmp_path / "db.sqlite")


def test_cli_useradd_creates_an_account(cli_env, monkeypatch, capsys):
    rc = _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW])
    assert rc == 0
    out = capsys.readouterr()
    assert "created alice" in out.out
    # The plaintext must never reach a stream that could be logged.
    assert GOOD_PW not in out.out and GOOD_PW not in out.err

    db = _store(cli_env)
    assert UserService(db, AuthConfig()).authenticate(username="alice", password=GOOD_PW).ok
    assert db.user_get("alice")["role"] == "admin"
    db.close()


def test_cli_useradd_reprompts_on_a_weak_password(cli_env, monkeypatch, capsys):
    """A rejected password costs a re-prompt, not a restart."""
    rc = _run_cli(
        monkeypatch,
        ["useradd", "bob"],
        ["password1234", "password1234", GOOD_PW, GOOD_PW],
    )
    assert rc == 0
    assert "common password" in capsys.readouterr().err
    db = _store(cli_env)
    assert db.user_get("bob") is not None
    db.close()


def test_cli_useradd_rejects_mismatched_confirmation(cli_env, monkeypatch, capsys):
    rc = _run_cli(monkeypatch, ["useradd", "bob"], [GOOD_PW, "different", GOOD_PW, GOOD_PW])
    assert rc == 0
    assert "do not match" in capsys.readouterr().err


def test_cli_userlist_reports_state(cli_env, monkeypatch, capsys):
    _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW])
    db = _store(cli_env)
    svc = UserService(db, AuthConfig(lockout_threshold=1, lockout_seconds=600))
    svc.create(username="bob", password=OTHER_PW, role="viewer")
    svc.authenticate(username="bob", password="nope")  # lock bob out
    db.close()

    assert _run_cli(monkeypatch, ["userlist"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "admin" in out
    assert "locked" in out


def test_cli_refuses_to_strand_the_site(cli_env, monkeypatch, capsys):
    """Same guard rail as the API: the CLI cannot delete the last admin."""
    _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW])
    rc = _run_cli(monkeypatch, ["userdel", "alice"])
    assert rc == 1
    assert "last enabled admin" in capsys.readouterr().err


def test_cli_password_reset_and_unlock(cli_env, monkeypatch, capsys):
    _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW])
    assert _run_cli(monkeypatch, ["userpasswd", "alice"], [OTHER_PW, OTHER_PW]) == 0
    assert "all their sessions were signed out" in capsys.readouterr().out

    db = _store(cli_env)
    svc = UserService(db, AuthConfig())
    assert svc.authenticate(username="alice", password=OTHER_PW).ok
    db.user_update(db.user_get("alice")["id"], locked_until=time.time() + 600)
    db.close()

    assert _run_cli(monkeypatch, ["userunlock", "alice"]) == 0
    db = _store(cli_env)
    assert float(db.user_get("alice")["locked_until"]) == 0.0
    db.close()


def test_cli_role_and_disable_round_trip(cli_env, monkeypatch, capsys):
    _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW])
    _run_cli(monkeypatch, ["useradd", "bob"], [OTHER_PW, OTHER_PW])
    assert _run_cli(monkeypatch, ["userrole", "bob", "viewer"]) == 0
    assert _run_cli(monkeypatch, ["userdisable", "bob"]) == 0
    db = _store(cli_env)
    assert db.user_get("bob")["role"] == "viewer"
    assert db.user_get("bob")["disabled"] == 1
    db.close()
    assert _run_cli(monkeypatch, ["userenable", "bob"]) == 0
    db = _store(cli_env)
    assert db.user_get("bob")["disabled"] == 0
    db.close()


def test_cli_totp_enrollment_prints_recovery_codes(cli_env, monkeypatch, capsys):
    _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW])

    # The command prints the secret, then reads the confirming code from
    # stdin. Feed it a genuine code derived from what it just stored.
    def fake_input(_prompt=""):
        db = _store(cli_env)
        secret = db.user_get("alice")["totp_secret"]
        db.close()
        return totp_mod.code_at(secret, totp_mod.counter_for())

    monkeypatch.setattr("builtins.input", fake_input)
    assert _run_cli(monkeypatch, ["usertotp", "alice"]) == 0
    out = capsys.readouterr().out
    assert "otpauth://totp/" in out
    assert "recovery codes" in out
    db = _store(cli_env)
    assert db.user_get("alice")["totp_enabled"] == 1
    assert db.recovery_codes_remaining(db.user_get("alice")["id"]) == totp_mod.RECOVERY_CODE_COUNT
    db.close()

    assert _run_cli(monkeypatch, ["usertotp-off", "alice"]) == 0
    db = _store(cli_env)
    assert db.user_get("alice")["totp_enabled"] == 0
    db.close()


def test_cli_uses_the_config_path_from_the_environment(monkeypatch, tmp_path):
    """`genwatch useradd` must land in the database the *service* uses.
    The systemd unit sets GENWATCH_CONFIG_PATH; an account created into
    some other database would look created but never authenticate."""
    data_dir = tmp_path / "servicedata"
    cfg = tmp_path / "elsewhere.yaml"
    cfg.write_text(f"data_dir: {data_dir}\n")
    monkeypatch.setenv("GENWATCH_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("GENWATCH_DATA_DIR", raising=False)
    monkeypatch.delenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert _run_cli(monkeypatch, ["useradd", "alice", "--role", "admin"], [GOOD_PW, GOOD_PW]) == 0
    db = Database(data_dir / "db.sqlite")
    assert db.user_get("alice") is not None
    db.close()


# ─── Boot-time refusals ───────────────────────────────────────────────────


async def test_lifespan_refuses_when_no_admin_and_no_legacy_hash(monkeypatch, tmp_path):
    monkeypatch.delenv("GENWATCH_MOCK", raising=False)
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "y" * 64)
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", "REPLACE_ME")
    monkeypatch.setenv("GENWATCH_TRANSPORT", "tcp")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__HOST", "127.0.0.1")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__PORT", "1")

    app = create_app()
    with pytest.raises(RuntimeError, match="No enabled admin account"):
        async with app.router.lifespan_context(app):
            pass


async def test_public_exposure_refuses_unsafe_config(monkeypatch, tmp_path):
    """Declaring the console internet-facing turns the deployment
    checklist from advice into a boot gate."""
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test-bootstrap-pw"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_SECURITY__PUBLIC_EXPOSURE", "true")

    app = create_app()
    with pytest.raises(RuntimeError, match="mock mode"):
        async with app.router.lifespan_context(app):
            pass


async def test_public_exposure_refuses_disabled_lockout(monkeypatch, tmp_path):
    monkeypatch.delenv("GENWATCH_MOCK", raising=False)
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test-bootstrap-pw"))
    monkeypatch.setenv("GENWATCH_AUTH__JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GENWATCH_AUTH__LOCKOUT_SECONDS", "0")
    monkeypatch.setenv("GENWATCH_SECURITY__PUBLIC_EXPOSURE", "true")
    monkeypatch.setenv("GENWATCH_TRANSPORT", "tcp")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__HOST", "127.0.0.1")
    monkeypatch.setenv("GENWATCH_MODBUS_TCP__PORT", "1")

    app = create_app()
    with pytest.raises(RuntimeError, match="lockout"):
        async with app.router.lifespan_context(app):
            pass
