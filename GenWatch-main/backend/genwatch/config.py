"""Layered configuration.

Three layers, highest-to-lowest priority:
  1. Environment variables (GENWATCH_*)
  2. /etc/genwatch/config.yaml (deployment)
  3. Built-in defaults

The register map (registers/h100.yaml) is loaded separately and is
hot-reloadable; it is *not* part of this Pydantic model.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_CONFIG_PATHS = [
    "/etc/genwatch/config.yaml",
    "./config.yaml",
]

# Holds the parsed config.yaml for the current load() call so the YAML
# settings source (ranked BELOW env) can read it. A ContextVar keeps it
# reentrancy-safe — load() sets it around construction and resets after.
_yaml_ctx: ContextVar[dict] = ContextVar("genwatch_yaml_cfg", default={})


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Feeds config.yaml into Settings as a source ranked below env.

    The previous implementation passed the YAML dict as constructor
    kwargs, which in pydantic-settings outranks *every* environment
    source — so any key present in config.yaml silently ignored its
    GENWATCH_* override, the exact opposite of the documented contract.
    Wiring the YAML in as a proper low-priority source restores
    "env wins", and pydantic-settings deep-merges the nested models so an
    env override of one field (e.g. GENWATCH_AUTH__JWT_SECRET) doesn't
    wipe the sibling YAML fields.
    """

    def get_field_value(self, field, field_name):  # noqa: ANN001
        return _yaml_ctx.get().get(field_name), field_name, False

    def prepare_field_value(self, field_name, field, value, value_is_complex):  # noqa: ANN001
        return value

    def __call__(self) -> dict:
        data = _yaml_ctx.get()
        return {name: data[name] for name in self.settings_cls.model_fields if name in data}


class SerialConfig(BaseModel):
    device: str = "/dev/ttyUSB0"
    baud: int = 9600
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    bytesize: Literal[7, 8] = 8
    timeout_s: float = 1.5


class ModbusTcpConfig(BaseModel):
    """Network bridge to the H-100's serial port.

    Used when ``transport: tcp`` — typically a Lantronix UDS/EDS/xDirect
    or similar terminal server that tunnels raw bytes between a TCP
    socket and a physical RS-232/RS-485 port. The H-100 frames Modbus
    **RTU** on the wire, so the framer must stay 'rtu' even though the
    transport is TCP — this is *not* Modbus/TCP.
    """

    host: str = "192.168.1.249"
    port: int = 10001  # Lantronix raw-TCP default (Channel 1)
    timeout_s: float = 1.5
    connect_timeout_s: float = 3.0
    framer: Literal["rtu", "socket"] = "rtu"


class ModbusConfig(BaseModel):
    slave: int = 100
    read_fc: Literal[3, 4] = 3
    prime_poll_ms: int = 1500
    base_poll_ms: int = 15000
    retries: int = 2
    register_file: str = "registers/h100.yaml"

    @field_validator("slave")
    @classmethod
    def _slave_range(cls, v: int) -> int:
        if not 1 <= v <= 247:
            raise ValueError("Modbus slave must be 1..247")
        return v


class RetentionConfig(BaseModel):
    raw_days: int = 7
    rollup_1m_days: int = 90
    rollup_1h_days: int = 730
    # Info/ok events older than this are pruned. Alarms/warns are never
    # auto-pruned (kept for forensic value).
    events_days: int = 30
    audit_days: int = 0  # 0 = never delete


