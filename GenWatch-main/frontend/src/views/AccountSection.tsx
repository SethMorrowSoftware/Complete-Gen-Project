// Settings → My account. Self-service password, two-factor and sessions.
//
// The 2FA flow is deliberately three steps — generate, scan, confirm with a
// live code — because activating on generation would lock out anyone whose
// QR scan silently failed. Recovery codes are shown exactly once; the
// server only keeps hashes, so there is no "show them again" to build.

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { Icon, Modal, Pill } from "../components/primitives";
import type { MeBody, SessionRow } from "../types";

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: { message?: string } | string })?.detail;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
  }
  return (e as Error)?.message ?? fallback;
}

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export function AccountSection({ auth, onAuthChanged }: {
  auth: MeBody;
  onAuthChanged: () => void;
}) {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [changingPw, setChangingPw] = useState(false);
  const [enrolling, setEnrolling] = useState(false);
  const [disabling, setDisabling] = useState(false);

  const reload = async () => {
    try {
      setSessions((await api.sessions()).sessions);
    } catch (e) {
      setError(errText(e, "could not load sessions"));
    }
  };
  useEffect(() => { void reload(); }, []);

  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>My account</h2>
        <p>
          Signed in as <span className="mono">{auth.operator}</span> ({auth.role}).
          Changing your password or enabling two-factor here affects only this account.
        </p>
      </div>

      {(error || notice) && (
        <div className="field-row">
          <div className="lbl">{error ? "Error" : "Done"}</div>
          <Pill tone={error ? "alarm" : "ok"}>{error ?? notice}</Pill>
        </div>
      )}

      <div className="field-row">
        <div className="lbl">
          Password
          <span className="desc">Changing it signs out every other session for this account</span>
        </div>
        <button className="btn" onClick={() => setChangingPw(true)}>
          <Icon name="lock" size={14} /> Change password
        </button>
      </div>

      <div className="field-row">
        <div className="lbl">
          Two-factor authentication
          <span className="desc">
            A 6-digit code from an authenticator app, on top of your password. Strongly
            recommended when this console is reachable from the internet — it is the single
            control that survives a leaked or reused password.
            {auth.totpRequired && " Required by policy on this server."}
          </span>
        </div>
        <div className="flex ai-c gap-8">
          {auth.totpEnabled ? (
            <>
              <Pill tone="ok">Enabled</Pill>
              {(auth.recoveryCodesRemaining ?? 0) <= 2 && (
                <Pill tone="warn">{auth.recoveryCodesRemaining} recovery codes left</Pill>
              )}
              <button className="btn btn-ghost" onClick={() => setEnrolling(true)}>
                Re-enroll
              </button>
              {!auth.totpRequired && (
                <button className="btn btn-ghost" onClick={() => setDisabling(true)}>
                  Turn off
                </button>
              )}
            </>
          ) : (
            <>
              <Pill tone={auth.totpRequired ? "alarm" : "warn"}>Not enabled</Pill>
              <button className="btn btn-primary" onClick={() => setEnrolling(true)}>
                Set up two-factor
              </button>
            </>
          )}
        </div>
      </div>

      <div className="settings-head" style={{ marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
        <h2 style={{ fontSize: 13 }}>Active sessions</h2>
        <p>
          Every browser or CLI signed in as this account. Signing out elsewhere revokes them on
          the server — the cookie stops working immediately, it isn't just dropped locally.
        </p>
      </div>

      <table className="reg-table">
        <thead>
          <tr><th>Session</th><th>Address</th><th>Started</th><th>Last used</th><th>Client</th></tr>
        </thead>
        <tbody>
          {sessions.map((s) => (
            <tr key={s.id}>
              <td className="mono">
                {s.id}{s.current && <Pill tone="ok">this one</Pill>}
                {s.remember && <Pill tone="info">stays signed in</Pill>}
              </td>
              <td className="mono">{s.ip || "—"}</td>
              <td className="mono" style={{ color: "var(--text-3)" }}>{when(s.createdAt)}</td>
              <td className="mono" style={{ color: "var(--text-3)" }}>{when(s.lastSeenAt)}</td>
              <td className="desc" style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }}>
                {s.userAgent || "—"}
              </td>
            </tr>
          ))}
          {sessions.length === 0 && <tr><td colSpan={5} className="desc">No sessions listed.</td></tr>}
        </tbody>
      </table>

      <div className="field-row">
        <div className="lbl">Sign out everywhere else <span className="desc">Keeps this browser signed in</span></div>
        <button
          className="btn btn-ghost"
          disabled={sessions.filter((s) => !s.current).length === 0}
          onClick={async () => {
            setError(null);
            try {
              const r = await api.revokeOtherSessions();
              setNotice(`${r.revoked} session(s) signed out`);
              await reload();
            } catch (e) {
              setError(errText(e, "could not revoke sessions"));
            }
          }}
        >
          Sign out other sessions
        </button>
      </div>

      <ChangePasswordModal
        open={changingPw}
        onClose={() => setChangingPw(false)}
        onDone={async () => {
          setChangingPw(false);
          setNotice("password changed — other sessions were signed out");
          await reload();
        }}
      />
      <TotpEnrollModal
        open={enrolling}
        onClose={() => setEnrolling(false)}
        onDone={async () => { setEnrolling(false); setNotice("two-factor enabled"); onAuthChanged(); }}
      />
      <TotpDisableModal
        open={disabling}
        onClose={() => setDisabling(false)}
        onDone={() => { setDisabling(false); setNotice("two-factor turned off"); onAuthChanged(); }}
      />
    </div>
  );
}

