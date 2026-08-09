"""Tests for the MQTT publisher.

Covers:
  - MQTT 3.1.1 wire encoding (remaining-length, strings, CONNECT, PUBLISH)
  - end-to-end publish against an in-process fake broker (CONNACK/PUBACK)
  - connect-refused (config error) vs transport error handling
  - MqttPublisher enabled/disabled gating
  - generator ON/OFF publishing + consecutive-state dedupe
  - worker retries on transport error, not on connect_refused
  - test() gating + success (non-retained)
  - load-source → ON/OFF forwarding (main._forward_to_mqtt) incl. boot suppression
  - ATS position → ON/OFF forwarding (AtsService._forward_to_mqtt)
  - API: GET /config never exposes the password, PUT hot-reloads, /api/mqtt/test
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

from genwatch.config import MqttConfig
from genwatch.db import Database
from genwatch.main import _forward_to_mqtt, create_app
from genwatch.services import mqtt as mqtt_mod
from genwatch.services.auth import hash_password
from genwatch.services.mqtt import (
    MqttPublisher,
    _build_connect,
    _build_publish,
    _encode_remaining_length,
    _encode_string,
    _mqtt_publish,
    _read_packet,
)


# ─── Wire encoding ─────────────────────────────────────────────────────────


def test_remaining_length_encoding():
    assert _encode_remaining_length(0) == b"\x00"
    assert _encode_remaining_length(127) == b"\x7f"
    assert _encode_remaining_length(128) == b"\x80\x01"
    assert _encode_remaining_length(16383) == b"\xff\x7f"
    assert _encode_remaining_length(16384) == b"\x80\x80\x01"


def test_encode_string():
    assert _encode_string("MQTT") == b"\x00\x04MQTT"
    assert _encode_string("") == b"\x00\x00"


def test_build_connect_clean_session_no_auth():
    pkt = _build_connect("cid", "", "", 30)
    assert pkt[0] == 0x10  # CONNECT control type
    assert b"\x00\x04MQTT" in pkt
    idx = pkt.index(b"MQTT") + 4
    assert pkt[idx] == 0x04       # protocol level 4 (MQTT 3.1.1)
    assert pkt[idx + 1] == 0x02   # connect flags: clean session only
    assert b"\x00\x03cid" in pkt  # client id in payload


def test_build_connect_with_auth():
    pkt = _build_connect("cid", "user", "secret", 30)
    idx = pkt.index(b"MQTT") + 4
    flags = pkt[idx + 1]
    assert flags & 0x80  # username flag
    assert flags & 0x40  # password flag
    assert b"\x00\x04user" in pkt
    assert b"\x00\x06secret" in pkt


def test_build_connect_password_requires_username():
    # Password without username must NOT set the password flag (spec §3.1.2.9).
    pkt = _build_connect("cid", "", "orphan", 30)
    idx = pkt.index(b"MQTT") + 4
    assert (pkt[idx + 1] & 0x40) == 0
    assert b"orphan" not in pkt


def test_build_publish_qos1_retain():
    pkt = _build_publish("t/x", "ON", qos=1, retain=True, packet_id=1)
    assert pkt[0] == (0x30 | (1 << 1) | 1)  # 0x33
    assert b"\x00\x03t/x" in pkt
    assert pkt.endswith(b"\x00\x01ON")  # packet id (0x0001) then payload


def test_build_publish_qos0_has_no_packet_id():
    pkt = _build_publish("t", "OFF", qos=0, retain=False, packet_id=1)
    assert pkt[0] == 0x30  # qos 0, retain 0
    assert pkt.endswith(b"\x00\x01tOFF")  # topic then payload, no packet id


# ─── In-process fake broker ────────────────────────────────────────────────


class FakeBroker:
    """A one-shot MQTT broker good enough to accept a single publish."""

    def __init__(self, connack_rc: int = 0, send_puback: bool = True):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.connack_rc = connack_rc
        self.send_puback = send_puback
        self.captured: dict = {}
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeBroker":
        self.thread.start()
        time.sleep(0.05)
        return self

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(5)
        with conn:
            try:
                ptype, _body = _read_packet(conn)  # CONNECT
                self.captured["connect_type"] = ptype
                conn.sendall(bytes([0x20, 0x02, 0x00, self.connack_rc]))
                if self.connack_rc != 0:
                    return
                ptype, body = _read_packet(conn)  # PUBLISH
                tlen = int.from_bytes(body[:2], "big")
                topic = body[2 : 2 + tlen].decode()
                rest = body[2 + tlen :]
                qos = (ptype >> 1) & 0x03
                retain = ptype & 0x01
                if qos > 0:
                    pid = rest[:2]
                    payload = rest[2:].decode()
                    if self.send_puback:
                        conn.sendall(b"\x40\x02" + pid)
                else:
                    payload = rest.decode()
                self.captured.update(topic=topic, payload=payload, qos=qos, retain=bool(retain))
            except (OSError, ConnectionError, ValueError):
                pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_publish_end_to_end():
    b = FakeBroker().start()
    ok, detail = _mqtt_publish(
        host="127.0.0.1", port=b.port, topic="facility/generator/status",
        payload="ON", qos=1, retain=True, username="", password="",
        client_id="genwatch-x", keepalive=30, timeout_s=5, tls=False, tls_insecure=False,
    )
    b.thread.join(5)
    b.close()
    assert ok, detail
    assert b.captured["topic"] == "facility/generator/status"
    assert b.captured["payload"] == "ON"
    assert b.captured["qos"] == 1
    assert b.captured["retain"] is True
    assert (b.captured["connect_type"] & 0xF0) == 0x10


def test_publish_connect_refused_is_reported():
    b = FakeBroker(connack_rc=5).start()  # 5 = not authorized
    ok, detail = _mqtt_publish(
        host="127.0.0.1", port=b.port, topic="t", payload="ON", qos=1, retain=True,
        username="", password="", client_id="x", keepalive=30, timeout_s=5,
        tls=False, tls_insecure=False,
    )
    b.thread.join(5)
    b.close()
    assert ok is False
    assert detail.startswith("connect_refused")
    assert "not authorized" in detail


def test_publish_connect_failed_when_no_broker():
    # 127.0.0.1:1 refuses immediately (nothing listening / privileged port).
    ok, detail = _mqtt_publish(
        host="127.0.0.1", port=1, topic="t", payload="ON", qos=0, retain=False,
        username="", password="", client_id="x", keepalive=30, timeout_s=1,
        tls=False, tls_insecure=False,
    )
    assert ok is False
    assert detail.startswith("connect_failed")


# ─── Enabled / disabled gating ─────────────────────────────────────────────


def test_publisher_disabled_when_flag_off(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=False, host="h", topic="t"), db)
    assert p.is_enabled() is False


def test_publisher_disabled_when_missing_host(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="", topic="t"), db)
    assert p.is_enabled() is False


def test_publisher_disabled_when_missing_topic(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic=""), db)
    assert p.is_enabled() is False


def test_publisher_enabled_with_full_config(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic="t"), db)
    assert p.is_enabled() is True


def test_client_id_defaults_to_site_slug(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic="t"), db, site_name="Castle Generator!")
    cid = p._client_id()
    assert cid.startswith("genwatch")
    assert cid.isalnum()
    assert len(cid) <= 23


# ─── Worker dispatch + dedupe ──────────────────────────────────────────────


async def test_worker_publishes_on_then_off(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    cfg = MqttConfig(enabled=True, host="h", topic="facility/generator/status", retain=True, qos=1)
    p = MqttPublisher(cfg, db)

    calls: list[dict] = []

    def fake_pub(**kw):
        calls.append(kw)
        return True, "ok"

    monkeypatch.setattr(mqtt_mod, "_mqtt_publish", fake_pub)
    await p.start()
    try:
        await p.publish_generator_status(True)
        await p.publish_generator_status(False)
        for _ in range(40):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await p.stop()
    assert len(calls) == 2
    assert calls[0]["payload"] == "ON"
    assert calls[0]["topic"] == "facility/generator/status"
    assert calls[0]["retain"] is True
    assert calls[0]["qos"] == 1
    assert calls[1]["payload"] == "OFF"


async def test_worker_dedupes_consecutive_identical_states(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic="t"), db)

    calls: list[str] = []
    monkeypatch.setattr(
        mqtt_mod, "_mqtt_publish",
        lambda **kw: (calls.append(kw["payload"]) or (True, "ok")),
    )
    await p.start()
    try:
        await p.publish_generator_status(True)   # ON  → publish
        await p.publish_generator_status(True)   # ON  → deduped
        await p.publish_generator_status(True)   # ON  → deduped
        await p.publish_generator_status(False)  # OFF → publish
        await p.publish_generator_status(True)   # ON  → publish
        for _ in range(40):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await p.stop()
    assert calls == ["ON", "OFF", "ON"]


async def test_worker_retries_on_transport_error(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic="t"), db)
    p.BACKOFF_S = (0.05, 0.05, 0.05)
    p.MAX_ATTEMPTS = 3

    n = {"c": 0}

    def fake_pub(**kw):
        n["c"] += 1
        if n["c"] < 3:
            return False, "TimeoutError: timed out"
        return True, "ok"

    monkeypatch.setattr(mqtt_mod, "_mqtt_publish", fake_pub)
    await p.start()
    try:
        await p.publish_generator_status(True)
        for _ in range(60):
            if n["c"] >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await p.stop()
    assert n["c"] == 3


async def test_worker_does_not_retry_on_connect_refused(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic="t"), db)
    p.BACKOFF_S = (0.02, 0.02, 0.02)

    attempts = {"c": 0}

    def fake_pub(**kw):
        attempts["c"] += 1
        return False, "connect_refused 4 (bad username or password)"

    monkeypatch.setattr(mqtt_mod, "_mqtt_publish", fake_pub)
    await p.start()
    try:
        await p.publish_generator_status(True)
        await asyncio.sleep(0.3)
    finally:
        await p.stop()
    assert attempts["c"] == 1, "connect_refused is a config error — must not retry"


# ─── test() endpoint helper ────────────────────────────────────────────────


async def test_test_returns_false_when_missing_host(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="", topic="t"), db)
    ok, msg = await p.test()
    assert ok is False
    assert "host" in msg


async def test_test_returns_false_when_missing_topic(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic=""), db)
    ok, msg = await p.test()
    assert ok is False
    assert "topic" in msg


async def test_test_publishes_non_retained(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite")
    p = MqttPublisher(MqttConfig(enabled=True, host="h", topic="t", retain=True), db)
    captured: dict = {}

    def fake_pub(**kw):
        captured.update(kw)
        return True, "ok"

    monkeypatch.setattr(mqtt_mod, "_mqtt_publish", fake_pub)
    ok, msg = await p.test()
    assert ok is True
    assert msg == "ok"
    # A test must never clobber the real retained status on the broker.
    assert captured["retain"] is False


# ─── load-source → ON/OFF forwarding (main._forward_to_mqtt) ───────────────


class _SpyPublisher:
    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self.calls: list[bool] = []
        self.cfg = MqttConfig(enabled=enabled, host="h", topic="t")

    def is_enabled(self) -> bool:
        return self._enabled

    async def publish_generator_status(self, on: bool, ts=None) -> None:
        self.calls.append(on)


async def test_forward_load_source_to_generator_publishes_on():
    spy = _SpyPublisher()
    await _forward_to_mqtt(spy, {"type": "load-source", "from": "utility", "to": "generator", "ts": 1})
    assert spy.calls == [True]


async def test_forward_load_source_to_utility_publishes_off():
    spy = _SpyPublisher()
    await _forward_to_mqtt(spy, {"type": "load-source", "from": "generator", "to": "utility", "ts": 1})
    assert spy.calls == [False]


async def test_forward_boot_unknown_to_utility_suppressed():
    spy = _SpyPublisher()
    await _forward_to_mqtt(spy, {"type": "load-source", "from": "unknown", "to": "utility", "ts": 1})
    assert spy.calls == []


async def test_forward_boot_unknown_to_generator_publishes_on():
    spy = _SpyPublisher()
    await _forward_to_mqtt(spy, {"type": "load-source", "from": "unknown", "to": "generator", "ts": 1})
    assert spy.calls == [True]


async def test_forward_ignores_non_load_source_events():
    spy = _SpyPublisher()
    await _forward_to_mqtt(spy, {"type": "transition", "from": "stopped", "to": "running", "ts": 1})
    await _forward_to_mqtt(spy, {"type": "alarm", "code": "X", "ts": 1})
    assert spy.calls == []


async def test_forward_noop_when_disabled():
    spy = _SpyPublisher(enabled=False)
    await _forward_to_mqtt(spy, {"type": "load-source", "from": "utility", "to": "generator", "ts": 1})
    assert spy.calls == []


# ─── ATS position → ON/OFF forwarding (AtsService._forward_to_mqtt) ─────────


def _ats_service(db, mqtt):
    from genwatch.modbus.registers import load_register_map
    from genwatch.services.ats import AtsService
    from genwatch.services.state import EventBus

    regmap = load_register_map(
        Path(__file__).parent.parent / "genwatch" / "registers" / "ats_pi.yaml"
    )
    return AtsService(regmap, db, EventBus(), slack=None, mqtt=mqtt)


async def test_ats_forward_position_to_generator_publishes_on(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    spy = _SpyPublisher()
    svc = _ats_service(db, spy)
    await svc._forward_to_mqtt({"type": "ats-position", "from": "utility", "to": "generator", "ts": 1})
    assert spy.calls == [True]


async def test_ats_forward_position_to_utility_publishes_off(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    spy = _SpyPublisher()
    svc = _ats_service(db, spy)
    await svc._forward_to_mqtt({"type": "ats-position", "from": "generator", "to": "utility", "ts": 1})
    assert spy.calls == [False]


async def test_ats_forward_transferring_ignored(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    spy = _SpyPublisher()
    svc = _ats_service(db, spy)
    await svc._forward_to_mqtt({"type": "ats-position", "from": "utility", "to": "transferring", "ts": 1})
    assert spy.calls == []


# ─── API integration ───────────────────────────────────────────────────────


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
        transport=transport, base_url="http://test",
        headers={"X-Requested-With": "pytest"},
    ) as c:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)
            yield c, app


async def _login(c: httpx.AsyncClient) -> None:
    r = await c.post("/api/auth/login", json={"username": "admin", "password": "test"})
    assert r.status_code == 200, r.text


async def test_get_config_exposes_mqtt_without_password(client):
    c, _app = client
    await _login(c)
    r = await c.put("/api/config", json={"mqtt": {"password": "s3cret-broker-pw"}})
    assert r.status_code == 200, r.text
    r = await c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    blob = json.dumps(body)
    assert "s3cret-broker-pw" not in blob
    assert body["mqtt"]["passwordConfigured"] is True
    assert body["mqtt"]["topic"] == "facility/generator/status"


async def test_put_mqtt_hot_reloads_publisher(client, app_env):
    c, app = client
    await _login(c)
    r = await c.put(
        "/api/config",
        json={"mqtt": {"enabled": True, "host": "broker.lan", "port": 1884, "topic": "x/y"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mqtt_updated"] is True
    assert body["restart_required"] is False

    pub = app.state.mqtt
    assert pub.cfg.enabled is True
    assert pub.cfg.host == "broker.lan"
    assert pub.cfg.port == 1884
    assert pub.cfg.topic == "x/y"

    on_disk = yaml.safe_load(app_env.read_text())
    assert on_disk["mqtt"]["host"] == "broker.lan"
    assert on_disk["mqtt"]["topic"] == "x/y"


async def test_put_mqtt_and_slack_together(client, app_env):
    """A single PUT touching both mqtt and slack must apply both."""
    c, app = client
    await _login(c)
    r = await c.put(
        "/api/config",
        json={
            "slack": {"enabled": True, "channel": "#x", "bot_token": "xoxb-z"},
            "mqtt": {"enabled": True, "host": "h", "topic": "t"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slack_updated"] is True
    assert body["mqtt_updated"] is True
    assert app.state.slack.cfg.enabled is True
    assert app.state.mqtt.cfg.enabled is True
    assert app.state.mqtt.cfg.host == "h"


async def test_mqtt_test_endpoint_reports_detail(client, monkeypatch):
    c, _app = client
    await _login(c)
    await c.put("/api/config", json={"mqtt": {"enabled": True, "host": "h", "topic": "t"}})
    monkeypatch.setattr(
        mqtt_mod, "_mqtt_publish",
        lambda **kw: (False, "connect_failed [Errno 111] Connection refused"),
    )
    r = await c.post("/api/mqtt/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "connect_failed" in body["detail"]


async def test_mqtt_test_requires_admin(client):
    c, _app = client
    r = await c.post("/api/mqtt/test")
    assert r.status_code == 401


async def test_mqtt_password_does_not_leak_to_audit_log(client, app_env):
    c, app = client
    await _login(c)
    r = await c.put("/api/config", json={"mqtt": {"password": "broker-pw-98765"}})
    assert r.status_code == 200, r.text
    with app.state.db._writer() as cur:
        rows = cur.execute(
            "SELECT action, detail FROM audit WHERE action='config.update' ORDER BY id DESC LIMIT 5"
        ).fetchall()
    assert rows, "no audit row written"
    for row in rows:
        assert "broker-pw-98765" not in (row["detail"] or "")