class AuthConfig(BaseModel):
    # ── Accounts ──────────────────────────────────────────────────────
    # Logins are per-user: named accounts with their own passwords live in
    # the database (`users` table), managed via Settings → Users in the UI
    # or the `genwatch useradd / userpasswd / userlist` CLI.
    #
    # admin_password_hash is the LEGACY single shared password. It is used
    # for exactly one thing now: seeding the first admin account on an
    # upgrade, so an existing deployment doesn't lock itself out. Once any
    # account exists it is never consulted again and can be cleared.
    # Generate: sudo -u genwatch genwatch hash
    admin_password_hash: str = ""
    # Username given to that seeded account.
    bootstrap_username: str = "admin"
    # Display name for the legacy single-operator identity. Kept for
    # backward compatibility with old audit rows; new sessions are
    # attributed to the account's username.
    operator_name: str = "operator"
    jwt_secret: str = ""  # filled at install-time
    session_hours: int = 12

    # ── "Keep me signed in" (remember me) ─────────────────────────────
    # Lifetime, in days, of a session started with the login form's
    # "keep me signed in on this device" box ticked. Remembered sessions
    # are exempt from idle_timeout_minutes, and are silently renewed
    # whenever the device uses the console with less than half the
    # lifetime left — so a device that signs in once stays signed in for
    # as long as it keeps being used at least once per remember_me_days.
    #
    # This trades "a stolen laptop holds a live session" for "the boss
    # doesn't retype a password every shift". Every revocation path still
    # applies in full: logout, a password change, disabling the account,
    # and Settings → Users → sign-out-everywhere all kill a remembered
    # session immediately, and it is listed like any other session under
    # Settings → My Account.
    #
    # 0 disables the feature — the checkbox is ignored server-side and
    # every login gets a plain session_hours session. Values past ~400
    # are academic: Chrome caps cookie lifetime at 400 days (renewal
    # re-issues the cookie, so the session itself survives regardless).
    remember_me_days: int = 365

    # ── Login hardening ───────────────────────────────────────────────
    # Sessions also expire after this much inactivity, independent of
    # session_hours. An operator who leaves the console open on a shop
    # laptop shouldn't hand over a live session for the rest of the day.
    # 0 disables the idle timeout (absolute expiry still applies).
    idle_timeout_minutes: int = 60

    # Failed sign-ins before the account itself locks (on top of the
    # per-IP rate limiter, which a botnet can sidestep by rotating IPs).
    lockout_threshold: int = 5
    # First lockout duration in seconds; doubles on each further lockout
    # up to lockout_max_seconds. 0 disables account lockout entirely
    # (NOT recommended when the console is reachable from the internet).
    lockout_seconds: int = 900
    lockout_max_seconds: int = 3600

    # Per-IP login token bucket: `login_rate_burst` attempts, then one
    # more every `login_rate_refill_seconds`.
    login_rate_burst: int = 5
    login_rate_refill_seconds: int = 180

    # Minimum length for new passwords. The policy also rejects common
    # passwords, keyboard runs, and passwords containing the username;
    # see services/auth.validate_password_strength. Passwords already on
    # disk are never re-validated — only new ones.
    password_min_length: int = 12

    # Require a TOTP second factor from every account. Enroll accounts
    # FIRST (Settings → Account, or `genwatch usertotp <name>`); accounts
    # that haven't enrolled are refused rather than silently exempted.
    require_totp: bool = False
    # Issuer name shown in the authenticator app.
    totp_issuer: str = "Castle Generator Monitor"

    # ── Session-cookie hardening ──────────────────────────────────────
    # cookie_secure controls the `Secure` attribute on the JWT cookie:
    #   None  (default) — auto-detect from the request. When the user
    #                     agent reached us over HTTPS (directly, or via
    #                     a reverse proxy that set X-Forwarded-Proto),
    #                     the cookie is issued Secure. On plain-HTTP
    #                     LAN deployments it is not. This is the right
    #                     default for the documented topologies
    #                     (Tailscale, Caddy, plain LAN) — operators
    #                     behind TLS get hardening for free, plain-HTTP
    #                     operators keep working.
    #   True             — always Secure. Use behind a reverse proxy
    #                     that terminates TLS but uvicorn doesn't see
    #                     it as HTTPS (uncommon — only needed if you've
    #                     disabled proxy-header handling).
    #   False            — never Secure. Only for local dev over plain
    #                     HTTP where the auto-detect would also pick
    #                     False; setting it explicitly silences any
    #                     future warning we add.
    cookie_secure: bool | None = None
    # cookie_samesite — defaults to 'strict' for CSRF defense in depth.
    # The two-step confirm-token flow already mitigates classical CSRF
    # against /api/control/*, but strict closes the door on any future
    # endpoint that forgets the token. UX impact: clicking a link from
    # Slack / email into GenWatch will show the login page (cookie not
    # sent on cross-site nav) — one extra login click, no breakage.
    # Use 'lax' only if you have a workflow that relies on cross-site
    # navigation carrying the session. 'none' requires cookie_secure
    # and is rarely useful for this product.
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"

    @field_validator("cookie_samesite")
    @classmethod
    def _samesite_requires_secure(cls, v: str, info) -> str:
        # SameSite=None requires Secure per the browser spec; rejecting
        # this combo at config-load time turns a silent browser-side
        # "cookie discarded" into a startup error operators can fix.
        if v == "none":
            cs = info.data.get("cookie_secure")
            if cs is False:
                raise ValueError(
                    "auth.cookie_samesite='none' requires auth.cookie_secure=true "
                    "(browser refuses to store SameSite=None cookies without Secure)"
                )
        return v