function ChangePasswordModal({ open, onClose, onDone }: {
  open: boolean; onClose: () => void; onDone: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.changePassword(current, next);
      setCurrent(""); setNext(""); setConfirm("");
      onDone();
    } catch (e) {
      setError(errText(e, "could not change the password"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Change password"
      sub="At least 12 characters mixing three of lowercase, uppercase, digits and symbols — or a passphrase of 20+ characters."
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary"
                  disabled={busy || !current || !next || next !== confirm}
                  onClick={submit}>
            {busy ? "Saving…" : "Change password"}
          </button>
        </>
      }
    >
      <div className="field-row">
        <div className="lbl">Current password</div>
        <input className="input" type="password" autoFocus autoComplete="current-password"
               value={current} onChange={(e) => setCurrent(e.target.value)} />
      </div>
      <div className="field-row">
        <div className="lbl">New password</div>
        <input className="input" type="password" autoComplete="new-password"
               value={next} onChange={(e) => setNext(e.target.value)} />
      </div>
      <div className="field-row">
        <div className="lbl">Confirm new password</div>
        <input className="input" type="password" autoComplete="new-password"
               value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      </div>
      {confirm && next !== confirm && (
        <div className="login-error"><Icon name="x" size={14} /><span>The two entries don't match.</span></div>
      )}
      {error && <div className="login-error" role="alert"><Icon name="x" size={14} /><span>{error}</span></div>}
    </Modal>
  );
}

function TotpEnrollModal({ open, onClose, onDone }: {
  open: boolean; onClose: () => void; onDone: () => void;
}) {
  const [secret, setSecret] = useState("");
  const [uri, setUri] = useState("");
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) { setSecret(""); setUri(""); setCode(""); setCodes(null); setError(null); return; }
    (async () => {
      try {
        const r = await api.totpEnroll();
        setSecret(r.secret);
        setUri(r.uri);
      } catch (e) {
        setError(errText(e, "could not start enrollment"));
      }
    })();
  }, [open]);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.totpConfirm(code);
      setCodes(r.recoveryCodes);
    } catch (e) {
      setError(errText(e, "that code didn't match"));
    } finally {
      setBusy(false);
    }
  };

  if (codes) {
    return (
      <Modal
        open={open}
        onClose={onDone}
        title="Save your recovery codes"
        sub="Each works once, in place of a code from your app. They are shown now and never again — the server only keeps hashes."
        footer={<button className="btn btn-primary" onClick={onDone}>I've saved them</button>}
      >
        <div className="mono" style={{
          display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, padding: 12,
          background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 8,
        }}>
          {codes.map((c) => <div key={c}>{c}</div>)}
        </div>
        <p className="desc" style={{ marginTop: 10 }}>
          Print them or put them in the site binder. Without a code and without these, getting
          back in means an admin clearing 2FA on the server.
        </p>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Set up two-factor authentication"
      sub="Add this account to an authenticator app, then confirm with a live code. It isn't active until the code checks out."
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || code.length < 6} onClick={confirm}>
            {busy ? "Checking…" : "Confirm & enable"}
          </button>
        </>
      }
    >
      <div className="field-row">
        <div className="lbl">
          Setup key
          <span className="desc">
            Enter this in your app (Google Authenticator, 1Password, Aegis, Authy…) as a
            time-based code, or paste the full link below.
          </span>
        </div>
        <input className="input mono" readOnly value={secret} onFocus={(e) => e.currentTarget.select()} />
      </div>
      <div className="field-row">
        <div className="lbl">Provisioning link <span className="desc">otpauth:// — apps that accept a link take this directly</span></div>
        <input className="input mono" readOnly value={uri} onFocus={(e) => e.currentTarget.select()} />
      </div>
      <div className="field-row">
        <div className="lbl">Code from the app</div>
        <input className="input mono" inputMode="numeric" placeholder="123456"
               value={code} onChange={(e) => setCode(e.target.value)} />
      </div>
      {error && <div className="login-error" role="alert"><Icon name="x" size={14} /><span>{error}</span></div>}
    </Modal>
  );
}

function TotpDisableModal({ open, onClose, onDone }: {
  open: boolean; onClose: () => void; onDone: () => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.totpDisable(password);
      setPassword("");
      onDone();
    } catch (e) {
      setError(errText(e, "could not turn off two-factor"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Turn off two-factor authentication"
      sub="Your password is required — otherwise a hijacked session could quietly strip 2FA off the account."
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || !password} onClick={submit}>
            {busy ? "Working…" : "Turn off two-factor"}
          </button>
        </>
      }
    >
      <div className="field-row">
        <div className="lbl">Password</div>
        <input className="input" type="password" autoFocus autoComplete="current-password"
               value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      {error && <div className="login-error" role="alert"><Icon name="x" size={14} /><span>{error}</span></div>}
    </Modal>
  );
}
