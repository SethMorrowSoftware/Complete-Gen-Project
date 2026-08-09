"""Configurable transfer alerts and the security alerts.

The transfer alert (utility ↔ generator) is the one an operator actually
waits on, so the knobs around it are worth pinning: per-direction gating,
channel routing, the mention that decides whether Slack pushes a
notification at all, and the debounce that stops a hunting ATS from
paging five times in a minute.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from genwatch.config import SlackConfig
from genwatch.db import Database
from genwatch.main import create_app
from genwatch.services import slack as slack_mod
from genwatch.services.auth import hash_password
from genwatch.services.slack import SlackNotifier


def _notifier(tmp_path, **cfg_kwargs) -> tuple[SlackNotifier, list[dict]]:
    db = Database(tmp_path / "t.sqlite")
    cfg = SlackConfig(enabled=True, channel="#alerts", bot_token="xoxb-test", **cfg_kwargs)
    return SlackNotifier(cfg, db, site_name="Test Site"), []


def _capture(monkeypatch, sink: list[dict]):
    def fake_post(token, payload, timeout):
        sink.append(payload)
        return True, "ok"

    monkeypatch.setattr(slack_mod, "_post_slack", fake_post)


async def _drain(sink: list, expect: int = 1, timeout_s: float = 2.0) -> None:
    """Wait for the worker to flush, then settle so late sends are caught."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while len(sink) < expect and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


# ─── Per-direction gating ─────────────────────────────────────────────────


async def test_transfer_to_generator_can_be_silenced_alone(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, alert_on_transfer_to_generator=False)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await n.alert_load_source_change("generator", "utility", ts=0)
        await _drain(posted)
    finally:
        await n.stop()
    assert len(posted) == 1
    assert "UTILITY" in posted[0]["blocks"][0]["text"]["text"]


async def test_return_to_utility_can_be_silenced_alone(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, alert_on_return_to_utility=False)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await n.alert_load_source_change("generator", "utility", ts=0)
        await _drain(posted)
    finally:
        await n.stop()
    assert len(posted) == 1
    assert "GENERATOR" in posted[0]["blocks"][0]["text"]["text"]


async def test_master_switch_silences_both_directions(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, alert_on_load_source_change=False)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await n.alert_load_source_change("generator", "utility", ts=0)
        await asyncio.sleep(0.15)
    finally:
        await n.stop()
    assert posted == []


