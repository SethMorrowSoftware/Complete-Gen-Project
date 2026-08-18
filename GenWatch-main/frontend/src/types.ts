// API types — mirror the FastAPI responses in backend/genwatch/api/*.

export type EngineState =
  | "stopped"
  | "cranking"
  | "running"
  | "exercising"
  | "cooling"
  | "alarm"
  | "unknown";

// Which source is currently supplying the load. Determined by either
//   1. The ATS-Pi companion device's direct position contacts, when
//      configured and healthy (docs/integrations/ats-pi-icd.md §10), or
//   2. GenWatch's H-100-electrical inference (services/state.py) as
//      the fallback when the ATS-Pi is absent or unreachable.
// 'transferring' is only produced by the ATS-Pi path — it represents
// the sub-second window during a load transfer when the switch
// contacts have opened from one source but not yet closed on the other.
export type LoadSource = "utility" | "generator" | "transferring" | "unknown";

export type CommsState = "healthy" | "degraded" | "lost";

export type Severity = "ok" | "info" | "warn" | "alarm";

export type Role = "viewer" | "operator" | "admin";

export interface Reading {
  rpm: number | null;
  hz: number | null;
  kw: number | null;
  pf: number | null;
  oilP: number | null;
  oilT: number | null;
  coolT: number | null;
  coolLevel: number | null;
  throttle: number | null;
  o2: number | null;
  batt: number | null;
  battA: number | null;
  vAB: number | null;
  vBC: number | null;
  vCA: number | null;
  iA: number | null;
  iB: number | null;
  iC: number | null;
  fuelPct: number | null;
  runHours: number | null;
  startCount: number | null;
}

export interface CommsHealth {
  state: CommsState;
  successPct: number;
  lastGoodAt: number | null;
  rateMs: number;
  p95LatencyMs: number;
}

export interface ActiveAlarm {
  code: string;
  desc: string;
  severity: Severity;
  raised_at: number;
  raw: number;
}

export interface PanelBlock {
  mode: "auto" | "manual" | "off" | "unknown";
  keySwitchRaw: number | null;
  engineStatusCode: number | null;
  activeAlarmCountHw: number | null;
  quietTestStatusRaw: number | null;
}

// ATS-Pi companion device snapshot — present on every /api/status
// response (as either {enabled: false} or the full block). See
// docs/integrations/ats-pi-icd.md for field semantics.
export type AtsPosition = "utility" | "generator" | "transferring" | "unknown";
export type AtsMode = "auto" | "manual" | "test" | "unknown";

export interface AtsCommsHealth {
  state: CommsState;
  successPct: number;
}

// Discriminated union: when disabled, only `enabled` is present.
// When enabled, the full snapshot is delivered. The hook handles
// both shapes safely.
export type AtsBlock =
  | { enabled: false }
  | {
      enabled: true;
      position: AtsPosition;
      normalAvailable: boolean | null;
      emergencyAvailable: boolean | null;
      engineStartCalling: boolean | null;
      atsMode: AtsMode;
      faultCodes: string[];
      lastTransferToGenTs: number | null;
      lastRetransferToUtilTs: number | null;
      transferCount24h: number;
      transferCountLifetime: number;
      // Identification — present only on REST seed response (omitted
      // from WS pushes to keep frequent payloads small). Hook merges
      // them in when present.
      icdVersion?: [number, number];
      atsPiFw?: [number, number, number];
      atsPiUnitId?: number;
      atsPiUptimeS?: number;
      cmdTestActive: boolean;
      cmdInhibitActive: boolean;
      cmdForceTransferActive: boolean;
      cmdBypassDelayActive: boolean;
      comms: AtsCommsHealth;
      // True iff the ATS-Pi's position is currently driving the
      // operator-visible loadSource (vs the H-100 fallback derivation).
      // See ICD §10.
      authoritative: boolean;
    };

