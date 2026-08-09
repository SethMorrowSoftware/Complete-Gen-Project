// Forced password change.
//
// Shown instead of the console when an account still carries a temporary
// password an admin issued. The server enforces this too — every endpoint
// except this one returns 403 password_change_required — so this screen is
// the UI half of a real gate, not a polite suggestion.

import { useState } from "react";
import { api, ApiError } from "../api/client";
import { BrandMark, Icon } from "../components/primitives";

export function ChangePasswordView({
  operator, onDone, onCancel,
}: {
  operator: string;
  onDone: () => void;
  onCancel: () => Promise<void> | void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const mismatch = !!confirm && next !== confirm;
  const canSubmit = !!current && !!next && next === confirm && !submitting;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.changePassword(current, next);
      onDone();
    } catch (e: unknown) {
      const body = e instanceof ApiError ? (e.body as { detail?: { message?: string } }) : null;
      // The server's policy messages name exactly what to fix ("must be at
      // least 12 characters", "contains the username"), so pass them
      // through verbatim rather than flattening to "invalid password".
      setError(body?.detail?.message ?? (e as Error)?.message ?? "Could not change the password");
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
            <div className="login-title">Set a new password</div>
            <div className="login-sub">{operator}</div>
          </div>
        </div>
        <p className="login-prompt">
          This account is using a temporary password. Choose your own before continuing —
          at least 12 characters, mixing three of lowercase, uppercase, digits and symbols,
          or a passphrase of 20+ characters.
        </p>

        <label className="login-field">
          <span>Current (temporary) password</span>
          <input className="input login-input" type="password" autoFocus
                 autoComplete="current-password"
                 value={current} onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label className="login-field">
          <span>New password</span>
          <input className="input login-input" type="password" autoComplete="new-password"
                 value={next} onChange={(e) => setNext(e.target.value)} />
        </label>
        <label className="login-field">
          <span>Confirm new password</span>
          <input className="input login-input" type="password" autoComplete="new-password"
                 value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </label>

        {mismatch && (
          <div className="login-error" role="alert">
            <Icon name="x" size={14} /> <span>The two entries don't match.</span>
          </div>
        )}
        {error && (
          <div className="login-error" role="alert">
            <Icon name="x" size={14} /> <span>{error}</span>
          </div>
        )}

        <button type="submit" className="btn btn-primary login-submit" disabled={!canSubmit}>
          <Icon name="lock" size={14} /> {submitting ? "Saving…" : "Set password & continue"}
        </button>
        <button type="button" className="btn btn-ghost" style={{ marginTop: 8 }}
                onClick={() => void onCancel()}>
          Sign out instead
        </button>
      </form>
      <div className="login-foot">
        <span className="dot" />
        Changing your password signs out every other session for this account.
      </div>
    </div>
  );
}
