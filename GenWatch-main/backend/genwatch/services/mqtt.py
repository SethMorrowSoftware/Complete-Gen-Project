"""MQTT publishing for the generator load source.

Publishes a small retained message every time the load moves between
utility and generator:

  - load transfers to the generator → publish ``payload_on``  ("ON")
  - load returns to utility          → publish ``payload_off`` ("OFF")

to ``topic`` (default ``facility/generator/status``). Home-automation,
SCADA, or dashboard systems subscribe to that topic to know, in real
time, whether the building is running on backup power.

Design notes
------------
* **No new dependency.** MQTT is a compact binary protocol; rather than
  pull in a broker client library we speak MQTT 3.1.1 (protocol level 4)
  directly over a socket — the same philosophy the Slack notifier uses
  with ``urllib`` instead of ``requests``. This keeps the hash-pinned,
  air-gap-friendly install (``requirements.lock``) unchanged.
* **Connect-publish-disconnect per message.** Load-source transitions are
  rare (a handful a month), so a fresh short-lived connection per event
  is simpler and more robust than babysitting a long-lived connection
  with keep-alive pings and reconnect state. Each publish opens a TCP
  (optionally TLS) connection, CONNECTs, PUBLISHes, and DISCONNECTs.
* **Retained + QoS 1 by default.** Retain means a subscriber that
  connects *after* a transition still learns the current state
  immediately. QoS 1 gives a broker PUBACK so a transient drop is
  retried rather than silently lost.
* **Best-effort, never blocking.** Messages are placed on an
  ``asyncio.Queue`` drained by a worker task; the blocking socket work
  runs in ``asyncio.to_thread``. A slow or unreachable broker can never
  stall the Modbus poller, the API event loop, or Slack.
* **Passwords are sensitive:** GET /api/config returns only
  ``passwordConfigured: bool``, never the literal password.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass

from ..config import MqttConfig
from ..db import Database

log = logging.getLogger("genwatch.mqtt")


# ─── MQTT 3.1.1 wire encoding ────────────────────────────────────────────

_CONNACK_RETURN = {
    0: "accepted",
    1: "unacceptable protocol version",
    2: "identifier rejected",
    3: "server unavailable",
    4: "bad username or password",
    5: "not authorized",
}


def _encode_remaining_length(n: int) -> bytes:
    """MQTT variable-length 'Remaining Length' field (1–4 bytes)."""
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n > 0:
            byte |= 0x80
        out.append(byte)
        if n <= 0:
            break
    return bytes(out)


def _encode_string(s: str) -> bytes:
    """A UTF-8 MQTT string: 2-byte big-endian length prefix + bytes."""
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def _build_connect(client_id: str, username: str, password: str, keepalive: int) -> bytes:
    """CONNECT control packet. Clean session, optional username/password."""
    flags = 0x02  # clean session
    payload = _encode_string(client_id)
    # Per §3.1.2.9 a password may only be present with a username.
    if username:
        flags |= 0x80
        payload += _encode_string(username)
        if password:
            flags |= 0x40
            payload += _encode_string(password)
    var_header = _encode_string("MQTT") + bytes([0x04, flags]) + keepalive.to_bytes(2, "big")
    remaining = var_header + payload
    return bytes([0x10]) + _encode_remaining_length(len(remaining)) + remaining


def _build_publish(topic: str, payload: str, qos: int, retain: bool, packet_id: int) -> bytes:
    """PUBLISH control packet."""
    header = 0x30 | ((qos & 0x03) << 1) | (0x01 if retain else 0x00)
    var_header = _encode_string(topic)
    if qos > 0:
        var_header += packet_id.to_bytes(2, "big")
    body = payload.encode("utf-8")
    remaining = var_header + body
    return bytes([header]) + _encode_remaining_length(len(remaining)) + remaining


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise ConnectionError on early EOF."""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise ConnectionError("broker closed the connection")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _read_packet(sock: socket.socket) -> tuple[int, bytes]:
    """Read one MQTT control packet → (first_byte, body_bytes)."""
    first = _recv_exact(sock, 1)[0]
    mult = 1
    length = 0
    while True:
        enc = _recv_exact(sock, 1)[0]
        length += (enc & 0x7F) * mult
        if (enc & 0x80) == 0:
            break
        mult *= 128
        if mult > 128 ** 3:
            raise ValueError("malformed Remaining Length")
    body = _recv_exact(sock, length) if length else b""
    return first, body