async def test_unknown_load_source_is_off_by_default_and_optional(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("generator", "unknown", ts=0)
        await asyncio.sleep(0.15)
        assert posted == []
        n.update_config(
            SlackConfig(
                enabled=True, channel="#alerts", bot_token="xoxb-test",
                alert_on_load_source_unknown=True,
            )
        )
        await n.alert_load_source_change("generator", "unknown", ts=0)
        await _drain(posted)
    finally:
        await n.stop()
    assert len(posted) == 1
    assert "UNKNOWN" in posted[0]["blocks"][0]["text"]["text"]


async def test_boot_time_unknown_to_utility_still_suppressed(tmp_path, monkeypatch):
    """Start-up firming is not a transfer and must never page anyone."""
    n, posted = _notifier(tmp_path, alert_on_load_source_unknown=True)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("unknown", "utility", ts=0)
        await asyncio.sleep(0.15)
    finally:
        await n.stop()
    assert posted == []


# ─── Routing and mentions ─────────────────────────────────────────────────


async def test_transfer_alert_uses_its_own_channel(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, channel_load_source="#on-call")
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await n.alert_alarm("0x42", "Low Oil", "alarm", ts=0)
        await _drain(posted, expect=2)
    finally:
        await n.stop()
    channels = {p["channel"] for p in posted}
    assert channels == {"#on-call", "#alerts"}


async def test_channel_override_falls_back_to_the_main_channel(tmp_path, monkeypatch):
    """Clearing an override must not silently drop the alert."""
    n, posted = _notifier(tmp_path, channel_load_source="")
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await _drain(posted)
    finally:
        await n.stop()
    assert posted[0]["channel"] == "#alerts"


async def test_mention_is_inside_the_message_body(tmp_path, monkeypatch):
    """Slack only notifies when the mention is in the text — a mention
    tucked into metadata is decorative."""
    n, posted = _notifier(
        tmp_path,
        mention_on_transfer_to_generator="<!channel>",
        mention_on_return_to_utility="<@U024BE7LH>",
    )
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await _drain(posted)
        gen = posted[0]
        assert gen["blocks"][0]["text"]["text"].startswith("*<!channel>")
        assert gen["text"].startswith("<!channel>")  # push-notification preview

        await n.alert_load_source_change("generator", "utility", ts=0)
        await _drain(posted, expect=2)
    finally:
        await n.stop()
    assert posted[1]["text"].startswith("<@U024BE7LH>")


async def test_alarms_do_not_inherit_the_transfer_mention(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, mention_on_transfer_to_generator="<!channel>")
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_alarm("0x42", "Low Oil", "alarm", ts=0)
        await _drain(posted)
    finally:
        await n.stop()
    assert "<!channel>" not in posted[0]["text"]


# ─── Debounce ─────────────────────────────────────────────────────────────


async def test_debounce_holds_the_alert_until_the_source_settles(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, load_source_debounce_s=0.3)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await asyncio.sleep(0.1)
        assert posted == [], "alert should still be held"
        await _drain(posted, timeout_s=1.5)
    finally:
        await n.stop()
    assert len(posted) == 1
    assert "GENERATOR" in posted[0]["blocks"][0]["text"]["text"]


async def test_debounce_collapses_a_full_flap_to_silence(tmp_path, monkeypatch):
    """utility → generator → utility inside the window is, from the
    channel's point of view, nothing happening."""
    n, posted = _notifier(tmp_path, load_source_debounce_s=0.4)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await asyncio.sleep(0.05)
        await n.alert_load_source_change("generator", "utility", ts=0)
        await asyncio.sleep(0.8)
    finally:
        await n.stop()
    assert posted == []


async def test_debounce_reports_the_settled_state_after_hunting(tmp_path, monkeypatch):
    """An ATS that hunts and then settles on the generator produces one
    message naming where the load actually ended up."""
    n, posted = _notifier(tmp_path, load_source_debounce_s=0.3)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await asyncio.sleep(0.05)
        await n.alert_load_source_change("generator", "utility", ts=0)
        await asyncio.sleep(0.05)
        await n.alert_load_source_change("utility", "generator", ts=0)
        await _drain(posted, timeout_s=1.5)
    finally:
        await n.stop()
    assert len(posted) == 1
    title = posted[0]["blocks"][0]["text"]["text"]
    assert "GENERATOR" in title
    assert "was utility" in title


async def test_zero_debounce_sends_immediately(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, load_source_debounce_s=0.0)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_load_source_change("utility", "generator", ts=0)
        await _drain(posted)
    finally:
        await n.stop()
    assert len(posted) == 1


# ─── Security alerts ──────────────────────────────────────────────────────


async def test_login_failure_alert_routes_to_the_security_channel(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, channel_security="#genwatch-security")
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_login_failure(username="alice", ip="203.0.113.9", reason="bad password", attempts=2)
        await _drain(posted)
    finally:
        await n.stop()
    assert posted[0]["channel"] == "#genwatch-security"
    assert "alice" in posted[0]["text"]
    assert "203.0.113.9" in str(posted[0]["blocks"])


async def test_login_failure_alerts_are_deduped_per_account(tmp_path, monkeypatch):
    """A spray attack must not be able to push real alarms out of the
    200-slot queue — the lockout alert carries the signal instead."""
    n, posted = _notifier(tmp_path)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        for i in range(6):
            await n.alert_login_failure(username="alice", ip="203.0.113.9", reason="bad", attempts=i)
        await n.alert_login_failure(username="bob", ip="203.0.113.9", reason="bad", attempts=1)
        await _drain(posted, expect=2)
    finally:
        await n.stop()
    assert len(posted) == 2  # one per account, not one per attempt


async def test_lockout_alert_is_not_deduped_away(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_login_failure(username="alice", ip="203.0.113.9", reason="bad", attempts=5)
        await n.alert_account_lockout(username="alice", ip="203.0.113.9", seconds=900)
        await _drain(posted, expect=2)
    finally:
        await n.stop()
    assert len(posted) == 2
    assert any("locked" in p["text"].lower() for p in posted)
    assert any("15 min" in str(p["blocks"]) for p in posted)


async def test_security_alerts_respect_their_flags(tmp_path, monkeypatch):
    n, posted = _notifier(
        tmp_path,
        alert_on_login_failure=False,
        alert_on_account_lockout=False,
        alert_on_user_change=False,
    )
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_login_failure(username="alice", ip="1.2.3.4", reason="bad", attempts=1)
        await n.alert_account_lockout(username="alice", ip="1.2.3.4", seconds=900)
        await n.alert_user_change(action="created", target="bob", actor="alice")
        await n.alert_login_success(username="alice", ip="1.2.3.4")  # off by default
        await asyncio.sleep(0.15)
    finally:
        await n.stop()
    assert posted == []


async def test_login_success_alert_when_enabled(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path, alert_on_login_success=True)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_login_success(username="alice", ip="1.2.3.4", method="password + TOTP")
        await _drain(posted)
    finally:
        await n.stop()
    assert "alice" in posted[0]["text"]
    assert "TOTP" in str(posted[0]["blocks"])


async def test_user_change_alert_names_actor_and_target(tmp_path, monkeypatch):
    n, posted = _notifier(tmp_path)
    _capture(monkeypatch, posted)
    await n.start()
    try:
        await n.alert_user_change(action="created", target="bob", actor="alice", detail="role operator")
        await _drain(posted)
    finally:
        await n.stop()
    blob = str(posted[0])
    assert "bob" in blob and "alice" in blob and "role operator" in blob


# ─── Test-message routing ─────────────────────────────────────────────────


async def test_test_message_exercises_the_selected_route(tmp_path, monkeypatch):
    n, _ = _notifier(
        tmp_path,
        channel_load_source="#on-call",
        channel_security="#sec",
        mention_on_transfer_to_generator="<!here>",
    )
    sent: list[dict] = []
    monkeypatch.setattr(
        slack_mod, "_post_slack", lambda t, p, to: (sent.append(p) or (True, "ok"))
    )
    ok, _ = await n.test("load_source")
    assert ok and sent[-1]["channel"] == "#on-call"
    assert sent[-1]["blocks"][0]["text"]["text"].startswith("*<!here>")

    ok, _ = await n.test("security")
    assert ok and sent[-1]["channel"] == "#sec"

    ok, _ = await n.test()
    assert ok and sent[-1]["channel"] == "#alerts"


# ─── API integration ──────────────────────────────────────────────────────


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GENWATCH_MOCK", "true")
    monkeypatch.setenv("GENWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GENWATCH_AUTH__ADMIN_PASSWORD_HASH", hash_password("test"))
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
            r = await c.post("/api/auth/login", json={"username": "admin", "password": "test"})
            assert r.status_code == 200, r.text
            yield c, app


async def test_new_alert_options_round_trip_through_the_api(client):
    c, app = client
    patch = {
        "slack": {
            "alert_on_transfer_to_generator": True,
            "alert_on_return_to_utility": False,
            "alert_on_load_source_unknown": True,
            "channel_load_source": "#on-call",
            "mention_on_transfer_to_generator": "<!channel>",
            "mention_on_return_to_utility": "<@U1>",
            "load_source_debounce_s": 45.0,
            "alert_on_login_failure": False,
            "alert_on_login_success": True,
            "channel_security": "#sec",
        }
    }
    r = await c.put("/api/config", json=patch)
    assert r.status_code == 200, r.text
    assert r.json()["slack_updated"] is True
    # Slack-only edits never demand a restart.
    assert r.json()["restart_required"] is False

    cfg = (await c.get("/api/config")).json()["slack"]
    assert cfg["alertOnReturnToUtility"] is False
    assert cfg["channelLoadSource"] == "#on-call"
    assert cfg["mentionOnTransferToGenerator"] == "<!channel>"
    assert cfg["loadSourceDebounceS"] == 45.0
    assert cfg["alertOnLoginFailure"] is False
    assert cfg["channelSecurity"] == "#sec"

    # …and the live notifier picked them up without a restart.
    live = app.state.slack.cfg
    assert live.channel_load_source == "#on-call"
    assert live.load_source_debounce_s == 45.0
    assert live.alert_on_return_to_utility is False


async def test_slack_test_endpoint_accepts_a_route(client, monkeypatch):
    c, _app = client
    sent: list[dict] = []
    monkeypatch.setattr(
        slack_mod, "_post_slack", lambda t, p, to: (sent.append(p) or (True, "ok"))
    )
    await c.put(
        "/api/config",
        json={"slack": {"enabled": True, "bot_token": "xoxb-x", "channel": "#a",
                        "channel_security": "#sec"}},
    )
    r = await c.post("/api/slack/test?kind=security")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sent[-1]["channel"] == "#sec"

    # An unknown route is rejected rather than silently posting to #a.
    assert (await c.post("/api/slack/test?kind=bogus")).status_code == 422