export interface StatusBody {
  state: EngineState;
  alarmRaw: number;
  timeInState: number;
  stateStartedAt: number;
  // Derived: 'utility' | 'generator' | 'transferring' | 'unknown'.
  // Driven by ATS-Pi when authoritative, falls back to H-100 telemetry.
  loadSource: LoadSource;
  loadSourceStartedAt: number;
  timeInLoadSource: number;
  comms: CommsHealth;
  reading: Reading;
  site: {
    id: string;
    name: string;
    ratingKw: number;
    tankGal: number;
    // 'diesel' | 'gaseous' | 'unknown' — drives UI gating (hide O₂ on
    // diesel, etc.). Optional for forward-compat with older backends.
    fuelType?: "diesel" | "gaseous" | "unknown";
  };
  exercise: {
    // Configured — declared in registers/h100.yaml at commissioning.
    // null when the YAML declares no schedule, or declares one the
    // backend rejected (bad weekday, non-HH:MM time). There is
    // deliberately no fallback: a missing schedule must not render as a
    // confident guess. See backend SiteConfig.exercise_day.
    enabled: boolean;
    day: string | null;
    time: string | null;
    durationMin: number;
    // Observed — what the controller has actually been doing, inferred
    // from its own "Internal Exercise Active" bit (backend
    // services/exercise.py). null when there isn't enough evidence yet;
    // absent entirely on backends predating the field. `day`/`time` use
    // the same vocabulary as the configured fields above so the two can
    // be compared directly.
    observed?: {
      day: string;
      time: string;
      samples: number;
      windowDays: number;
      lastStartTs: number;
    } | null;
  };
  activeAlarms: ActiveAlarm[];
  hts: {
    transferredToGen: boolean;
    lastTransferTs: number | null;
    transfers30d: number;
  };
  lastAlarm: {
    ts: number;
    severity: Severity;
    message: string;
  } | null;
  panel: PanelBlock;
  // ATS-Pi companion. Always present on /api/status responses (the
  // backend emits at least {enabled: false}); typed as required.
  ats: AtsBlock;
  serverTs: number;
}

export interface EventRow {
  id: number;
  ts: number;
  severity: Severity;
  type: string;
  message: string;
  meta: string | null;
}

export interface ConfirmToken {
  token: string;
  issuedAt: number;
  expiresAt: number;
}

export type LiveMessage =
  | { type: "hello"; state: EngineState; comms: Partial<CommsHealth>; serverTs: number }
  | { type: "ping" }
  | {
      type: "snapshot";
      ts: number;
      state: EngineState;
      timeInState: number;
      alarmRaw: number;
      comms: CommsHealth;
      reading: Reading;
      // Optional for forward-compat with older backends — present from
      // v0.1.1 onwards. Used to gate the control buttons on the
      // H-100 front-panel key switch being in AUTO.
      panel?: PanelBlock;
      // Optional for forward-compat — present once the load-source
      // derivation lands server-side. The hook falls back to the
      // seeded value when the field is absent.
      loadSource?: LoadSource;
      timeInLoadSource?: number;
      // ATS-Pi block — null when ats.enabled=false on the backend,
      // populated otherwise. Same shape as REST /api/status.
      ats?: AtsBlock | null;
    }
  | { type: "transition"; from: EngineState; to: EngineState; ts: number }
  | { type: "load-source"; from: LoadSource; to: LoadSource; ts: number }
  | { type: "alarm"; code: string; desc: string; severity: Severity; ts: number }
  | { type: "alarm-cleared"; code: string; ts: number }
  // ATS-Pi events emitted by services/ats.py — drive immediate UI
  // updates without waiting for the next snapshot push.
  | { type: "ats-position"; from: AtsPosition; to: AtsPosition; ts: number }
  | { type: "ats-source"; source: "normal" | "emergency"; available: boolean; code: string; ts: number }
  | { type: "ats-mode"; from: AtsMode; to: AtsMode; ts: number }
  | { type: "ats-comms"; from: CommsState; to: CommsState; successPct: number; ts: number }
  | { type: "ats-reboot"; prev_uptime_s: number; new_uptime_s: number; ts: number }
  | { type: "event"; sev: Severity; eventType: string; msg: string; meta: string; ts: number };

export interface MeBody {
  authenticated: boolean;
  operator?: string;
  role?: Role;
  // Set when an admin issued a temporary password — the console stays
  // locked to the change-password screen until it's replaced.
  mustChangePassword?: boolean;
  totpEnabled?: boolean;
  totpRequired?: boolean;
  recoveryCodesRemaining?: number;
}

// One row of GET /api/users (admin only). Never carries the password
// hash or the TOTP secret — the API doesn't return them at all.
export interface UserRow {
  username: string;
  role: Role;
  disabled: boolean;
  mustChangePassword: boolean;
  totpEnabled: boolean;
  lockedUntil: number;
  failedAttempts: number;
  lastLoginAt: number | null;
  lastLoginIp: string;
  passwordChangedAt: number;
  createdAt: number;
  createdBy: string;
  activeSessions: number;
  recoveryCodesRemaining: number;
}

export interface SessionRow {
  id: string;
  current: boolean;
  createdAt: number;
  lastSeenAt: number;
  expiresAt: number;
  ip: string;
  userAgent: string;
  // "Keep me signed in" session: long-lived, no idle timeout.
  remember: boolean;
}