# Default Content-Security-Policy for the operator console.
#
# `{ws_self}` is substituted at request time with ws://HOST and wss://HOST
# for the request's own Host header, so the live WebSocket works without
# opening connect-src to every host on the internet. If you override
# `security.csp`, include the placeholder (or your own explicit ws origin)
# or the Live view will stop updating.
#
# The two fonts.* origins are there because index.html pulls Geist and
# JetBrains Mono from Google Fonts. Self-host them and you can drop both.
DEFAULT_CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "img-src 'self' data:; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self' {ws_self}"
)


class SecurityConfig(BaseModel):
    """HTTP-layer hardening. Matters most when the console is exposed.

    ``public_exposure`` is the one switch to flip when this server becomes
    reachable from the open internet. It doesn't change behaviour by
    itself so much as it turns advisory checks into refusals: the service
    will not boot with a configuration that is fine on a LAN but reckless
    on a public address (see main.py::_check_public_exposure).
    """

    # Tell GenWatch it is internet-facing. Turns the deployment checklist
    # from warnings into boot-time errors, and defaults require_https on.
    public_exposure: bool = False

    # Emit the hardening response headers (CSP, nosniff, frame-ancestors,
    # Referrer-Policy, Permissions-Policy, COOP/CORP, no-store on /api).
    # Only turn this off if you are debugging a header conflict with a
    # reverse proxy that sets its own.
    headers_enabled: bool = True

    # HSTS is only ever sent on requests we already saw as HTTPS, so it
    # cannot lock out a plain-HTTP LAN deployment.
    hsts_enabled: bool = True
    hsts_max_age: int = 31536000  # 1 year
    # Leave off unless every hostname under this domain is HTTPS — this
    # header is remembered by browsers for a year and is easy to regret.
    hsts_include_subdomains: bool = False

    # Content-Security-Policy string; "" disables the header.
    csp: str = DEFAULT_CSP

    # Refuse plain-HTTP requests. `None` means "follow public_exposure":
    # enforced when the console is declared internet-facing, off on a LAN.
    # The scheme is read from the request, including X-Forwarded-Proto
    # from a trusted proxy (see GENWATCH_TRUSTED_PROXIES).
    require_https: bool | None = None

    # Optional network allowlist, as IPs or CIDRs ("203.0.113.4",
    # "198.51.100.0/24", "2001:db8::/32"). Empty = allow all. Loopback is
    # always allowed so a local admin can never be locked out.
    #
    # CAVEAT: behind a reverse proxy this only works if the proxy sets
    # X-Forwarded-For AND its address is in GENWATCH_TRUSTED_PROXIES —
    # otherwise every request appears to come from the proxy and the
    # allowlist either passes everything or nothing.
    ip_allowlist: list[str] = []


class AtsConfig(BaseModel):
    """ATS-Pi companion device (see docs/integrations/ats-pi-icd.md).

    A second Modbus TCP device on the LAN that physically observes the
    ASCO Series 300 ATS via 18RX module + aux contacts. When healthy,
    its `position` register is the authoritative loadSource for the UI;
    when unreachable, GenWatch falls back to the H-100-derived value.

    Disabled by default — sites without an ATS-Pi see no change. When
    enabled, GenWatch starts a second Modbus client + poller targeting
    the configured host. The two stacks are fully independent: ATS-Pi
    going down does not affect generator monitoring or vice versa.
    """

    enabled: bool = False
    host: str = "192.168.1.250"
    # Default 5020 matches the companion starter's secure-by-default
    # bind (high port → no CAP_NET_BIND_SERVICE / no root on the
    # companion side). Existing deployments with explicit port: 502
    # in their config.yaml are unaffected — only fresh installs see
    # the new default. If your companion is configured for 502, keep
    # `port: 502` in /etc/genwatch/config.yaml.
    port: int = 5020
    # 'socket' = real Modbus/TCP (MBAP framer). The ATS-Pi speaks proper
    # Modbus/TCP, *not* the RTU-over-TCP that the H-100 Lantronix bridge
    # uses. Don't change unless the ATS-Pi side documents otherwise.
    framer: Literal["socket", "rtu"] = "socket"
    slave: int = 1
    timeout_s: float = 1.0
    connect_timeout_s: float = 3.0
    register_file: str = "registers/ats_pi.yaml"
    # When set, GenWatch refuses to mark the ATS poller authoritative
    # unless the device's reported ats_pi_unit_id register matches. Lets
    # the site catch a misconfigured-IP-points-at-wrong-ATS-Pi mistake
    # before bad data influences the operator UI. Left unset (None) the
    # check is skipped — GenWatch then trusts any ATS-Pi at `host`, so the
    # lifespan logs a loud startup warning (see main.py) because a
    # wrong-site cross-wire would otherwise go undetected.
    expected_unit_id: int | None = None