def _mqtt_publish(
    *,
    host: str,
    port: int,
    topic: str,
    payload: str,
    qos: int,
    retain: bool,
    username: str,
    password: str,
    client_id: str,
    keepalive: int,
    timeout_s: float,
    tls: bool,
    tls_insecure: bool,
) -> tuple[bool, str]:
    """Synchronous connect-publish-disconnect. Run via ``asyncio.to_thread``.

    Returns ``(ok, detail)``. On failure ``detail`` starts with
    ``connect_refused`` for a broker-side rejection (auth/identifier — a
    config problem not worth retrying) and with a transport tag otherwise.
    """
    try:
        raw = socket.create_connection((host, port), timeout=timeout_s)
    except OSError as e:
        return False, f"connect_failed {e}"

    sock: socket.socket = raw
    try:
        if tls:
            ctx = ssl.create_default_context()
            if tls_insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=host)
        sock.settimeout(timeout_s)

        # CONNECT → CONNACK
        sock.sendall(_build_connect(client_id, username, password, keepalive))
        ptype, body = _read_packet(sock)
        if (ptype & 0xF0) != 0x20:
            return False, f"protocol_error expected CONNACK got 0x{ptype:02x}"
        if len(body) < 2:
            return False, "protocol_error malformed CONNACK"
        rc = body[1]
        if rc != 0:
            return False, f"connect_refused {rc} ({_CONNACK_RETURN.get(rc, 'unknown')})"

        # PUBLISH (→ PUBACK for QoS 1)
        packet_id = 1
        sock.sendall(_build_publish(topic, payload, qos, retain, packet_id))
        if qos == 1:
            ptype, body = _read_packet(sock)
            if (ptype & 0xF0) != 0x40:
                return False, f"protocol_error expected PUBACK got 0x{ptype:02x}"
            if len(body) < 2 or int.from_bytes(body[:2], "big") != packet_id:
                return False, "protocol_error PUBACK id mismatch"

        # DISCONNECT — best-effort; the publish already succeeded.
        try:
            sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        return True, "ok"
    except (OSError, ssl.SSLError, ValueError, ConnectionError) as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ─── Async publisher ─────────────────────────────────────────────────────


@dataclass
class _PendingPublish:
    topic: str
    payload: str
    qos: int
    retain: bool
    label: str
    attempt: int = 0
    enqueued_at: float = 0.0


