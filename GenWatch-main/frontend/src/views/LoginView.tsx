// Login screen — named accounts, with an optional TOTP second step.
//
// The two-step shape matters: the server only asks for a code once the
// username and password are already correct, so the code field appearing
// is itself a signal we don't show until then. Failures are deliberately
// vague ("username or password is incorrect") because the server refuses
// to distinguish them — that's what stops this page being an
// account-enumeration oracle on a public address.

import { useState } from "react";
import { api, ApiError } from "../api/client";
import { BrandMark, Icon } from "../components/primitives";

type Detail = { code?: string; message?: string };

function detailOf(e: unknown): Detail {
  if (e instanceof ApiError && e.body && typeof e.body === "object") {
    const d = (e.body as { detail?: unknown }).detail;
    if (d && typeof d === "object") return d as Detail;
    if (typeof d === "string") return { message: d };
  }
  return {};
}

export function LoginView({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = !!username && !!password && (!needsTotp || totp.length >= 6) && !submitting;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    setHint(null);
    try {
      const r = await api.login(username, password, needsTotp ? totp : undefined);
      if (r.usedRecoveryCode) {
        // Worth saying out loud — they just burned a one-time code.
        console.info(`Signed in with a recovery code · ${r.recoveryCodesRemaining} left`);
      }
      onLoggedIn();
    } catch (e: unknown) {
      const d = detailOf(e);
      const status = e instanceof ApiError ? e.status : 0;
      if (d.code === "totp_required") {
        setNeedsTotp(true);
        setHint("Enter the 6-digit code from your authenticator app.");
      } else if (d.code === "totp_invalid") {
        setNeedsTotp(true);
        setTotp("");
        setError("That code isn't valid — try the next one from your app.");
      } else if (d.code === "totp_enrollment_required") {
        setError(d.message ?? "Two-factor enrollment is required for this account.");
      } else if (d.code === "account_locked" || d.code === "rate_limited") {
        setError(d.message ?? "Too many attempts — try again shortly.");
      } else if (d.code === "no_accounts") {
        setError(d.message ?? "No operator accounts exist yet.");
      } else if (d.code === "https_required") {
        setError("This server requires HTTPS. Reopen the console over https://.");
      } else if (status === 401 || status === 403) {
        // Matches the server: one message for unknown user, wrong
        // password, and disabled account alike.
        setError("Username or password is incorrect.");
      } else {
        setError(d.message ?? (e as Error)?.message ?? "Sign-in failed");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-shell">
      <form onSubmit={submit} className="login-card">
        <div className="login-brand">
          <BrandMark size={56} />
          <div>
            <div className="login-title">Castle Generator Monitor</div>
            <div className="login-sub">Operator Console · v0.1</div>
          </div>
        </div>
        <p className="login-prompt">
          Sign in to view live telemetry and issue control commands.
        </p>

        <label className="login-field">
          <span>Username</span>
          <input
            className="input login-input"
            type="text"
            autoFocus
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            autoComplete="username"
            placeholder="operator"
            value={username}
            disabled={needsTotp}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>

        <label className="login-field">
          <span>Password</span>
          <input
            className="input login-input"
            type="password"
            autoComplete="current-password"
            placeholder="••••••••"
            value={password}
            disabled={needsTotp}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {needsTotp && (
          <label className="login-field">
            <span>Authentication code</span>
            <input
              className="input login-input mono"
              type="text"
              autoFocus
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="123456"
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
            />
          </label>
        )}

        {hint && !error && (
          <div className="login-error" role="status"
               style={{ color: "var(--text-2)", borderColor: "var(--border)" }}>
            <Icon name="lock" size={14} />
            <span>{hint}</span>
          </div>
        )}
        {error && (
          <div className="login-error" role="alert">
            <Icon name="x" size={14} />
            <span>{error}</span>
          </div>
        )}

        <button type="submit" className="btn btn-primary login-submit" disabled={!canSubmit}>
          <Icon name="lock" size={14} /> {submitting ? "Signing in…" : "Sign in"}
        </button>

        {needsTotp && (
          <button
            type="button"
            className="btn btn-ghost"
            style={{ marginTop: 8 }}
            onClick={() => { setNeedsTotp(false); setTotp(""); setError(null); setHint(null); }}
          >
            Back
          </button>
        )}
      </form>
      <div className="login-foot">
        <span className="dot" />
        Hardware safeties at the H-100 panel remain primary.
      </div>
    </div>
  );
}