class SlackConfig(BaseModel):
    """Slack alerts via the Web API (chat.postMessage) using a bot token.

    Create a Slack app at https://api.slack.com/apps, add the
    ``chat:write`` scope, install it to your workspace, and invite the
    bot user to the target channel. The token starts with ``xoxb-``.
    """

    enabled: bool = False
    bot_token: str = ""        # xoxb-...
    channel: str = ""          # "#alerts" or channel id "C0123ABCD"
    site_label: str = ""       # overrides site.name in messages

    # Which event types to forward to Slack. All default to True except
    # state-change (chatty — a generator transitions through several
    # states on a normal start) and warning-severity alarms.
    alert_on_alarm: bool = True
    alert_on_warning: bool = True
    alert_on_alarm_cleared: bool = True
    alert_on_state_change: bool = False
    alert_on_command: bool = True
    alert_on_comms_lost: bool = True
    # Utility ↔ generator load-source transitions. Defaults on because
    # an outage / restored-power notification is high-signal — operators
    # want to know immediately, even if engine state change alerts are off.
    #
    # This is the master switch for transfer alerts; the two direction
    # flags below sub-gate it, so turning this off silences both.
    alert_on_load_source_change: bool = True

    # ── Transfer alerts: per-direction control ────────────────────────
    # "The power just went out" and "the power is back" are different
    # messages to different people. Sites that only want the outage page
    # turn the retransfer off; sites running scheduled exercises often do
    # the opposite.
    alert_on_transfer_to_generator: bool = True   # utility → generator
    alert_on_return_to_utility: bool = True       # generator → utility
    # A sustained loss of both the ATS-Pi position and the H-100
    # electrical inference. Rare, and usually means an instrumentation
    # problem rather than a transfer — off by default so it can't drown
    # the two alerts above.
    alert_on_load_source_unknown: bool = False

    # Route transfer alerts to their own channel (e.g. an on-call channel
    # that pages, while routine alarms go to a quieter one). Empty = use
    # `channel`.
    channel_load_source: str = ""

    # Text prepended to the transfer message so Slack actually notifies
    # someone. Slack mention syntax, NOT a plain @name:
    #   "<!channel>"            — everyone in the channel
    #   "<!here>"               — everyone currently online
    #   "<@U024BE7LH>"          — one person, by member ID
    #   "<!subteam^S012ABC3DE>" — a user group
    # Empty = no mention (message still posts).
    mention_on_transfer_to_generator: str = ""
    mention_on_return_to_utility: str = ""

    # Hold a transfer alert for this many seconds and only send it if the
    # load source is still there when the timer fires. An ATS that hunts
    # (or a utility that browns out repeatedly) can otherwise produce a
    # burst of contradictory pages during the exact minutes an operator
    # most needs a clear signal. 0 = send immediately.
    #
    # A transition that lands back on the previously-announced source
    # within the window cancels the alert entirely — nothing changed, so
    # nothing is worth paging about.
    load_source_debounce_s: float = 0.0

    # ── Fuel alerts ───────────────────────────────────────────────────
    # Gated by `fuel.enabled` above all — these flags only decide which of
    # the fuel events reach Slack once monitoring is on. Thresholds,
    # hysteresis and the reminder interval live in the `fuel` section,
    # because they describe the condition rather than the notification.
    alert_on_fuel_warning: bool = True    # crossed the warn threshold
    alert_on_fuel_critical: bool = True   # crossed the critical threshold
    alert_on_fuel_reminder: bool = True   # still low N hours later
    alert_on_fuel_recovered: bool = True  # refuelled
    alert_on_fuel_drop: bool = True       # abnormal drop (leak / theft)
    channel_fuel: str = ""                # blank = use `channel`
    # Only on the critical tier and the drop alert — a "order fuel this
    # week" warning does not deserve an @channel at 2 a.m.
    mention_on_fuel_critical: str = ""

    # ── Security alerts ───────────────────────────────────────────────
    # Sign-in activity on an internet-exposed console. Failures are
    # deduplicated per account per minute so a brute-force attempt can't
    # flood the channel (the lockout alert is the one that matters).
    alert_on_login_failure: bool = True
    alert_on_account_lockout: bool = True
    alert_on_login_success: bool = False   # chatty at most sites
    # Account created / deleted / role changed / password reset / 2FA
    # changed — the audit trail an operator wants to see in real time.
    alert_on_user_change: bool = True
    # Route security alerts somewhere other than `channel` (an admin-only
    # channel, typically — these messages name accounts and source IPs).
    channel_security: str = ""