class MqttPublisher:
    """Async-queued, best-effort MQTT publisher for load-source status.

    Mirrors ``services/slack.SlackNotifier``: an in-process queue drained
    by a worker task, exponential-backoff retries on transport errors,
    and a per-message wall-clock deadline so a broker outage can't pile
    up stale retries.
    """

    MAX_QUEUE = 100
    MAX_ATTEMPTS = 4
    BACKOFF_S = (1.0, 4.0, 16.0)
    # CONNECT keep-alive advertised to the broker. The connection is
    # short-lived (publish then disconnect), so this only bounds how long
    # a hung broker mid-handshake is tolerated alongside the socket timeout.
    KEEPALIVE_S = 30
    # Per-message wall-clock deadline. Past this the worker drops the
    # message regardless of remaining attempts — a sustained broker outage
    # shouldn't keep re-queuing a now-stale status.
    MAX_AGE_S = 300.0

    def __init__(self, cfg: MqttConfig, db: Database, *, site_name: str = ""):
        self.cfg = cfg
        self.db = db
        self.site_name = site_name
        self._queue: asyncio.Queue[_PendingPublish] = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._worker_task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        self._running = False
        # Last status we intended to publish ("on"/"off"). Dedupes the two
        # signal paths (ATS-Pi position + H-100 telemetry) that can both
        # report the same transition, avoiding a redundant retained republish.
        self._last_state: str | None = None

    # ---- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="mqtt-worker")
        log.info(
            "MQTT publisher started (enabled=%s, broker=%s:%d, topic=%s, tls=%s)",
            self.cfg.enabled, self.cfg.host, self.cfg.port, self.cfg.topic, self.cfg.tls,
        )

    async def stop(self) -> None:
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker_task = None
        for t in list(self._retry_tasks):
            t.cancel()
        for t in list(self._retry_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._retry_tasks.clear()

    def update_config(self, cfg: MqttConfig) -> None:
        """Hot-reload the config — picked up on the next publish."""
        self.cfg = cfg
        log.info(
            "MQTT config updated (enabled=%s, broker=%s:%d, topic=%s)",
            cfg.enabled, cfg.host, cfg.port, cfg.topic,
        )

    def is_enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.host and self.cfg.topic)

    def _client_id(self) -> str:
        """Configured client id, or a stable one derived from the site name.

        Capped to the 23-char / alphanumeric identifier every MQTT 3.1.1
        broker must accept (§3.1.3.1)."""
        if self.cfg.client_id:
            return self.cfg.client_id
        slug = "".join(c for c in (self.site_name or "") if c.isalnum())
        return ("genwatch" + slug)[:23]

    # ---- public API ---------------------------------------------------
    async def publish_generator_status(self, on: bool, ts: float | None = None) -> None:
        """Publish the generator's load status.

        ``on=True``  → the generator is now carrying the load  (payload_on).
        ``on=False`` → the load has returned to utility          (payload_off).

        Consecutive identical states are deduped: both the ATS-Pi position
        path and the H-100 telemetry path can announce the same transition,
        and re-publishing an identical retained value would be redundant.
        """
        if not self.is_enabled():
            return
        state_key = "on" if on else "off"
        if self._last_state == state_key:
            log.debug("mqtt: generator status already %s — skipping republish", state_key)
            return
        self._last_state = state_key
        payload = self.cfg.payload_on if on else self.cfg.payload_off
        await self._enqueue(
            topic=self.cfg.topic,
            payload=payload,
            qos=self.cfg.qos,
            retain=self.cfg.retain,
            label=f"generator/{state_key}",
        )

    async def test(self) -> tuple[bool, str]:
        """Synchronous one-shot publish for the Settings 'Test' button.

        Publishes a distinct, NON-retained payload so it doesn't clobber
        the real retained status on the broker. Returns (ok, detail)."""
        if not self.cfg.host:
            return False, "host not set"
        if not self.cfg.topic:
            return False, "topic not set"
        cfg = self.cfg
        ok, detail = await asyncio.to_thread(
            _mqtt_publish,
            host=cfg.host, port=cfg.port, topic=cfg.topic,
            payload=f"{cfg.payload_off} (genwatch test)",
            qos=cfg.qos, retain=False,
            username=cfg.username, password=cfg.password,
            client_id=self._client_id(), keepalive=self.KEEPALIVE_S,
            timeout_s=cfg.timeout_s, tls=cfg.tls, tls_insecure=cfg.tls_insecure,
        )
        return ok, "ok" if ok else detail

    # ---- internals ----------------------------------------------------
    async def _enqueue(self, *, topic: str, payload: str, qos: int, retain: bool, label: str) -> None:
        if not self.is_enabled():
            return
        msg = _PendingPublish(
            topic=topic, payload=payload, qos=qos, retain=retain,
            label=label, enqueued_at=time.time(),
        )
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Prefer the freshest status. Evict the oldest queued publish
            # to make room — under a broker outage the queue fills with
            # stale retries and the newest transition is the one that matters.
            try:
                dropped = self._queue.get_nowait()
                log.warning("MQTT queue full — evicted oldest (%s) to enqueue %s",
                            dropped.label, label)
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("MQTT queue full — dropping publish: %s", label)

    async def _worker(self) -> None:
        while self._running:
            try:
                msg = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                ok, detail = await self._send(msg)
                if ok:
                    log.info("MQTT published %s=%r to %s", msg.label, msg.payload, msg.topic)
                    continue
                # Broker-side refusal (auth/identifier) is a config problem;
                # retrying won't fix it. Everything else is transport — retry.
                if detail.startswith("connect_refused"):
                    log.error("MQTT broker refused publish: %s — %s", detail, msg.label)
                    continue
                msg.attempt += 1
                if msg.attempt >= self.MAX_ATTEMPTS:
                    log.error("MQTT publish failed after %d attempts: %s — %s",
                              msg.attempt, detail, msg.label)
                    continue
                if msg.enqueued_at and (time.time() - msg.enqueued_at) > self.MAX_AGE_S:
                    log.warning("MQTT: abandoning publish past %ds deadline: %s",
                                int(self.MAX_AGE_S), msg.label)
                    continue
                backoff = self.BACKOFF_S[min(msg.attempt - 1, len(self.BACKOFF_S) - 1)]
                log.warning("MQTT publish failed (%s), retry %d/%d in %.1fs",
                            detail, msg.attempt, self.MAX_ATTEMPTS, backoff)
                t = asyncio.create_task(self._requeue_after(backoff, msg))
                self._retry_tasks.add(t)
                t.add_done_callback(self._retry_tasks.discard)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.exception("MQTT worker error: %s", e)

    async def _requeue_after(self, delay: float, msg: _PendingPublish) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("MQTT queue full on retry — dropping: %s", msg.label)

    async def _send(self, msg: _PendingPublish) -> tuple[bool, str]:
        cfg = self.cfg
        if not cfg.host or not cfg.topic:
            return False, "not_configured"
        return await asyncio.to_thread(
            _mqtt_publish,
            host=cfg.host, port=cfg.port, topic=msg.topic, payload=msg.payload,
            qos=msg.qos, retain=msg.retain,
            username=cfg.username, password=cfg.password,
            client_id=self._client_id(), keepalive=self.KEEPALIVE_S,
            timeout_s=cfg.timeout_s, tls=cfg.tls, tls_insecure=cfg.tls_insecure,
        )