// Returned by GET /api/config.slack — the bot token itself is never
// exposed; only a flag confirming it is set on disk.
export interface SlackConfigView {
  enabled: boolean;
  channel: string;
  siteLabel: string;
  botTokenConfigured: boolean;
  alertOnAlarm: boolean;
  alertOnWarning: boolean;
  alertOnAlarmCleared: boolean;
  alertOnStateChange: boolean;
  alertOnCommand: boolean;
  alertOnCommsLost: boolean;
  alertOnLoadSourceChange: boolean;
  // Transfer alerts — per-direction gating, routing, mentions, debounce.
  alertOnTransferToGenerator: boolean;
  alertOnReturnToUtility: boolean;
  alertOnLoadSourceUnknown: boolean;
  channelLoadSource: string;
  mentionOnTransferToGenerator: string;
  mentionOnReturnToUtility: string;
  loadSourceDebounceS: number;
  // Sign-in / account security alerts.
  // Fuel alerts — thresholds live in FuelConfigView, these only decide
  // which fuel events reach Slack.
  alertOnFuelWarning: boolean;
  alertOnFuelCritical: boolean;
  alertOnFuelReminder: boolean;
  alertOnFuelRecovered: boolean;
  alertOnFuelDrop: boolean;
  channelFuel: string;
  mentionOnFuelCritical: string;
  alertOnLoginFailure: boolean;
  alertOnAccountLockout: boolean;
  alertOnLoginSuccess: boolean;
  alertOnUserChange: boolean;
  channelSecurity: string;
}

// Sent in PUT /api/config.slack — omit a field to leave it unchanged.
// Set bot_token to "" to explicitly clear it.
export interface SlackUpdate {
  enabled?: boolean;
  bot_token?: string;
  channel?: string;
  site_label?: string;
  alert_on_alarm?: boolean;
  alert_on_warning?: boolean;
  alert_on_alarm_cleared?: boolean;
  alert_on_state_change?: boolean;
  alert_on_command?: boolean;
  alert_on_comms_lost?: boolean;
  alert_on_load_source_change?: boolean;
  alert_on_transfer_to_generator?: boolean;
  alert_on_return_to_utility?: boolean;
  alert_on_load_source_unknown?: boolean;
  channel_load_source?: string;
  mention_on_transfer_to_generator?: string;
  mention_on_return_to_utility?: string;
  load_source_debounce_s?: number;
  alert_on_fuel_warning?: boolean;
  alert_on_fuel_critical?: boolean;
  alert_on_fuel_reminder?: boolean;
  alert_on_fuel_recovered?: boolean;
  alert_on_fuel_drop?: boolean;
  channel_fuel?: string;
  mention_on_fuel_critical?: string;
  alert_on_login_failure?: boolean;
  alert_on_account_lockout?: boolean;
  alert_on_login_success?: boolean;
  alert_on_user_change?: boolean;
  channel_security?: string;
}

// Returned by GET /api/config.mqtt — the broker password is never
// exposed; only a flag confirming it is set on disk.
export interface MqttConfigView {
  enabled: boolean;
  host: string;
  port: number;
  topic: string;
  payloadOn: string;
  payloadOff: string;
  qos: number;
  retain: boolean;
  username: string;
  passwordConfigured: boolean;
  clientId: string;
  tls: boolean;
  tlsInsecure: boolean;
  publishOnStart: boolean;
}

// Sent in PUT /api/config.mqtt — omit a field to leave it unchanged.
// Set password to "" to explicitly clear it.
export interface MqttUpdate {
  enabled?: boolean;
  host?: string;
  port?: number;
  topic?: string;
  payload_on?: string;
  payload_off?: string;
  qos?: number;
  retain?: boolean;
  username?: string;
  password?: string;
  client_id?: string;
  tls?: boolean;
  tls_insecure?: boolean;
  publish_on_start?: boolean;
}

// Returned by GET /api/config.fuel. tankGal / fuelType come from the
// register map rather than config.yaml — the UI needs them to show
// gallons and to explain why a gaseous site never alerts.
export interface FuelConfigView {
  enabled: boolean;
  warn_pct: number;
  critical_pct: number;
  hysteresis_pct: number;
  renotify_hours: number;
  min_valid_pct: number;
  max_valid_pct: number;
  drop_alert_pct: number;
  drop_window_minutes: number;
  drop_only_when_stopped: boolean;
  tankGal: number;
  fuelType: "diesel" | "gaseous" | "unknown";
}

// Sent in PUT /api/config.fuel — omit a field to leave it unchanged.
export interface FuelUpdate {
  enabled?: boolean;
  warn_pct?: number;
  critical_pct?: number;
  hysteresis_pct?: number;
  renotify_hours?: number;
  min_valid_pct?: number;
  max_valid_pct?: number;
  drop_alert_pct?: number;
  drop_window_minutes?: number;
  drop_only_when_stopped?: boolean;
}