class FuelConfig(BaseModel):
    """Low-fuel warnings and abnormal-drop detection.

    Disabled by default, deliberately. ``fuel_level_pct`` (0x0092) is not
    verified on every H-100 revision — its neighbour ``coolant_level``
    carries a standing note that the panel's engineering unit may not be a
    true percent, and unconfigured channels on this controller read
    0xFFFF. Confirm the reading against the panel with ``genwatch panel``
    before turning this on; a false "FUEL EMPTY" page is how a site learns
    to ignore its alerts.

    Automatically inert at gaseous-fuel sites (``site.fuel_type`` in the
    register map), which have no local tank to run out of.

    See services/fuel.py for why this doesn't ride on the existing
    ``warn_range`` / ``alarm_range`` numeric-alarm mechanism — the short
    version is that those only evaluate while the engine is *running*,
    and low fuel matters most while it is stopped.
    """

    enabled: bool = False

    # Two tiers, because they mean different things to whoever gets the
    # message: "order fuel this week" versus "you have hours of runtime".
    warn_pct: float = 25.0
    critical_pct: float = 10.0

    # A tank on a running genset sloshes. Rising past a threshold has to
    # clear it by this much before we'll call the situation improved, so a
    # sender oscillating on the line warns once instead of flapping.
    hysteresis_pct: float = 3.0

    # Repeat while the tank is still low. Low fuel is a condition somebody
    # has to act on, not a one-shot event — silence after the first
    # message reads as "handled". 0 disables the reminder.
    renotify_hours: float = 12.0

    # Readings outside this band are treated as a SENSOR FAULT and
    # suppress alerting, rather than being read as an empty tank.
    min_valid_pct: float = 0.0
    max_valid_pct: float = 100.0

    # ── Abnormal-drop detection (leak / theft) ────────────────────────
    # Alert when the level falls this many points inside
    # drop_window_minutes. 0 disables. Off by default because the right
    # threshold depends on tank size and site behaviour.
    drop_alert_pct: float = 0.0
    drop_window_minutes: int = 60
    # Only evaluate while the engine is NOT burning fuel. A loaded
    # generator drawing the tank down is expected and would alert on every
    # outage; the same fall with the engine stopped is a leak or a siphon.
    drop_only_when_stopped: bool = True


