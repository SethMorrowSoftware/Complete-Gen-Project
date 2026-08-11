"""Read/write the deployment config + register map.

GET  /api/config        full effective config (sanitized — no secrets)
PUT  /api/config        admin-only; writes to disk, reloads poller
GET  /api/registers     current register map (for the Settings UI table)
POST /api/registers/reload  re-read registers.yaml from disk
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .deps import Principal, require_admin, require_operator

log = logging.getLogger("genwatch.api.settings")

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/config")
async def get_config(
    request: Request,
    p: Principal = Depends(require_operator),
) -> dict:
    s = request.app.state.settings
    return {
        "configPath": s.config_path,
        "mock": s.mock,
        "transport": s.transport,
        "serial": s.serial.model_dump(),
        "modbus_tcp": s.modbus_tcp.model_dump(),
        "modbus": s.modbus.model_dump(),
        "retention": s.retention.model_dump(),
        "auth": {
            "operatorName": s.auth.operator_name,
            "sessionHours": s.auth.session_hours,
            "idleTimeoutMinutes": s.auth.idle_timeout_minutes,
            "rememberMeDays": s.auth.remember_me_days,
            "passwordConfigured": bool(s.auth.admin_password_hash),
            "jwtSecretConfigured": bool(s.auth.jwt_secret),
            "requireTotp": s.auth.require_totp,
            "passwordMinLength": s.auth.password_min_length,
            "lockoutThreshold": s.auth.lockout_threshold,
            "lockoutSeconds": s.auth.lockout_seconds,
            "accountCount": request.app.state.db.user_count(),
        },
        "security": {
            "publicExposure": s.security.public_exposure,
            "requireHttps": s.https_required,
            "headersEnabled": s.security.headers_enabled,
            "hstsEnabled": s.security.hsts_enabled,
            "ipAllowlistCount": len(s.security.ip_allowlist),
        },
        "slack": {
            "enabled": s.slack.enabled,
            "channel": s.slack.channel,
            "siteLabel": s.slack.site_label,
            # Never return the bot token itself; just confirm presence.
            "botTokenConfigured": bool(s.slack.bot_token),
            "alertOnAlarm": s.slack.alert_on_alarm,
            "alertOnWarning": s.slack.alert_on_warning,
            "alertOnAlarmCleared": s.slack.alert_on_alarm_cleared,
            "alertOnStateChange": s.slack.alert_on_state_change,
            "alertOnCommand": s.slack.alert_on_command,
            "alertOnCommsLost": s.slack.alert_on_comms_lost,
            "alertOnLoadSourceChange": s.slack.alert_on_load_source_change,
            "alertOnTransferToGenerator": s.slack.alert_on_transfer_to_generator,
            "alertOnReturnToUtility": s.slack.alert_on_return_to_utility,
            "alertOnLoadSourceUnknown": s.slack.alert_on_load_source_unknown,
            "channelLoadSource": s.slack.channel_load_source,
            "mentionOnTransferToGenerator": s.slack.mention_on_transfer_to_generator,
            "mentionOnReturnToUtility": s.slack.mention_on_return_to_utility,
            "loadSourceDebounceS": s.slack.load_source_debounce_s,
            "alertOnFuelWarning": s.slack.alert_on_fuel_warning,
            "alertOnFuelCritical": s.slack.alert_on_fuel_critical,
            "alertOnFuelReminder": s.slack.alert_on_fuel_reminder,
            "alertOnFuelRecovered": s.slack.alert_on_fuel_recovered,
            "alertOnFuelDrop": s.slack.alert_on_fuel_drop,
            "channelFuel": s.slack.channel_fuel,
            "mentionOnFuelCritical": s.slack.mention_on_fuel_critical,
            "alertOnLoginFailure": s.slack.alert_on_login_failure,
            "alertOnAccountLockout": s.slack.alert_on_account_lockout,
            "alertOnLoginSuccess": s.slack.alert_on_login_success,
            "alertOnUserChange": s.slack.alert_on_user_change,
            "channelSecurity": s.slack.channel_security,
        },
        "fuel": {
            **s.fuel.model_dump(),
            # Tank size and fuel type come from the register map, not
            # config.yaml, but the UI needs them to show gallons and to
            # explain why a gaseous site never alerts.
            "tankGal": request.app.state.regmap.site.tank_gal,
            "fuelType": request.app.state.regmap.site.fuel_type,
        },
        "mqtt": {
            "enabled": s.mqtt.enabled,
            "host": s.mqtt.host,
            "port": s.mqtt.port,
            "topic": s.mqtt.topic,
            "payloadOn": s.mqtt.payload_on,
            "payloadOff": s.mqtt.payload_off,
            "qos": s.mqtt.qos,
            "retain": s.mqtt.retain,
            "username": s.mqtt.username,
            # Never return the password itself; just confirm presence.
            "passwordConfigured": bool(s.mqtt.password),
            "clientId": s.mqtt.client_id,
            "tls": s.mqtt.tls,
            "tlsInsecure": s.mqtt.tls_insecure,
            "publishOnStart": s.mqtt.publish_on_start,
        },
        "wsPushMs": s.ws_push_ms,
    }


class SlackUpdate(BaseModel):
    enabled: bool | None = None
    # bot_token: empty string clears, None preserves on-disk value.
    bot_token: str | None = None
    channel: str | None = None
    site_label: str | None = None
    alert_on_alarm: bool | None = None
    alert_on_warning: bool | None = None
    alert_on_alarm_cleared: bool | None = None
    alert_on_state_change: bool | None = None
    alert_on_command: bool | None = None
    alert_on_comms_lost: bool | None = None
    alert_on_load_source_change: bool | None = None
    # Transfer-alert routing and per-direction gating.
    alert_on_transfer_to_generator: bool | None = None
    alert_on_return_to_utility: bool | None = None
    alert_on_load_source_unknown: bool | None = None
    channel_load_source: str | None = None
    mention_on_transfer_to_generator: str | None = None
    mention_on_return_to_utility: str | None = None
    load_source_debounce_s: float | None = None
    # Fuel alerts (thresholds live in the `fuel` section).
    alert_on_fuel_warning: bool | None = None
    alert_on_fuel_critical: bool | None = None
    alert_on_fuel_reminder: bool | None = None
    alert_on_fuel_recovered: bool | None = None
    alert_on_fuel_drop: bool | None = None
    channel_fuel: str | None = None
    mention_on_fuel_critical: str | None = None
    # Sign-in / account security alerts.
    alert_on_login_failure: bool | None = None
    alert_on_account_lockout: bool | None = None
    alert_on_login_success: bool | None = None
    alert_on_user_change: bool | None = None
    channel_security: str | None = None


class MqttUpdate(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = None
    topic: str | None = None
    payload_on: str | None = None
    payload_off: str | None = None
    qos: int | None = None
    retain: bool | None = None
    username: str | None = None
    # password: empty string clears, None preserves on-disk value.
    password: str | None = None
    client_id: str | None = None
    tls: bool | None = None
    tls_insecure: bool | None = None
    publish_on_start: bool | None = None


class FuelUpdate(BaseModel):
    enabled: bool | None = None
    warn_pct: float | None = None
    critical_pct: float | None = None
    hysteresis_pct: float | None = None
    renotify_hours: float | None = None
    min_valid_pct: float | None = None
    max_valid_pct: float | None = None
    drop_alert_pct: float | None = None
    drop_window_minutes: int | None = None
    drop_only_when_stopped: bool | None = None


class ConfigUpdate(BaseModel):
    transport: str | None = None
    serial: dict | None = None
    modbus_tcp: dict | None = None
    modbus: dict | None = None
    retention: dict | None = None
    slack: SlackUpdate | None = None
    fuel: FuelUpdate | None = None
    mqtt: MqttUpdate | None = None
    ws_push_ms: int | None = None


# Every field of SlackUpdate hot-reloads — the notifier reads its config
# on each send, so a PUT takes effect on the next alert with no restart.
# Derived from the model so a new option can't be added to SlackUpdate and
# silently forgotten here.
_SLACK_HOTRELOAD_FIELDS = set(SlackUpdate.model_fields)


@router.put("/config")
async def update_config(
    request: Request,
    body: ConfigUpdate,
    p: Principal = Depends(require_admin),
) -> dict:
    s = request.app.state.settings
    if not s.config_path:
        raise HTTPException(
            409,
            "no config.yaml path configured — set GENWATCH_CONFIG_PATH or copy deploy/config.yaml.example",
        )

    cfg_path = Path(s.config_path)
    # Read existing on-disk yaml (preserve fields we don't touch)
    on_disk: dict = {}
    if cfg_path.exists():
        with cfg_path.open() as f:
            on_disk = yaml.safe_load(f) or {}

    if body.transport is not None:
        if body.transport not in ("serial", "tcp"):
            raise HTTPException(400, "transport must be 'serial' or 'tcp'")
        on_disk["transport"] = body.transport
    if body.serial:
        on_disk.setdefault("serial", {}).update(body.serial)
    if body.modbus_tcp:
        on_disk.setdefault("modbus_tcp", {}).update(body.modbus_tcp)
    if body.modbus:
        on_disk.setdefault("modbus", {}).update(body.modbus)
    if body.retention:
        on_disk.setdefault("retention", {}).update(body.retention)
    if body.ws_push_ms is not None:
        on_disk["ws_push_ms"] = int(body.ws_push_ms)

    slack_changed = False
    if body.slack is not None:
        # Pull only the fields the operator actually sent (exclude None
        # → don't touch). bot_token == "" is explicit clear.
        slack_patch = body.slack.model_dump(exclude_none=True)
        if slack_patch:
            on_disk.setdefault("slack", {}).update(slack_patch)
            slack_changed = True

    fuel_changed = False
    if body.fuel is not None:
        fuel_patch = body.fuel.model_dump(exclude_none=True)
        if fuel_patch:
            on_disk.setdefault("fuel", {}).update(fuel_patch)
            fuel_changed = True

    mqtt_changed = False
    if body.mqtt is not None:
        # Same rule as slack: only the fields sent; password == "" clears.
        mqtt_patch = body.mqtt.model_dump(exclude_none=True)
        if mqtt_patch:
            on_disk.setdefault("mqtt", {}).update(mqtt_patch)
            mqtt_changed = True

    # Atomic write: tmp -> rename
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w") as f:
        yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
    shutil.move(tmp, cfg_path)

    # Slack + MQTT settings hot-reload — no restart required. Both are
    # folded into a single settings.model_copy so a PUT touching both
    # doesn't drop one update.
    settings_updates: dict = {}

    slack_for_audit = None
    if slack_changed:
        from ..config import SlackConfig
        # Sanitize what we audit (don't echo the token back to the audit log)
        slack_for_audit = {**slack_patch}
        if "bot_token" in slack_for_audit:
            slack_for_audit["bot_token"] = "<set>" if slack_for_audit["bot_token"] else "<cleared>"
        merged = {**s.slack.model_dump(), **slack_patch}
        settings_updates["slack"] = SlackConfig(**merged)

    if fuel_changed:
        from ..config import FuelConfig
        settings_updates["fuel"] = FuelConfig(**{**s.fuel.model_dump(), **fuel_patch})

    mqtt_for_audit = None
    if mqtt_changed:
        from ..config import MqttConfig
        # Sanitize what we audit (don't echo the broker password)
        mqtt_for_audit = {**mqtt_patch}
        if "password" in mqtt_for_audit:
            mqtt_for_audit["password"] = "<set>" if mqtt_for_audit["password"] else "<cleared>"
        merged_m = {**s.mqtt.model_dump(), **mqtt_patch}
        settings_updates["mqtt"] = MqttConfig(**merged_m)

    if settings_updates:
        new_settings = s.model_copy(update=settings_updates)
        request.app.state.settings = new_settings
        if slack_changed:
            notifier = getattr(request.app.state, "slack", None)
            if notifier is not None:
                notifier.update_config(settings_updates["slack"])
        if fuel_changed:
            # Hot-reload the live monitor. It keeps its accumulated state
            # (current status, reminder timer, drop history) across the
            # swap, so raising a threshold doesn't re-announce a tank
            # that was already low.
            sm = getattr(request.app.state, "state_machine", None)
            if sm is not None and getattr(sm, "fuel", None) is not None:
                sm.fuel.update_config(settings_updates["fuel"])
        if mqtt_changed:
            publisher = getattr(request.app.state, "mqtt", None)
            if publisher is not None:
                publisher.update_config(settings_updates["mqtt"])

    audit_detail = body.model_dump(exclude_none=True)
    if "slack" in audit_detail and slack_for_audit is not None:
        audit_detail["slack"] = slack_for_audit
    if "mqtt" in audit_detail and mqtt_for_audit is not None:
        audit_detail["mqtt"] = mqtt_for_audit
    request.app.state.db.write_audit(p.operator, "config.update", str(audit_detail), "", "ok")

    # Slack-only changes don't require a restart; transport/serial/modbus do.
    restart_required = any(v is not None for v in (
        body.transport, body.serial, body.modbus_tcp, body.modbus, body.retention, body.ws_push_ms,
    ))
    log.info(
        "config updated on disk by %s (slack=%s, fuel=%s, mqtt=%s, restart_required=%s)",
        p.operator, slack_changed, fuel_changed, mqtt_changed, restart_required,
    )
    return {
        "ok": True,
        "configPath": str(cfg_path),
        "restart_required": restart_required,
        "slack_updated": slack_changed,
        "fuel_updated": fuel_changed,
        "mqtt_updated": mqtt_changed,
    }


@router.post("/slack/test")
async def test_slack(
    request: Request,
    kind: str = Query("generic", pattern="^(generic|load_source|fuel|security)$"),
    p: Principal = Depends(require_admin),
) -> dict:
    """Send a synchronous test message to Slack.

    Uses the current in-memory configuration (which reflects the most
    recent PUT /api/config). Returns 200 with ``{ok, detail}`` even on
    failure so the UI can surface the Slack error verbatim instead of
    swallowing it as an HTTP error.

    ``kind`` selects which alert *route* to exercise — the transfer, fuel
    and security alerts can each be pointed at their own channel with
    their own mention text, and an operator should be able to prove that
    plumbing works before an outage is what tests it.
    """
    notifier = getattr(request.app.state, "slack", None)
    if notifier is None:
        raise HTTPException(503, "slack notifier not initialised")
    ok, detail = await notifier.test(kind)
    request.app.state.db.write_audit(
        p.operator, "slack.test", f"kind={kind} {detail if not ok else ''}".strip(),
        "", "ok" if ok else "failed",
    )
    return {"ok": ok, "detail": detail}


@router.post("/mqtt/test")
async def test_mqtt(
    request: Request,
    p: Principal = Depends(require_admin),
) -> dict:
    """Publish a one-shot (non-retained) test message to the MQTT broker.

    Uses the current in-memory configuration (reflecting the most recent
    PUT /api/config). Returns 200 with ``{ok, detail}`` even on failure so
    the UI can surface the broker/connection error verbatim. The test
    payload is NOT retained, so it doesn't clobber the real generator
    status on the broker.
    """
    publisher = getattr(request.app.state, "mqtt", None)
    if publisher is None:
        raise HTTPException(503, "mqtt publisher not initialised")
    ok, detail = await publisher.test()
    request.app.state.db.write_audit(
        p.operator, "mqtt.test", detail if not ok else "", "", "ok" if ok else "failed"
    )
    return {"ok": ok, "detail": detail}


@router.get("/registers")
async def get_registers(
    request: Request,
    p: Principal = Depends(require_operator),
) -> dict:
    rm = request.app.state.regmap
    snap = request.app.state.state_machine.snap
    reading = snap.last_reading.values

    out = []
    for r in rm.registers:
        out.append({
            "addr": f"0x{r.addr:04X}",
            "name": r.name,
            "fc": f"0{r.fc}",
            "type": r.type,
            "tier": r.tier,
            "group": r.group,
            "unit": r.unit,
            "scale": r.scale if r.scale != 1.0 else None,
            "value": reading.get(r.name),
        })
    for c in rm.controls.values():
        out.append({
            "addr": f"0x{c.addr:04X}",
            "name": c.name,
            "fc": f"0{c.fc}",
            "type": "u16",
            "tier": "controls",
            "group": "Controls · write-gated",
            "unit": "cmd",
            "scale": None,
            "value": None,
        })

    return {
        "path": str(rm.path),
        "slave": rm.slave,
        "primePollMs": rm.prime_poll_ms,
        "basePollMs": rm.base_poll_ms,
        "registers": out,
    }


@router.post("/registers/reload")
async def reload_registers(
    request: Request,
    p: Principal = Depends(require_admin),
) -> dict:
    from ..modbus.registers import load_register_map

    rm_old = request.app.state.regmap
    try:
        rm_new = load_register_map(rm_old.path)
    except Exception as e:  # noqa: BLE001
        request.app.state.db.write_audit(p.operator, "registers.reload", str(e), "", "failed")
        raise HTTPException(400, f"register map invalid: {e}")

    # Hot-swap into every live consumer so the next poll, the next state
    # derivation, and the next control write all see the new map. Order
    # is deliberate: poller first (it's what's actually reading the bus
    # — start there to bound the inconsistency window), then state +
    # control (which only read derived values keyed by name).
    poller = getattr(request.app.state, "poller", None)
    state_machine = getattr(request.app.state, "state_machine", None)
    control = getattr(request.app.state, "control", None)
    if poller is not None:
        await poller.apply_regmap(rm_new)
    if state_machine is not None:
        state_machine.apply_regmap(rm_new)
    if control is not None:
        await control.apply_regmap(rm_new)
    request.app.state.regmap = rm_new

    request.app.state.db.write_audit(p.operator, "registers.reload", str(rm_new.path), "", "ok")
    request.app.state.db.write_event(
        severity="info",
        type_="CONFIG",
        message=f"Register file reloaded — {rm_new.path.name}",
        meta=f"{len(rm_new.registers)} regs · {len(rm_new.controls)} controls",
    )
    return {"ok": True, "registers": len(rm_new.registers), "controls": len(rm_new.controls)}


@router.get("/registers/verify")
async def verify_registers(
    request: Request,
    p: Principal = Depends(require_admin),
) -> dict:
    from ..modbus.registers import validate_register_map

    rm = request.app.state.regmap
    report = validate_register_map(rm)
    live = {
        "skipped": request.app.state.settings.mock,
        "ok": True,
        "tested": 0,
        "failed": 0,
        "failures": [],
    }
    if not request.app.state.settings.mock:
        failures = []
        for reg in rm.registers:
            r = await request.app.state.client.read(reg.addr, 1, fc=reg.fc)
            if not r.ok:
                failures.append({
                    "name": reg.name,
                    "addr": f"0x{reg.addr:04X}",
                    "fc": reg.fc,
                    "error": r.error,
                })
        live = {
            "skipped": False,
            "ok": len(failures) == 0,
            "tested": len(rm.registers),
            "failed": len(failures),
            "failures": failures,
        }

    overall_ok = report.ok and live["ok"]
    request.app.state.db.write_audit(
        p.operator,
        "registers.verify",
        f"static_ok={report.ok} live_ok={live['ok']} tested={live['tested']} failed={live['failed']}",
        "",
        "ok" if overall_ok else "failed",
    )
    return {
        "ok": overall_ok,
        "static": {
            "ok": report.ok,
            "errors": report.errors,
            "warnings": report.warnings,
        },
        "live": live,
    }
