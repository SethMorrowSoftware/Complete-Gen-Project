// Settings → Users. Admin-only account management.
//
// Mirrors the `genwatch useradd / userpasswd / userrole / …` CLI, and
// hits the same service layer, so the guard rails are identical: the last
// enabled admin can't be deleted, disabled or demoted from either side.

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { Icon, Modal, Pill } from "../components/primitives";
import type { Role, UserRow } from "../types";

const ROLE_HELP: Record<Role, string> = {
  viewer: "Read-only — live view, history, events.",
  operator: "Everything a viewer can do, plus start/stop/exercise/transfer and alarm ack.",
  admin: "Everything an operator can do, plus configuration, register map and user management.",
};

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: { message?: string } | string })?.detail;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
  }
  return (e as Error)?.message ?? fallback;
}

function when(ts: number | null | undefined): string {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export function UsersSection({ me }: { me: string }) {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<UserRow | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const reload = async () => {
    try {
      setUsers((await api.users()).users);
    } catch (e) {
      setError(errText(e, "could not load accounts"));
    }
  };
  useEffect(() => { void reload(); }, []);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(label);
      await reload();
    } catch (e) {
      setError(errText(e, "action failed"));
    } finally {
      setBusy(null);
    }
  };

  const adminCount = (users ?? []).filter((u) => u.role === "admin" && !u.disabled).length;

  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Operator accounts</h2>
        <p>
          Each operator signs in with their own username and password, so the audit log and
          Slack alerts name a person rather than "operator". Roles decide what they can reach:
          viewers read, operators command the generator, admins change configuration and manage
          accounts. The last enabled admin is protected — you cannot delete, disable or demote
          it, because nobody could get back in without a site visit.
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
          Add an account
          <span className="desc">
            The operator sets their own password at first sign-in.
          </span>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Icon name="plus" size={14} /> New account
        </button>
      </div>

      <table className="reg-table">
        <thead>
          <tr>
            <th>Username</th><th>Role</th><th>State</th><th>2FA</th>
            <th>Last sign-in</th><th>Sessions</th><th />
          </tr>
        </thead>
        <tbody>
          {(users ?? []).map((u) => {
            const locked = u.lockedUntil * 1000 > Date.now();
            const isMe = u.username === me;
            const lastAdmin = u.role === "admin" && !u.disabled && adminCount <= 1;
            return (
              <tr key={u.username}>
                <td className="mono" style={{ color: "var(--text)" }}>
                  {u.username}{isMe && <span className="desc"> (you)</span>}
                </td>
                <td>
                  <select
                    className="input"
                    value={u.role}
                    disabled={isMe || lastAdmin || busy != null}
                    title={isMe ? "You cannot change your own role" : ROLE_HELP[u.role]}
                    onChange={(e) =>
                      act(`${u.username} → ${e.target.value}`,
                          () => api.updateUser(u.username, { role: e.target.value as Role }))}
                  >
                    <option value="viewer">viewer</option>
                    <option value="operator">operator</option>
                    <option value="admin">admin</option>
                  </select>
                </td>
                <td>
                  {u.disabled ? <Pill tone="alarm">disabled</Pill>
                    : locked ? <Pill tone="warn">locked</Pill>
                    : u.mustChangePassword ? <Pill tone="warn">must set password</Pill>
                    : <Pill tone="ok">active</Pill>}
                  {u.failedAttempts > 0 && !locked && (
                    <span className="desc"> {u.failedAttempts} failed</span>
                  )}
                </td>
                <td>
                  {u.totpEnabled
                    ? <Pill tone="ok">on{u.recoveryCodesRemaining <= 2 ? " · low codes" : ""}</Pill>
                    : <span className="desc">off</span>}
                </td>
                <td className="mono" style={{ color: "var(--text-3)" }}>
                  {when(u.lastLoginAt)}
                  {u.lastLoginIp && <div className="desc">{u.lastLoginIp}</div>}
                </td>
                <td className="mono">{u.activeSessions}</td>
                <td>
                  <div className="flex ai-c gap-8" style={{ flexWrap: "wrap", justifyContent: "flex-end" }}>
                    <button className="btn btn-ghost" disabled={busy != null}
                            onClick={() => setResetting(u)}>Reset password</button>
                    {locked && (
                      <button className="btn btn-ghost" disabled={busy != null}
                              onClick={() => act(`${u.username} unlocked`, () => api.unlockUser(u.username))}>
                        Unlock
                      </button>
                    )}
                    {u.activeSessions > 0 && (
                      <button className="btn btn-ghost" disabled={busy != null}
                              onClick={() => act(`${u.username} signed out everywhere`,
                                                 () => api.revokeUserSessions(u.username))}>
                        Sign out
                      </button>
                    )}
                    {u.totpEnabled && (
                      <button className="btn btn-ghost" disabled={busy != null}
                              title="Lost-phone path — clears the second factor for this account"
                              onClick={() => act(`${u.username} 2FA cleared`,
                                                 () => api.disableUserTotp(u.username))}>
                        Clear 2FA
                      </button>
                    )}
                    {!isMe && (
                      <button className="btn btn-ghost" disabled={busy != null || lastAdmin}
                              style={{ color: lastAdmin ? undefined : "var(--red)" }}
                              title={lastAdmin ? "This is the last enabled admin" : undefined}
                              onClick={() => act(`${u.username} deleted`,
                                                 () => api.deleteUser(u.username))}>
                        Delete
                      </button>
                    )}
                    {!isMe && !u.disabled && (
                      <button className="btn btn-ghost" disabled={busy != null || lastAdmin}
                              onClick={() => act(`${u.username} disabled`,
                                                 () => api.updateUser(u.username, { disabled: true }))}>
                        Disable
                      </button>
                    )}
                    {u.disabled && (
                      <button className="btn btn-ghost" disabled={busy != null}
                              onClick={() => act(`${u.username} enabled`,
                                                 () => api.updateUser(u.username, { disabled: false }))}>
                        Enable
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
          {users != null && users.length === 0 && (
            <tr><td colSpan={7} className="desc">No accounts yet.</td></tr>
          )}
        </tbody>
      </table>

      <CreateUserModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={async (msg) => { setCreating(false); setNotice(msg); await reload(); }}
      />
      <ResetPasswordModal
        user={resetting}
        onClose={() => setResetting(null)}
        onDone={async (msg) => { setResetting(null); setNotice(msg); await reload(); }}
      />
    </div>
  );
}

function CreateUserModal({
  open, onClose, onCreated,
}: {
  open: boolean; onClose: () => void; onCreated: (msg: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("operator");
  const [mustChange, setMustChange] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.createUser({
        username, password, role, must_change_password: mustChange,
      });
      onCreated(`created ${username.toLowerCase()}`);
      setUsername(""); setPassword(""); setRole("operator"); setMustChange(true);
    } catch (e) {
      setError(errText(e, "could not create the account"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New operator account"
      sub="The password you set here is temporary — they'll be required to replace it before the console unlocks."
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || !username || !password}
                  onClick={submit}>
            {busy ? "Creating…" : "Create account"}
          </button>
        </>
      }
    >
      <div className="field-row">
        <div className="lbl">Username <span className="desc">3–32 chars · letters, digits, . _ -</span></div>
        <input className="input mono" autoFocus autoCapitalize="none" spellCheck={false}
               value={username} onChange={(e) => setUsername(e.target.value)} />
      </div>
      <div className="field-row">
        <div className="lbl">
          Temporary password
          <span className="desc">12+ chars, or a 20+ char passphrase</span>
        </div>
        <input className="input" type="text" autoComplete="off" spellCheck={false}
               value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      <div className="field-row">
        <div className="lbl">Role <span className="desc">{ROLE_HELP[role]}</span></div>
        <select className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="viewer">viewer</option>
          <option value="operator">operator</option>
          <option value="admin">admin</option>
        </select>
      </div>
      <div className="field-row">
        <div className="lbl">
          Require a password change
          <span className="desc">Recommended — you've seen this password, they shouldn't keep it</span>
        </div>
        <input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
      </div>
      {error && <div className="login-error" role="alert"><Icon name="x" size={14} /><span>{error}</span></div>}
    </Modal>
  );
}

function ResetPasswordModal({
  user, onClose, onDone,
}: {
  user: UserRow | null; onClose: () => void; onDone: (msg: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      await api.resetUserPassword(user.username, password, true);
      onDone(`password reset for ${user.username}`);
      setPassword("");
    } catch (e) {
      setError(errText(e, "could not reset the password"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={user != null}
      onClose={onClose}
      title={`Reset password — ${user?.username ?? ""}`}
      sub="Every live session for this account is signed out immediately, and they must set their own password at next sign-in."
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || !password} onClick={submit}>
            {busy ? "Saving…" : "Reset password"}
          </button>
        </>
      }
    >
      <div className="field-row">
        <div className="lbl">Temporary password <span className="desc">12+ chars, or a 20+ char passphrase</span></div>
        <input className="input" type="text" autoFocus autoComplete="off" spellCheck={false}
               value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      {error && <div className="login-error" role="alert"><Icon name="x" size={14} /><span>{error}</span></div>}
    </Modal>
  );
}
