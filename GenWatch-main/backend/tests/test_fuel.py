"""Fuel-level monitoring and its Slack alerts.

The behaviours worth pinning are the ones that decide whether an operator
trusts the alert: it must fire while the engine is STOPPED (when there's
still time to act), it must not flap on a sloshing sender, it must keep
reminding while the tank is still low, and it must stay silent when the
register is reading nonsense rather than paging "FUEL EMPTY" at 3 a.m.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from genwatch.config import FuelConfig, SlackConfig
from genwatch.db import Database
from genwatch.main import create_app
from genwatch.services import slack as slack_mod
from genwatch.services.auth import hash_password
from genwatch.services.fuel import FuelMonitor
from genwatch.services.slack import SlackNotifier

HOUR = 3600.0


def _mon(**cfg) -> FuelMonitor:
    base = dict(enabled=True, warn_pct=25.0, critical_pct=10.0, hysteresis_pct=3.0,
                renotify_hours=12.0)
    base.update(cfg)
    return FuelMonitor(cfg=FuelConfig(**base), tank_gal=680, fuel_type="diesel")


def _kinds(events) -> list[tuple[str, str]]:
    return [(e.kind, e.status) for e in events]


# ─── Thresholds ───────────────────────────────────────────────────────────


def test_first_reading_above_threshold_says_nothing():
    """Starting up with a full tank is not an event."""
    m = _mon()
    assert m.update(pct=80.0, engine_state="stopped", now=0) == []
    assert m.status == "ok"


def test_crossing_warn_then_critical_fires_once_each():
    m = _mon()
    m.update(pct=80.0, engine_state="stopped", now=0)
    assert _kinds(m.update(pct=24.0, engine_state="stopped", now=60)) == [("level", "warn")]
    # Still in the warn band → no repeat (the reminder is time-based).
    assert m.update(pct=20.0, engine_state="stopped", now=120) == []
    assert _kinds(m.update(pct=9.0, engine_state="stopped", now=180)) == [("level", "critical")]
    assert m.update(pct=8.0, engine_state="stopped", now=240) == []


def test_alert_fires_while_the_engine_is_stopped():
    """The whole reason this isn't the numeric-range mechanism: those
    bands only evaluate while the engine is running, and a low tank
    matters most before the outage starts."""
    m = _mon()
    m.update(pct=80.0, engine_state="stopped", now=0)
    events = m.update(pct=8.0, engine_state="stopped", now=60)
    assert [e.status for e in events] == ["critical"]
    assert events[0].severity == "alarm"


def test_message_reports_gallons_when_the_tank_size_is_known():
    m = _mon()
    m.update(pct=80.0, engine_state="stopped", now=0)
    msg = m.update(pct=9.0, engine_state="stopped", now=60)[0].message
    assert "9%" in msg
    assert "61 gal" in msg  # 9% of a 680 gal tank — what you tell the supplier

    unknown_tank = FuelMonitor(cfg=FuelConfig(enabled=True), tank_gal=0, fuel_type="diesel")
    unknown_tank.update(pct=80.0, engine_state="stopped", now=0)
    assert "gal" not in unknown_tank.update(pct=5.0, engine_state="stopped", now=60)[0].message


# ─── Hysteresis ───────────────────────────────────────────────────────────


def test_sloshing_around_the_threshold_does_not_flap():
    """A sender oscillating on the line warns once, not on every poll."""
    m = _mon(hysteresis_pct=3.0)
    m.update(pct=30.0, engine_state="running", now=0)
    assert _kinds(m.update(pct=24.0, engine_state="running", now=15)) == [("level", "warn")]
    silent = []
    for i, pct in enumerate([26.0, 24.5, 27.0, 23.0, 26.5, 24.0]):
        silent += m.update(pct=pct, engine_state="running", now=30 + i * 15)
    assert silent == [], "readings inside the hysteresis band must stay quiet"
    assert m.status == "warn"
    # Only a genuine refill (past warn + hysteresis) clears it.
    assert _kinds(m.update(pct=28.5, engine_state="running", now=200)) == [("recovered", "ok")]


def test_recovery_from_critical_to_warn_is_reported_as_improvement():
    m = _mon()
    m.update(pct=30.0, engine_state="stopped", now=0)
    m.update(pct=8.0, engine_state="stopped", now=60)
    events = m.update(pct=15.0, engine_state="stopped", now=120)
    assert _kinds(events) == [("recovered", "warn")]
    assert events[0].severity == "ok"
    assert "rising" in events[0].message.lower()


def test_refuelling_reports_recovery():
    m = _mon()
    m.update(pct=30.0, engine_state="stopped", now=0)
    m.update(pct=8.0, engine_state="stopped", now=60)
    events = m.update(pct=95.0, engine_state="stopped", now=120)
    assert _kinds(events) == [("recovered", "ok")]
    assert "replenished" in events[0].message.lower()


# ─── Reminders ────────────────────────────────────────────────────────────


def test_reminder_repeats_while_still_low():
    """Low fuel is a condition somebody must act on — silence after the
    first message reads as 'handled'."""
    m = _mon(renotify_hours=12.0)
    m.update(pct=30.0, engine_state="stopped", now=0)
    m.update(pct=8.0, engine_state="stopped", now=60)
    assert m.update(pct=8.0, engine_state="stopped", now=60 + 6 * HOUR) == []
    later = m.update(pct=8.0, engine_state="stopped", now=60 + 13 * HOUR)
    assert _kinds(later) == [("reminder", "critical")]
    assert "still" in later[0].message.lower()
    # …and the timer restarts, rather than reminding every poll thereafter.
    assert m.update(pct=8.0, engine_state="stopped", now=60 + 14 * HOUR) == []


def test_reminder_can_be_disabled():
    m = _mon(renotify_hours=0)
    m.update(pct=30.0, engine_state="stopped", now=0)
    m.update(pct=8.0, engine_state="stopped", now=60)
    assert m.update(pct=8.0, engine_state="stopped", now=60 + 100 * HOUR) == []


# ─── Sensor trust ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, -1.0, 101.0, 65535.0])
def test_implausible_readings_are_treated_as_a_sensor_fault(bad):
    """fuel_level_pct isn't verified on every H-100 revision and an
    unconfigured channel reads 0xFFFF. A false 'FUEL EMPTY' page is how a
    site learns to ignore its alerts."""
    m = _mon()
    m.update(pct=80.0, engine_state="stopped", now=0)
    assert m.update(pct=bad, engine_state="stopped", now=60) == []
    assert m.status == "ok", "a bad reading must not move the status"


def test_a_stale_register_does_not_look_like_an_empty_tank():
    m = _mon()
    m.update(pct=40.0, engine_state="stopped", now=0)
    # Poller evicted the value (fan-out failing) → values.get returns None.
    assert m.update(pct=None, engine_state="stopped", now=60) == []
    # It comes back, genuinely low → now we speak.
    assert _kinds(m.update(pct=8.0, engine_state="stopped", now=120)) == [("level", "critical")]


def test_disabled_and_gaseous_sites_never_alert():
    off = _mon(enabled=False)
    assert off.update(pct=1.0, engine_state="stopped", now=0) == []

    # A natural-gas site is fed by the utility — it has no tank to empty.
    gas = FuelMonitor(cfg=FuelConfig(enabled=True), tank_gal=0, fuel_type="gaseous")
    assert gas.is_applicable() is False
    assert gas.update(pct=1.0, engine_state="stopped", now=0) == []


# ─── Abnormal drop (leak / theft) ─────────────────────────────────────────


def test_drop_detection_is_off_unless_configured():
    m = _mon()
    m.update(pct=90.0, engine_state="stopped", now=0)
    assert m.update(pct=50.0, engine_state="stopped", now=600) == []


def test_drop_while_stopped_is_reported():
    m = _mon(drop_alert_pct=10.0, drop_window_minutes=60)
    m.update(pct=90.0, engine_state="stopped", now=0)
    events = m.update(pct=70.0, engine_state="stopped", now=1800)
    drops = [e for e in events if e.kind == "drop"]
    assert len(drops) == 1
    assert drops[0].dropped_pct == 20.0
    assert drops[0].severity == "alarm"
    assert "leak or theft" in drops[0].message


def test_burning_fuel_is_not_losing_fuel():
    """A loaded generator drawing the tank down is expected; alerting on
    it would fire on every outage."""
    m = _mon(drop_alert_pct=10.0, drop_window_minutes=60)
    m.update(pct=90.0, engine_state="stopped", now=0)
    assert [e for e in m.update(pct=70.0, engine_state="running", now=1800) if e.kind == "drop"] == []
    # Sites that want the running case can opt in.
    m2 = _mon(drop_alert_pct=10.0, drop_window_minutes=60, drop_only_when_stopped=False)
    m2.update(pct=90.0, engine_state="running", now=0)
    assert [e for e in m2.update(pct=70.0, engine_state="running", now=1800) if e.kind == "drop"]


def test_drop_alert_does_not_repeat_every_poll():
    """A sustained leak keeps the peak inside the window; without a
    cooldown it would re-fire on every reading."""
    m = _mon(drop_alert_pct=10.0, drop_window_minutes=60)
    m.update(pct=90.0, engine_state="stopped", now=0)
    first = [e for e in m.update(pct=70.0, engine_state="stopped", now=600) if e.kind == "drop"]
    assert len(first) == 1
    repeats = []
    for i in range(1, 6):
        repeats += [e for e in m.update(pct=70.0 - i, engine_state="stopped", now=600 + i * 60)
                    if e.kind == "drop"]
    assert repeats == []

    # Keep sampling the way the poller does (the level holding steady), so
    # the window stays populated, then leak again once the cooldown has
    # expired — that second event must be reported.
    now = 900.0
    quiet = []
    while now < 600 + 2 * 3600:
        quiet += [e for e in m.update(pct=65.0, engine_state="stopped", now=now) if e.kind == "drop"]
        now += 60
    assert quiet == [], "a steady level must not re-trigger the drop alert"
    again = [e for e in m.update(pct=50.0, engine_state="stopped", now=now) if e.kind == "drop"]
    assert len(again) == 1
    assert again[0].dropped_pct == 15.0


def test_a_normal_exercise_run_does_not_look_like_theft():
    """Regression: the drop check compares against the highest level in
    the window, so a run that legitimately burns fuel used to leave the
    pre-run level sitting in that window and fire 'dropped 12% while
    stopped' the moment the engine stopped. Burning must reset the
    baseline, not merely skip the check."""
    m = _mon(drop_alert_pct=10.0, drop_window_minutes=60)
    now, pct = 0.0, 90.0
    for _ in range(40):                       # 10 min idle at 90%
        m.update(pct=pct, engine_state="stopped", now=now)
        now += 15
    for _ in range(120):                      # 30 min exercise, burns 12%
        pct -= 0.1
        m.update(pct=pct, engine_state="running", now=now)
        now += 15
    fired = []
    for _ in range(20):                       # engine stops, polling continues
        fired += [e for e in m.update(pct=pct, engine_state="stopped", now=now) if e.kind == "drop"]
        now += 15
    assert fired == [], "a normal run must not read as a leak once the engine stops"

    # A genuine loss after the run still alerts.
    leak = []
    for step in range(15):
        leak += [e for e in m.update(pct=pct - step, engine_state="stopped", now=now)
                 if e.kind == "drop"]
        now += 15
    assert len(leak) == 1


def test_drop_alert_needs_two_samples_in_the_window():
    """After a comms outage there is nothing to compare against. We can't
    tell a leak from 'we weren't looking', so we say nothing."""
    m = _mon(drop_alert_pct=10.0, drop_window_minutes=60)
    m.update(pct=90.0, engine_state="stopped", now=0)
    # Two hours of no polls (Modbus link down), then one reading far lower.
    assert [e for e in m.update(pct=40.0, engine_state="stopped", now=2 * 3600 + 60)
            if e.kind == "drop"] == []