class MqttConfig(BaseModel):
    """MQTT publishing for the generator's load source.

    When enabled, GenWatch publishes a small retained message to an MQTT
    broker every time the load moves between utility and generator:

      - load transfers to the generator → publish ``payload_on``  ("ON")
      - load returns to utility          → publish ``payload_off`` ("OFF")

    to ``topic`` (default ``facility/generator/status``). The message is
    published *retained* so a home-automation / SCADA subscriber that
    connects later immediately learns the current state without waiting
    for the next transition.

    The signal is the operator-visible *load source* — the same
    utility↔generator transition that drives the Slack load-source alert
    and the UI's ON UTILITY / ON GENERATOR indicator. It is driven by the
    ATS-Pi's physical position when that companion is authoritative, and
    by the H-100 electrical inference otherwise, so it works with or
    without the ATS-Pi.

    Disabled by default — sites without a broker see no change. The
    publisher is fully independent and best-effort: a slow or unreachable
    broker never blocks the Modbus poller, the API, or Slack.

    Implemented against a broker with no new Python dependency (raw
    MQTT 3.1.1 over a socket, mirroring the dependency-free Slack
    notifier), so the hash-pinned offline install is unaffected.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883
    # The status topic. "ON"/"OFF" are published here on load-source change.
    topic: str = "facility/generator/status"
    payload_on: str = "ON"       # published when the generator takes the load
    payload_off: str = "OFF"     # published when the load returns to utility
    # QoS 0 (at most once) or 1 (at least once, broker sends PUBACK). QoS 1
    # is the default: a status transition is worth a delivery confirmation.
    qos: Literal[0, 1] = 1
    # Retain the last status on the broker so late subscribers see current
    # state on connect. Correct default for a status topic.
    retain: bool = True
    # Optional broker auth. Empty username → anonymous connect.
    username: str = ""
    password: str = ""
    # MQTT client id. Empty → a stable id derived from the site name at
    # runtime (a fixed id lets the broker's session/ACLs track this client).
    client_id: str = ""
    # Wrap the connection in TLS (broker on 8883, typically). Uses the
    # system trust store; set tls_insecure to skip cert verification only
    # for a self-signed broker on a trusted LAN.
    tls: bool = False
    tls_insecure: bool = False
    # Per-connect socket timeout (connect + each packet exchange).
    timeout_s: float = 10.0
    # Publish the boot-time load source too. Off by default so a restart
    # while on utility doesn't emit a spurious "OFF"; the first real
    # transition still publishes. Turn on if you want the retained topic
    # seeded on every service start.
    publish_on_start: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GENWATCH_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority, highest first: explicit init kwargs > env > config.yaml
        # > file secrets/defaults. This is what the module docstring
        # promises; the YAML source is injected here rather than as
        # constructor kwargs so it can't outrank env.
        return (
            init_settings,
            env_settings,
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    # paths
    data_dir: str = "/var/lib/genwatch"
    config_path: str = ""  # set by load()

    # mock mode: no real serial, synthesised telemetry. Default on if no
    # /dev/ttyUSB0 exists so the service still boots for development.
    mock: bool = False

    # Which Modbus link to use:
    #   "tcp"    — Modbus-RTU over a network serial bridge (Lantronix etc.); uses modbus_tcp.
    #   "serial" — direct USB-to-serial cable on this host; uses serial.
    transport: Literal["serial", "tcp"] = "tcp"

    serial: SerialConfig = Field(default_factory=SerialConfig)
    modbus_tcp: ModbusTcpConfig = Field(default_factory=ModbusTcpConfig)
    modbus: ModbusConfig = Field(default_factory=ModbusConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    fuel: FuelConfig = Field(default_factory=FuelConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    ats: AtsConfig = Field(default_factory=AtsConfig)

    # WebSocket push cadence — kept at prime poll by default per design
    ws_push_ms: int = 1500

    # CORS — only for development; production serves static UI from same origin
    cors_origins: list[str] = []

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "db.sqlite"

    @property
    def https_required(self) -> bool:
        """Whether plain-HTTP requests are refused.

        Explicit ``security.require_https`` wins; otherwise it follows
        ``security.public_exposure`` so declaring the console
        internet-facing turns on TLS enforcement without a second knob.
        """
        if self.security.require_https is not None:
            return self.security.require_https
        return self.security.public_exposure

    @property
    def register_file_path(self) -> Path:
        """Absolute path to the register YAML.

        If modbus.register_file is relative, resolve against the
        installed package's registers/ directory first, then the cwd.
        """
        p = Path(self.modbus.register_file)
        if p.is_absolute():
            return p
        pkg_local = Path(__file__).parent / p
        return pkg_local if pkg_local.exists() else p


def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return data


def load(config_path: str | None = None) -> Settings:
    """Load settings: defaults -> YAML -> env. Env wins."""
    # 1. Find config.yaml
    candidates = [config_path] if config_path else DEFAULT_CONFIG_PATHS
    yaml_data: dict = {}
    chosen = ""
    for c in candidates:
        if not c:
            continue
        if Path(c).exists():
            yaml_data = _load_yaml(c)
            chosen = c
            break

    # 2. Build Settings. The YAML is fed through a dedicated low-priority
    #    source (see _YamlSettingsSource) so environment variables still
    #    win — passing it as kwargs here would invert that.
    token = _yaml_ctx.set(yaml_data)
    try:
        s = Settings(config_path=chosen)
    finally:
        _yaml_ctx.reset(token)

    # 3. Ensure data dir exists. If we can't create it (read-only fs in
    #    test), fall back to a tempdir under the cwd.
    try:
        Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        fallback = Path(os.getcwd()) / "var-genwatch"
        fallback.mkdir(parents=True, exist_ok=True)
        s = s.model_copy(update={"data_dir": str(fallback)})

    return s
