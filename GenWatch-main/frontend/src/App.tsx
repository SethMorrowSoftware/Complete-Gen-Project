// Top-level app: auth gate, topbar + nav + footer, view switching.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, setUnauthorizedHandler } from "./api/client";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { BrandMark, Icon, IconButton } from "./components/primitives";
import { useLiveData } from "./hooks/useLiveData";
import type { MeBody } from "./types";
import { ChangePasswordView } from "./views/ChangePasswordView";
import { EventsView } from "./views/EventsView";
import { HistoryView } from "./views/HistoryView";
import { LiveView } from "./views/LiveView";
import { LoginView } from "./views/LoginView";
import { SettingsView } from "./views/SettingsView";

type View = "live" | "history" | "events" | "settings";
type Theme = "dark" | "light";

const THEME_KEY = "genwatch.theme";

// Most frequently a present operator will refresh their session. Well
// under any sane `auth.idle_timeout_minutes` (default 60), and rare
// enough that continuous mouse movement costs one request every 5 min.
const KEEPALIVE_MS = 5 * 60 * 1000;

function readTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === "light" || v === "dark") return v;
  } catch { /* ignore */ }
  return "dark";
}

function writeTheme(t: Theme) {
  try { localStorage.setItem(THEME_KEY, t); } catch { /* ignore */ }
}

function applyTheme(t: Theme, animate: boolean) {
  const root = document.documentElement;
  if (animate) {
    root.setAttribute("data-theme-switching", "1");
    window.setTimeout(() => root.removeAttribute("data-theme-switching"), 280);
  }
  if (t === "light") root.setAttribute("data-theme", "light");
  else root.removeAttribute("data-theme");
}