def test_slow_normal_consumption_does_not_trip_the_drop_alert():
    m = _mon(drop_alert_pct=15.0, drop_window_minutes=60)
    now = 0.0
    pct = 90.0
    fired = []
    for _ in range(48):  # 8 hours at 10-minute samples, ~1%/hour
        fired += [e for e in m.update(pct=pct, engine_state="stopped", now=now) if e.kind == "drop"]
        now += 600
        pct -= 1.0 / 6
    assert fired == []


# ─── Slack routing ────────────────────────────────────────────────────────


def _notifier(tmp_path, **cfg):
    db = Database(tmp_path / "t.sqlite")
    return SlackNotifier(
        SlackConfig(enabled=True, channel="#alerts", bot_token="xoxb-test", **cfg),
        db, site_name="Test Site",
    )


async def _drain(sink, expect=1, timeout_s=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while len(sink) < expect and loop.time() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


async def test_fuel_alert_routing_and_mention(tmp_path, monkeypatch):
    n = _notifier(tmp_path, channel_fuel="#fuel", mention_on_fuel_critical="<!channel>")
    posted: list[dict] = []
    monkeypatch.setattr(slack_mod, "_post_slack",
                        lambda t, p, to: (posted.append(p) or (True, "ok")))
    await n.start()
    try:
        await n.alert_fuel(kind="level", status="warn", pct=24.0, gallons=163)
        await _drain(posted)
        assert posted[0]["channel"] == "#fuel"
        # A "order fuel this week" warning must not ping @channel at 2 a.m.
        assert "<!channel>" not in posted[0]["text"]
        assert "163 gal" in str(posted[0]["blocks"])

        await n.alert_fuel(kind="level", status="critical", pct=8.0, gallons=54)
        await _drain(posted, expect=2)
        assert posted[1]["text"].startswith("<!channel>") or \
            posted[1]["blocks"][0]["text"]["text"].startswith("*<!channel>")
    finally:
        await n.stop()


async def test_fuel_alert_flags_gate_each_kind(tmp_path, monkeypatch):
    n = _notifier(tmp_path, alert_on_fuel_warning=False, alert_on_fuel_reminder=False,
                  alert_on_fuel_recovered=False, alert_on_fuel_drop=False)
    posted: list[dict] = []
    monkeypatch.setattr(slack_mod, "_post_slack",
                        lambda t, p, to: (posted.append(p) or (True, "ok")))
    await n.start()
    try:
        await n.alert_fuel(kind="level", status="warn", pct=24.0)
        await n.alert_fuel(kind="reminder", status="critical", pct=8.0)
        await n.alert_fuel(kind="recovered", status="ok", pct=90.0)
        await n.alert_fuel(kind="drop", status="ok", pct=70.0, dropped_pct=20, window_minutes=60)
        await asyncio.sleep(0.15)
        assert posted == []
        # Critical is still on — the one that survives everything else off.
        await n.alert_fuel(kind="level", status="critical", pct=8.0)
        await _drain(posted)
    finally:
        await n.stop()
    assert len(posted) == 1
    assert "CRITICAL" in posted[0]["blocks"][0]["text"]["text"]


async def test_fuel_test_route(tmp_path, monkeypatch):
    n = _notifier(tmp_path, channel_fuel="#fuel", mention_on_fuel_critical="<!here>")
    sent: list[dict] = []
    monkeypatch.setattr(slack_mod, "_post_slack",
                        lambda t, p, to: (sent.append(p) or (True, "ok")))
    ok, _ = await n.test("fuel")
    assert ok and sent[-1]["channel"] == "#fuel"
    assert "<!here>" in sent[-1]["blocks"][0]["text"]["text"]


# ─── End to end through the state machine ─────────────────────────────────


def _state_machine(tmp_path, **fuel_cfg):
    from genwatch.modbus.registers import load_register_map
    from genwatch.services.state import EventBus, StateMachine

    regmap = load_register_map("genwatch/registers/h100.yaml")
    db = Database(tmp_path / "sm.sqlite")
    base = dict(enabled=True, warn_pct=25.0, critical_pct=10.0)
    base.update(fuel_cfg)
    return StateMachine(regmap, db, EventBus(), fuel_cfg=FuelConfig(**base)), db


def test_state_machine_emits_fuel_events_and_logs_them(tmp_path):
    from genwatch.modbus.poller import Reading
    from genwatch.services.state import CommsHealth

    sm, db = _state_machine(tmp_path)
    comms = CommsHealth()

    sm.update(Reading(values={"fuel_level_pct": 80.0}), comms)
    emitted = sm.update(Reading(values={"fuel_level_pct": 8.0}), comms)

    fuel_events = [e for e in emitted if e.get("type") == "fuel"]
    assert len(fuel_events) == 1
    ev = fuel_events[0]
    assert ev["status"] == "critical"
    assert ev["pct"] == 8.0
    assert ev["gallons"] == 54       # 8% of the 680 gal tank in h100.yaml
    assert ev["severity"] == "alarm"

    # It lands in the operator-visible events log like any other event.
    rows = db.read_events(limit=10, type_="FUEL")
    assert len(rows) == 1
    assert "CRITICAL" in rows[0]["message"]
    db.close()


def test_state_machine_stays_silent_when_fuel_is_disabled(tmp_path):
    from genwatch.modbus.poller import Reading
    from genwatch.services.state import CommsHealth

    sm, db = _state_machine(tmp_path, enabled=False)
    sm.update(Reading(values={"fuel_level_pct": 80.0}), CommsHealth())
    emitted = sm.update(Reading(values={"fuel_level_pct": 2.0}), CommsHealth())
    assert [e for e in emitted if e.get("type") == "fuel"] == []
    assert db.read_events(limit=10, type_="FUEL") == []
    db.close()


# ─── API ──────────────────────────────────────────────────────────────────


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


async def test_fuel_config_round_trips_and_hot_reloads(client):
    c, app = client
    cfg = (await c.get("/api/config")).json()["fuel"]
    assert cfg["enabled"] is False            # opt-in: verify the register first
    assert cfg["tankGal"] == 680              # from the register map, not config.yaml
    assert cfg["fuelType"] == "diesel"

    r = await c.put("/api/config", json={"fuel": {
        "enabled": True, "warn_pct": 30.0, "critical_pct": 12.0,
        "renotify_hours": 6.0, "drop_alert_pct": 15.0,
    }})
    assert r.status_code == 200, r.text
    assert r.json()["fuel_updated"] is True
    # Thresholds are a live setting — no restart.
    assert r.json()["restart_required"] is False

    cfg = (await c.get("/api/config")).json()["fuel"]
    assert cfg["enabled"] is True and cfg["warn_pct"] == 30.0 and cfg["drop_alert_pct"] == 15.0
    live = app.state.state_machine.fuel
    assert live.cfg.warn_pct == 30.0
    assert live.is_applicable() is True


async def test_raising_a_threshold_does_not_re_announce_a_known_low_tank(client):
    """The monitor keeps its state across a config hot-reload — otherwise
    editing settings re-pages everyone about a tank they already know is
    low."""
    c, app = client
    await c.put("/api/config", json={"fuel": {"enabled": True}})
    mon = app.state.state_machine.fuel
    mon.update(pct=80.0, engine_state="stopped", now=0)
    assert mon.update(pct=8.0, engine_state="stopped", now=60)  # went critical
    assert mon.status == "critical"

    await c.put("/api/config", json={"fuel": {"warn_pct": 40.0}})
    assert app.state.state_machine.fuel.status == "critical"
    assert app.state.state_machine.fuel.update(pct=8.0, engine_state="stopped", now=120) == []


async def test_fuel_slack_flags_round_trip(client):
    c, app = client
    r = await c.put("/api/config", json={"slack": {
        "alert_on_fuel_warning": False,
        "channel_fuel": "#fuel-alerts",
        "mention_on_fuel_critical": "<!channel>",
    }})
    assert r.status_code == 200, r.text
    cfg = (await c.get("/api/config")).json()["slack"]
    assert cfg["alertOnFuelWarning"] is False
    assert cfg["channelFuel"] == "#fuel-alerts"
    assert app.state.slack.cfg.mention_on_fuel_critical == "<!channel>"
