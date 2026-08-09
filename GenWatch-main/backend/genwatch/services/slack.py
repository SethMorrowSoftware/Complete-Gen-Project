"""Slack alerting via the Web API (chat.postMessage).

Sends formatted notifications for alarms, alarm clears, state changes,
operator commands, Modbus comms transitions, utility ↔ generator
transfers, and sign-in / account activity. Each event type is gated by a
flag in SlackConfig so an operator can dial the chattiness to their taste
(e.g. alarms only, no per-transition spam).

The transfer alerts (``alert_load_source_change``) get more machinery
than the rest, because they're the ones people actually wait for:

* per-direction gating — "the power went out" and "the power is back" go
  to different audiences, and plenty of sites want only one of them;
* a channel override, so an outage can page an on-call channel while
  routine alarms stay somewhere quieter;
* a mention prefix (``<!channel>``, ``<@U…>``, ``<!subteam^S…>``) placed
  inside the message body, which is the only place Slack reads it from
  when deciding whether to actually notify anyone;
* a settle-time debounce — an ATS that hunts, or a utility browning out
  repeatedly, otherwise produces a burst of contradictory pages during
  the exact minutes an operator needs one clear signal. A flap that
  lands back on the previously-announced source sends nothing at all.

Design notes
------------
* Authenticates with a Slack **bot token** (``xoxb-...``). User tokens
  (``xoxp-...``) are not supported — they have broader scopes than this
  service needs.
* Messages are placed on an in-process asyncio.Queue and drained by a
  worker task. A slow or unreachable Slack will never block the Modbus
  poller or the API event loop.
* Sends with ``urllib.request`` in ``asyncio.to_thread`` so we don't pull
  in an HTTP library just for this. The Slack endpoint is one POST.
* Retries with exponential backoff (up to 4 attempts) on transport
  errors. If Slack returns ``{"ok": false, "error": "..."}`` we don't
  retry — the error is a config/auth issue that retrying won't fix.
* Bot tokens are sensitive: GET /api/config returns only
  ``botTokenConfigured: bool``, never the literal token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import SlackConfig
from ..db import Database

log = logging.getLogger("genwatch.slack")

SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"


@dataclass
class _PendingMessage:
    blocks: list[dict]
    fallback: str
    attempt: int = 0
    # Wall-clock instant when the message was first enqueued. Used by
    # the worker to abandon retries past MAX_AGE_S so a sustained Slack
    # outage can't pile up retry tasks chewing through SEND_TIMEOUT_S
    # each, blocking newer (more urgent) alerts behind them.
    enqueued_at: float = 0.0
    # Per-message channel override (transfer alerts and security alerts
    # can be routed to their own channels). Empty → cfg.channel.
    channel: str = ""


class SlackNotifier:
    """Async-queued Slack notifier."""

    MAX_QUEUE = 200
    MAX_ATTEMPTS = 4
    BACKOFF_S = (1.0, 4.0, 16.0)
    SEND_TIMEOUT_S = 10.0
    # Per-message wall-clock deadline. Past this, the worker drops the
    # message regardless of how many attempts remain. 5 minutes is
    # longer than any transient network blip but short enough that a
    # real Slack outage doesn't leave stale messages queued for hours.
    MAX_AGE_S = 300.0
    # Per-(code, kind) dedupe window. An alarm bit that flaps between
    # set and clear within this window only fires Slack ONCE per
    # transition direction — without it, a chattery alarm could
    # exhaust the 200-slot queue and push real alerts off the floor.
    # State-change / load-source / command / comms events are inherently
    # edge-triggered and don't go through dedupe.
    DEDUPE_WINDOW_S = 60.0

    def __init__(self, cfg: SlackConfig, db: Database, *, site_name: str = ""):
        self.cfg = cfg
        self.db = db
        self.site_name = site_name
        self._queue: asyncio.Queue[_PendingMessage] = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._worker_task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        self._running = False
        # Per-(code, kind) last-sent timestamps. Read+write only from
        # the event loop (alert_* methods are async); no lock needed.
        self._recent_sends: dict[tuple[str, str], float] = {}
        # ── Load-source debounce state (see alert_load_source_change) ──
        # The in-flight delayed alert, the source it will announce, and
        # the last source we actually told Slack about. The last one is
        # what makes a flap that returns to the announced state collapse
        # into no message at all.
        self._ls_pending: asyncio.Task | None = None
        self._ls_pending_to: str = ""
        self._ls_pending_from: str = ""
        self._ls_announced: str | None = None

    # ---- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="slack-worker")
        log.info(
            "Slack notifier started (enabled=%s, channel=%s, token=%s)",
            self.cfg.enabled,
            self.cfg.channel or "(unset)",
            "set" if self.cfg.bot_token else "unset",
        )

    async def stop(self) -> None:
        self._running = False
        if self._ls_pending is not None:
            self._ls_pending.cancel()
            try:
                await self._ls_pending
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._ls_pending = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker_task = None
        # Cancel any pending retry timers
        for t in list(self._retry_tasks):
            t.cancel()
        for t in list(self._retry_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._retry_tasks.clear()

    def update_config(self, cfg: SlackConfig) -> None:
        """Hot-reload the config — picked up on the next send."""
        self.cfg = cfg
        log.info(
            "Slack config updated (enabled=%s, channel=%s)",
            self.cfg.enabled, self.cfg.channel or "(unset)",
        )

    def is_enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.bot_token and self.cfg.channel)

    # ---- event helpers ------------------------------------------------
    def _dedupe_skip(self, code: str, kind: str) -> bool:
        """True if (code, kind) was sent inside DEDUPE_WINDOW_S.

        Records the current time on a miss so the next call within the
        window short-circuits. A chatty alarm bit that flickers
        set/clear under min_poll_count threshold gets one raise and
        one clear per window, not one of each per flicker.
        """
        now = time.time()
        key = (code, kind)
        # Best-effort housekeeping — keep the dict bounded under flap.
        # 10× window is plenty; entries past that are useless even if
        # the alarm later re-fires.
        if len(self._recent_sends) > 256:
            cutoff = now - self.DEDUPE_WINDOW_S * 10
            self._recent_sends = {
                k: t for k, t in self._recent_sends.items() if t > cutoff
            }
        last = self._recent_sends.get(key)
        if last is not None and (now - last) < self.DEDUPE_WINDOW_S:
            return True
        self._recent_sends[key] = now
        return False

    async def alert_alarm(self, code: str, desc: str, severity: str, ts: float) -> None:
        if severity == "alarm" and not self.cfg.alert_on_alarm:
            return
        if severity == "warn" and not self.cfg.alert_on_warning:
            return
        if self._dedupe_skip(code, "raised"):
            log.debug("Slack dedupe — skipping repeat alarm %s within %.0fs", code, self.DEDUPE_WINDOW_S)
            return
        if severity == "alarm":
            title = f":rotating_light: Alarm raised — {desc}"
        else:
            title = f":warning: Warning — {desc}"
        await self._enqueue(
            severity=severity,
            title=title,
            fields=[("Code", code), ("Site", self._site())],
            fallback=f"{severity.upper()}: {desc} ({code}) @ {self._site()}",
        )

    async def alert_alarm_cleared(self, code: str, desc: str, ts: float) -> None:
        if not self.cfg.alert_on_alarm_cleared:
            return
        if self._dedupe_skip(code, "cleared"):
            log.debug("Slack dedupe — skipping repeat clear %s within %.0fs", code, self.DEDUPE_WINDOW_S)
            return
        await self._enqueue(
            severity="ok",
            title=f":white_check_mark: Alarm cleared — {desc or code}",
            fields=[("Code", code), ("Site", self._site())],
            fallback=f"Alarm cleared: {desc or code} ({code}) @ {self._site()}",
        )

    async def alert_state_change(self, old: str, new: str, ts: float) -> None:
        if not self.cfg.alert_on_state_change:
            return
        await self._enqueue(
            severity="info",
            title=f":arrows_counterclockwise: Engine state — {old} → {new}",
            fields=[("Site", self._site())],
            fallback=f"Engine state {old} → {new} @ {self._site()}",
        )

    async def alert_load_source_change(self, old: str, new: str, ts: float) -> None:
        """Notify on a utility ↔ generator load-source transition.

        This is the alert operators actually wait for: the load just moved
        to backup power, or the utility is back. It is driven by the
        ATS-Pi's physical switch position when that companion is
        authoritative and by the H-100 electrical inference otherwise, so
        it fires with or without the companion.

        Gating, in order:
          1. ``alert_on_load_source_change`` — master switch.
          2. The boot-time ``unknown → utility`` firming is suppressed;
             it's an artifact of starting up, not a transfer.
          3. Per-direction flags, so a site can page on the outage and
             stay quiet on the restore (or the reverse).
          4. ``load_source_debounce_s`` — hold the message and only send
             it if the load source is still there when the timer expires.
        """
        if not self.cfg.alert_on_load_source_change:
            return
        if old == "unknown" and new == "utility":
            return
        if new == "generator" and not self.cfg.alert_on_transfer_to_generator:
            return
        if new == "utility" and not self.cfg.alert_on_return_to_utility:
            return
        if new not in ("generator", "utility") and not self.cfg.alert_on_load_source_unknown:
            return

        debounce = max(0.0, float(self.cfg.load_source_debounce_s or 0.0))
        if debounce <= 0:
            self._ls_announced = new
            await self._send_load_source(old, new)
            return

        # Debounced path. A newer transition supersedes whatever is
        # waiting: cancel the pending task and re-arm with the new
        # destination, keeping `from` anchored to the last state Slack was
        # told about, so the message reads truthfully after a flap.
        if self._ls_announced is None:
            # Nothing announced yet — as far as Slack is concerned the
            # load has been on `old` all along. Recording that here is
            # what lets a flap back to `old` cancel the alert entirely
            # instead of posting "Load on UTILITY (was utility)".
            self._ls_announced = old
        if self._ls_pending is not None and not self._ls_pending.done():
            self._ls_pending.cancel()
        self._ls_pending_from = self._ls_announced
        self._ls_pending_to = new
        self._ls_pending = asyncio.create_task(
            self._load_source_after(debounce), name="slack-load-source-debounce"
        )

    async def _load_source_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        new = self._ls_pending_to
        old = self._ls_pending_from
        self._ls_pending = None
        if not self._running:
            return
        if self._ls_announced is not None and new == self._ls_announced:
            # The source flapped away and came back inside the window —
            # from Slack's point of view nothing changed, so say nothing.
            log.info(
                "Slack: load source settled back on %s within the debounce window "
                "— no alert sent", new,
            )
            return
        self._ls_announced = new
        try:
            await self._send_load_source(old, new)
        except Exception as e:  # noqa: BLE001
            log.exception("Slack: debounced load-source send failed: %s", e)

    async def _send_load_source(self, old: str, new: str) -> None:
        old = old or "unknown"
        if new == "generator":
            emoji, sev = ":zap:", "warn"
            title = f"{emoji} Load on GENERATOR (was {old})"
            mention = self.cfg.mention_on_transfer_to_generator
        elif new == "utility":
            emoji, sev = ":electric_plug:", "ok"
            title = f"{emoji} Load on UTILITY (was {old})"
            mention = self.cfg.mention_on_return_to_utility
        else:
            emoji, sev = ":grey_question:", "warn"
            title = f"{emoji} Load source UNKNOWN (was {old})"
            mention = ""
        await self._enqueue(
            severity=sev,
            title=title,
            fields=[("Site", self._site())],
            fallback=f"Load source {old} → {new} @ {self._site()}",
            channel=self.cfg.channel_load_source,
            mention=mention,
        )

    async def alert_command(self, verb: str, operator: str, result: str, ts: float) -> None:
        if not self.cfg.alert_on_command:
            return
        sev = "info" if result == "ok" else "warn"
        emoji = ":joystick:" if result == "ok" else ":x:"
        await self._enqueue(
            severity=sev,
            title=f"{emoji} Control command — `{verb}` ({result})",
            fields=[("Operator", operator), ("Site", self._site())],
            fallback=f"Command {verb} by {operator}: {result} @ {self._site()}",
        )

    async def alert_comms_change(self, old: str, new: str, success_pct: float, ts: float) -> None:
        if not self.cfg.alert_on_comms_lost:
            return
        # Only fire on transitions involving non-healthy state, to keep
        # the channel quiet during normal jitter between healthy/degraded.
        if old == new:
            return
        if old == "healthy" and new == "degraded":
            # don't alert until either lost or recovered
            return
        recovered = new == "healthy"
        emoji = ":white_check_mark:" if recovered else ":satellite_antenna:"
        sev = "ok" if recovered else "warn"
        title = f"{emoji} Modbus comms — {old} → {new}"
        await self._enqueue(
            severity=sev,
            title=title,
            fields=[
                ("Success rate", f"{success_pct:.1f}%"),
                ("Site", self._site()),
            ],
            fallback=f"Modbus comms {old} → {new} ({success_pct:.0f}%) @ {self._site()}",
        )

    # ---- security events ----------------------------------------------
    # An internet-exposed login deserves the same treatment as an alarm
    # bit: the operator finds out from Slack, not from reading the audit
    # table after something has already gone wrong.

    async def alert_login_failure(
        self, *, username: str, ip: str, reason: str, attempts: int, ts: float = 0.0
    ) -> None:
        if not self.cfg.alert_on_login_failure:
            return
        # Dedupe per account per window: a spray attack must not be able
        # to push real alarms out of the 200-slot queue. The lockout alert
        # below is the one that carries the "this is happening" signal.
        if self._dedupe_skip(f"login:{username or '?'}", "failure"):
            return
        await self._enqueue(
            severity="warn",
            title=f":no_entry: Failed sign-in — `{username or '(no username)'}`",
            fields=[
                ("Source IP", ip or "unknown"),
                ("Reason", reason or "invalid credentials"),
                ("Failed attempts", str(attempts)),
                ("Site", self._site()),
            ],
            fallback=f"Failed sign-in for {username or '(none)'} from {ip} @ {self._site()}",
            channel=self.cfg.channel_security,
        )

    async def alert_account_lockout(
        self, *, username: str, ip: str, seconds: float, ts: float = 0.0
    ) -> None:
        if not self.cfg.alert_on_account_lockout:
            return
        mins = max(1, int(round(seconds / 60.0)))
        await self._enqueue(
            severity="alarm",
            title=f":lock: Account locked — `{username}`",
            fields=[
                ("Locked for", f"{mins} min"),
                ("Source IP", ip or "unknown"),
                ("Site", self._site()),
            ],
            fallback=f"Account {username} locked for {mins} min after repeated failures @ {self._site()}",
            channel=self.cfg.channel_security,
        )

    async def alert_login_success(
        self, *, username: str, ip: str, method: str = "password", ts: float = 0.0
    ) -> None:
        if not self.cfg.alert_on_login_success:
            return
        await self._enqueue(
            severity="info",
            title=f":key: Sign-in — `{username}`",
            fields=[("Source IP", ip or "unknown"), ("Method", method), ("Site", self._site())],
            fallback=f"{username} signed in from {ip} @ {self._site()}",
            channel=self.cfg.channel_security,
        )

    async def alert_user_change(
        self, *, action: str, target: str, actor: str, detail: str = "", ts: float = 0.0
    ) -> None:
        """Account created / deleted / role changed / password reset / 2FA changed."""
        if not self.cfg.alert_on_user_change:
            return
        fields = [("Account", target), ("By", actor or "system"), ("Site", self._site())]
        if detail:
            fields.insert(2, ("Detail", detail))
        await self._enqueue(
            severity="warn",
            title=f":bust_in_silhouette: Account {action} — `{target}`",
            fields=fields,
            fallback=f"Account {action}: {target} by {actor or 'system'} @ {self._site()}",
            channel=self.cfg.channel_security,
        )

    # ---- test ---------------------------------------------------------
    async def test(self, kind: str = "generic") -> tuple[bool, str]:
        """Send a synchronous test message. Returns (ok, error_or_'ok').

        ``kind`` picks which alert *route* to exercise, so an operator can
        confirm the channel override and the mention text actually work
        before an outage is the thing testing them:

          generic      — the plain channel
          load_source  — channel_load_source + mention_on_transfer_to_generator
          security     — channel_security
        """
        if not self.cfg.bot_token:
            return False, "bot_token not set"
        channel, mention, label = self._route_for_test(kind)
        if not channel:
            return False, "channel not set"
        when = time.strftime("%Y-%m-%d %H:%M:%S %Z").strip() or "(unknown)"
        title = f":test_tube: Castle Generator Monitor — {label} test"
        if mention:
            title = f"{mention} {title}"
        blocks = _build_blocks(
            severity="info",
            title=title,
            fields=[("Site", self._site()), ("Route", label), ("When", when)],
        )
        fallback = f"Castle Generator Monitor {label} test message from {self._site()}"
        ok, err = await self._send(blocks, fallback, timeout=self.SEND_TIMEOUT_S, channel=channel)
        return ok, "ok" if ok else err

    def _route_for_test(self, kind: str) -> tuple[str, str, str]:
        if kind == "load_source":
            return (
                self.cfg.channel_load_source or self.cfg.channel,
                self.cfg.mention_on_transfer_to_generator,
                "transfer alert",
            )
        if kind == "security":
            return (self.cfg.channel_security or self.cfg.channel, "", "security alert")
        return (self.cfg.channel, "", "general")

    # ---- internals ----------------------------------------------------
    def _site(self) -> str:
        return self.cfg.site_label or self.site_name or "Castle Generator Monitor"

    async def _enqueue(
        self,
        *,
        severity: str,
        title: str,
        fields: list[tuple[str, str]],
        fallback: str,
        channel: str = "",
        mention: str = "",
    ) -> None:
        if not self.is_enabled():
            return
        if mention:
            # The mention has to sit inside the message body for Slack to
            # actually notify anyone — a mention in metadata is just text.
            # It goes in both the block title and the notification-preview
            # fallback so the push notification names the ping too.
            title = f"{mention} {title}"
            fallback = f"{mention} {fallback}"
        blocks = _build_blocks(severity=severity, title=title, fields=fields)
        msg = _PendingMessage(
            blocks=blocks, fallback=fallback, enqueued_at=time.time(), channel=channel,
        )
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Prefer FRESH alerts over a stale backlog. Under a sustained
            # Slack outage the queue fills with old retried messages;
            # dropping the *newest* (the previous behaviour) meant the most
            # actionable alarm was the one thrown away, and because the
            # dedupe timestamp was already recorded, its immediate retry
            # was suppressed too. Evict the oldest to make room so the new
            # alert (whose dedupe we just recorded) actually gets queued.
            try:
                dropped = self._queue.get_nowait()
                log.warning("Slack queue full — evicted oldest (%s) to enqueue %s",
                            dropped.fallback, fallback)
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("Slack queue full — dropping notification: %s", fallback)

    async def _worker(self) -> None:
        while self._running:
            try:
                msg = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                ok, err = await self._send(
                    msg.blocks, msg.fallback,
                    timeout=self.SEND_TIMEOUT_S, channel=msg.channel,
                )
                if ok:
                    continue
                # Retry on transport errors only — slack_error responses
                # (auth, channel_not_found, etc.) are config issues.
                if err.startswith("slack_error"):
                    log.error("Slack rejected message: %s — %s", err, msg.fallback)
                    continue
                msg.attempt += 1
                if msg.attempt >= self.MAX_ATTEMPTS:
                    log.error(
                        "Slack send failed after %d attempts: %s — %s",
                        msg.attempt, err, msg.fallback,
                    )
                    continue
                # Wall-clock deadline: a sustained Slack outage stops
                # producing retry tasks for old messages. Without this,
                # a 30-minute Slack outage with chatty events queues
                # ~16s of retries per message; backpressure pushes
                # newer (higher-signal) alerts off the floor.
                if msg.enqueued_at and (time.time() - msg.enqueued_at) > self.MAX_AGE_S:
                    log.warning(
                        "Slack: abandoning message past %ds deadline: %s",
                        int(self.MAX_AGE_S), msg.fallback,
                    )
                    continue
                backoff = self.BACKOFF_S[min(msg.attempt - 1, len(self.BACKOFF_S) - 1)]
                log.warning(
                    "Slack send failed (%s), retry %d/%d in %.1fs",
                    err, msg.attempt, self.MAX_ATTEMPTS, backoff,
                )
                t = asyncio.create_task(self._requeue_after(backoff, msg))
                self._retry_tasks.add(t)
                t.add_done_callback(self._retry_tasks.discard)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.exception("Slack worker error: %s", e)

    async def _requeue_after(self, delay: float, msg: _PendingMessage) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("Slack queue full on retry — dropping: %s", msg.fallback)

    async def _send(
        self,
        blocks: list[dict],
        fallback: str,
        *,
        timeout: float,
        channel: str = "",
    ) -> tuple[bool, str]:
        token = self.cfg.bot_token
        # Per-message override (transfer / security routing) falls back to
        # the main channel, so clearing an override can't silently drop
        # alerts on the floor.
        channel = channel or self.cfg.channel
        if not token or not channel:
            return False, "not_configured"
        payload = {
            "channel": channel,
            "text": fallback,  # for notification previews & accessibility
            "blocks": blocks,
        }
        try:
            return await asyncio.to_thread(_post_slack, token, payload, timeout)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


def _post_slack(token: str, payload: dict, timeout: float) -> tuple[bool, str]:
    """Synchronous POST to Slack — run via asyncio.to_thread."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API_POST_MESSAGE,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "GenWatch/0.1 (+https://github.com/sethmorrowsoftware/genwatch)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        return False, f"http_{e.code} {raw[:200]}"
    except urllib.error.URLError as e:
        return False, f"url_error {e.reason!r}"
    except TimeoutError:
        return False, "timeout"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"invalid_json {raw[:200]}"

    if data.get("ok"):
        return True, "ok"
    return False, f"slack_error {data.get('error', 'unknown')}"


def _build_blocks(*, severity: str, title: str, fields: list[tuple[str, str]]) -> list[dict]:
    """Render a Block Kit message body.

    Slack Block Kit doesn't expose a sidebar color when using
    chat.postMessage with blocks (that was an 'attachments' concept),
    so we lean on emoji prefixes in the title and a context block at
    the bottom for severity + provenance.
    """
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}
    ]
    if fields:
        chunks = [{"type": "mrkdwn", "text": f"*{label}*\n{val}"} for label, val in fields]
        # Slack rejects more than 10 fields in one block; chunk if needed.
        for i in range(0, len(chunks), 10):
            blocks.append({"type": "section", "fields": chunks[i : i + 10]})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_severity:_ `{severity}` · _via Castle Generator Monitor_"}
            ],
        }
    )
    return blocks