export function App() {
  const [auth, setAuth] = useState<MeBody | null>(null);
  const [view, setView] = useState<View>("live");
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    api.me().then(setAuth).catch(() => setAuth({ authenticated: false }));
  }, []);

  // A mid-session 401 (token expiry / secret rotation) from any request
  // forces the UI back to the login screen instead of leaving a stale,
  // live-looking dashboard. Unmounting Shell also tears down the WS loop.
  useEffect(() => {
    setUnauthorizedHandler(() => setAuth({ authenticated: false }));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => { applyTheme(theme, false); }, []);

  // Idle-timeout keepalive. The server expires a session that hasn't been
  // *used* for `auth.idle_timeout_minutes`, and deliberately does not
  // count the live WebSocket as use — otherwise the Live view, the one
  // page people leave open on an unattended screen, would keep a session
  // alive forever. So an operator who is actually present (moving,
  // typing, switching tabs back) refreshes it here, throttled to one
  // request per KEEPALIVE_MS.
  //
  // It doubles as an expiry detector: /api/auth/me answers 200 with
  // authenticated:false rather than 401, so without this a lapsed session
  // would leave a frozen, live-looking dashboard until the operator
  // clicked something.
  useEffect(() => {
    if (!auth?.authenticated) return;
    let last = 0;
    let cancelled = false;
    const ping = () => {
      const now = Date.now();
      if (now - last < KEEPALIVE_MS) return;
      last = now;
      api.me()
        .then((m) => { if (!cancelled && !m.authenticated) setAuth({ authenticated: false }); })
        .catch(() => { /* transient network error — the next event retries */ });
    };
    const onVisible = () => { if (document.visibilityState === "visible") ping(); };
    const events: Array<keyof DocumentEventMap> = ["pointerdown", "keydown", "wheel"];
    events.forEach((e) => document.addEventListener(e, ping, { passive: true }));
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      events.forEach((e) => document.removeEventListener(e, ping));
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [auth?.authenticated]);

  const toggleTheme = () => {
    setTheme((t) => {
      const next: Theme = t === "dark" ? "light" : "dark";
      applyTheme(next, true);
      writeTheme(next);
      return next;
    });
  };

  if (!auth) {
    return (
      <div className="boot-screen">
        <BrandMark size={48} />
        <div className="boot-spinner" />
      </div>
    );
  }
  if (!auth.authenticated) {
    return <LoginView onLoggedIn={() => api.me().then(setAuth)} />;
  }
  if (auth.mustChangePassword) {
    // The server blocks every other endpoint for this account until the
    // temporary password is replaced, so showing the console here would
    // just render a wall of 403s.
    return (
      <ChangePasswordView
        operator={auth.operator ?? "operator"}
        onDone={() => api.me().then(setAuth)}
        onCancel={async () => { await api.logout(); setAuth({ authenticated: false }); }}
      />
    );
  }
  return (
    <Shell
      auth={auth}
      view={view}
      setView={setView}
      theme={theme}
      onToggleTheme={toggleTheme}
      // Settings can change the signed-in account (password, 2FA), so it
      // needs a way to refresh the identity the shell is rendering from.
      onAuthChanged={() => { void api.me().then(setAuth); }}
      onLogout={async () => { await api.logout(); setAuth({ authenticated: false }); }}
    />
  );
}

function Shell({ auth, view, setView, theme, onToggleTheme, onAuthChanged, onLogout }: {
  auth: MeBody;
  view: View;
  setView: (v: View) => void;
  theme: Theme;
  onToggleTheme: () => void;
  onAuthChanged: () => void;
  onLogout: () => Promise<void>;
}) {
  const live = useLiveData();
  const [clock, setClock] = useState(new Date());
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const navItems: Array<{ id: View; label: string; icon: any }> = [
    { id: "live", label: "Live", icon: "activity" },
    { id: "history", label: "History", icon: "history" },
    { id: "events", label: "Events", icon: "bell" },
    { id: "settings", label: "Settings", icon: "settings" },
  ];

  const status = live.status;
  const comms = status?.comms;
  const panel = status?.panel;
  const activeAlarmCount = status?.activeAlarms.length ?? 0;
  const dateStr = clock.toLocaleString("en-US", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hour12: false,
  });

  // Bump timeInState locally each second between WS pushes for smooth UI.
  // We anchor on a monotonic wall-clock instant captured when the status
  // was received, so a client/server NTP gap can't make the value go
  // backward.
  const seenAt = useRef<{ ts: number; receivedMs: number } | null>(null);
  if (status && (!seenAt.current || seenAt.current.ts !== status.serverTs)) {
    seenAt.current = { ts: status.serverTs, receivedMs: Date.now() };
  }
  const tickedStatus = useMemo(() => {
    if (!status || !seenAt.current) return null;
    const elapsedS = Math.max(0, Math.floor((Date.now() - seenAt.current.receivedMs) / 1000));
    return { ...status, timeInState: status.timeInState + elapsedS };
    // `clock` is the heartbeat that drives this recomputation each second.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, clock]);

  const commsLabel = comms ? `${comms.successPct.toFixed(1)}%` : "—";
  const commsRate = comms ? `${(comms.rateMs / 1000).toFixed(1)}s` : "—";

  // Stale-data detection: if the WebSocket is down OR we haven't
  // received a push within ~3 poll intervals, the numbers on screen are
  // no longer "live" and the operator needs to know. The `clock` 1 Hz
  // tick recomputes this so the badge appears even if no other state
  // changes. Threshold floors at 6s so a brief network hiccup on a slow
  // poll cadence doesn't flash the warning.
  const staleThresholdMs = Math.max(6000, (comms?.rateMs ?? 1500) * 3);
  const sinceLastPushMs = live.lastPushAt == null ? Infinity : Date.now() - live.lastPushAt;
  // A known-dead Modbus link is stale even if the WebSocket is happily
  // delivering snapshots (the backend keeps pushing with comms.state
  // "lost"). Folding it in here forces the STALE badge and the
  // control-button gate the moment the H-100 link drops, rather than
  // waiting out the no-push timer — which the keepalive `ping`s would
  // otherwise keep resetting.
  const commsLost = comms?.state === "lost";
  const stale = (live.wsDown || sinceLastPushMs > staleThresholdMs || commsLost) && !!status;
  // Panel-mode freshness gate (defense in depth against a backend
  // mismatch that drops `panel` from snapshots while keeping the WS
  // alive). The control buttons consume this — when the panel block
  // hasn't been refreshed inside the staleness window, treat the
  // panel as unknown and block remote commands, even if other parts
  // of the snapshot keep flowing.
  const sincePanelMs = live.panelLastSeenAt == null ? Infinity : Date.now() - live.panelLastSeenAt;
  const panelStale = sincePanelMs > staleThresholdMs && !!status;
  // Reference `clock` so the memoizer recomputes once per second.
  void clock;

  const navTabs = (
    <nav className="nav" role="tablist" aria-label="Primary">
      {navItems.map((n) => (
        <button key={n.id}
                aria-current={view === n.id ? "page" : undefined}
                onClick={() => setView(n.id)}
                role="tab">
          <Icon name={n.icon} size={13} stroke={1.8} />
          <span className="lbl-text">{n.label}</span>
          {n.id === "events" && activeAlarmCount > 0 && (
            <span className="nav-badge" aria-label={`${activeAlarmCount} active alarms`}>
              {activeAlarmCount}
            </span>
          )}
        </button>
      ))}
    </nav>
  );

  return (
    <div className="app">
      <header className="topbar" data-scrolled={scrolled ? "1" : "0"}>
        <div className="brand">
          <BrandMark />
          <div className="brand-name">
            <span className="brand-title">Castle</span>
            <span className="brand-sub">Generator Monitor</span>
          </div>
        </div>
        <div className="nav-desktop-slot">{navTabs}</div>
        <div className="topbar-right">
          {stale && (
            <div
              className="comms-badge"
              data-state="lost"
              title={
                live.wsDown
                  ? "Live connection to server is down — values shown are last known."
                  : commsLost
                  ? "Modbus link to the H-100 is LOST — values shown are last known. Remote commands are blocked."
                  : `No live updates for ${Math.round(sinceLastPushMs / 1000)}s — values shown may be stale.`
              }
              style={{ borderColor: "var(--red)" }}
            >
              <span className="pulse" />
              <span>STALE DATA</span>
            </div>
          )}
          {panel && <PanelChip mode={panel.mode} />}
          <div className="comms-badge" data-state={comms?.state ?? "lost"}
               title={`Modbus comms: ${comms?.state ?? "lost"} · ${commsLabel} success · ${commsRate} poll`}>
            <span className="pulse" />
            <span>Comms · {commsLabel}</span>
            <span className="mono">{commsRate}</span>
          </div>
          <span className="clock" title={clock.toString()}>{dateStr}</span>
          <IconButton
            icon={theme === "dark" ? "sun" : "moon"}
            onClick={onToggleTheme}
            title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            variant="ghost"
          />
          <button className="user-chip" onClick={onLogout} title="Sign out" aria-label="Sign out">
            <span className="avatar">{(auth.operator ?? "??").slice(0, 2).toUpperCase()}</span>
            <span>{auth.operator ?? "operator"}</span>
            <span className="role">{auth.role ?? "viewer"}</span>
          </button>
        </div>
      </header>

      <div className="nav-mobile-slot">{navTabs}</div>

      <main className="main">
        {live.loading && !tickedStatus && (
          <div className="connecting">
            <div className="connecting-spinner" />
            <span>Connecting to controller…</span>
          </div>
        )}
        {live.error && (
          <div className="alarm-strip" style={{ marginBottom: 14 }}>
            <span className="led" />
            <strong>Connection error</strong>
            <span>{live.error}</span>
          </div>
        )}
        {tickedStatus && (
          <div className="view" key={view}>
            {/* Per-view boundary: a render error costs you that view, not
                the whole console. The topbar keeps showing comms health —
                which is the thing you actually need during an outage —
                and the other tabs still work. Keyed on `view` so
                switching tabs clears a previous crash. */}
            <ErrorBoundary key={view} label={view}>
              {view === "live" && <LiveView status={tickedStatus} history={live.history} operator={auth.operator ?? "operator"} role={auth.role ?? "viewer"} stale={stale} panelStale={panelStale} />}
              {view === "history" && <HistoryView />}
              {view === "events" && <EventsView />}
              {view === "settings" && (
                <SettingsView auth={auth} onAuthChanged={onAuthChanged} />
              )}
            </ErrorBoundary>
          </div>
        )}
      </main>

      <footer className="foot">
        <span>
          Castle Generator Monitor <span className="foot-ver">v0.1</span>
          {tickedStatus && (
            <>
              <span className="foot-sep" />
              {tickedStatus.site.id} · {tickedStatus.site.name}
            </>
          )}
        </span>
        <span className="mono">
          {comms?.state === "lost"
            ? "comms lost"
            : `poll ${(comms?.rateMs ?? 1500) / 1000}s`}
        </span>
      </footer>
    </div>
  );
}

// Topbar chip showing the H-100 front-panel key-switch position.
// Operator commands from this UI only engage the controller when the
// panel is in AUTO; MANUAL/OFF means remote writes are ignored locally
// at the unit. Decoded from input_status_1 bits via panel_mode_bits in
// the YAML — if the bits don't match a known mode the chip shows
// "PANEL: ?" so the operator knows the YAML needs verifying.
function PanelChip({ mode }: { mode: "auto" | "manual" | "off" | "unknown" }) {
  const label = mode === "unknown" ? "?" : mode.toUpperCase();
  // Reuse the comms-badge CSS scheme: healthy (green) / degraded (amber) /
  // lost (red). auto → healthy, manual → degraded, off/unknown → lost.
  const state =
    mode === "auto"   ? "healthy" :
    mode === "manual" ? "degraded" :
    "lost";
  const title =
    mode === "auto"
      ? "H-100 panel key switch in AUTO — remote commands will engage."
      : mode === "manual"
      ? "H-100 panel key switch in MANUAL — remote start/stop/exercise are ignored at the controller. Set to AUTO at the unit."
      : mode === "off"
      ? "H-100 panel key switch in OFF — engine is locked out. Remote commands will not run."
      : "Panel key-switch position not recognized — verify panel_mode_bits in h100.yaml against your firmware.";
  return (
    <div className="comms-badge" data-state={state} title={title}>
      <span className="pulse" />
      <span>Panel · {label}</span>
    </div>
  );
}
