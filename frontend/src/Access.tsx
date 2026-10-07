import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, json, errorText, setCSRF } from "./api";
import { Notice } from "./ui";
import { SetupWizard } from "./Settings";
type Session = {
  configured: boolean;
  authenticated: boolean;
  admin_exists: boolean;
  csrf_token: string;
};
export default function Access({
  children,
}: {
  children: (refresh: () => void) => ReactNode;
}) {
  const [session, setSession] = useState<Session | null>(null),
    [error, setError] = useState("");
  const refresh = useCallback(() => {
    void api<Session>("/session")
      .then((s) => {
        setCSRF(s.csrf_token);
        setSession(s);
        setError("");
      })
      .catch((e) => setError(errorText(e)));
  }, []);
  useEffect(() => {
    refresh();
    addEventListener("iris-auth", refresh);
    return () => removeEventListener("iris-auth", refresh);
  }, [refresh]);
  if (session?.configured && session.authenticated) return children(refresh);
  return (
    <main className="access-page">
      <div className="access-brand">
        <span aria-hidden>✳</span> Iris <small>相处与记忆</small>
      </div>
      <div className="panel access-card">
        <span className="eyebrow">仅本机管理</span>
        <h1>{session?.configured ? "管理员登录" : "首次设置"}</h1>
        {error && (
          <Notice error>
            {error}
            <button onClick={refresh}>重新连接</button>
          </Notice>
        )}
        {!session ? (
          <p>正在连接…</p>
        ) : session.authenticated ? (
          <SetupWizard done={refresh} />
        ) : (
          <PasswordForm
            creating={!session.admin_exists}
            configured={session.configured}
            done={refresh}
          />
        )}
      </div>
    </main>
  );
}
function PasswordForm({
  creating,
  configured,
  done,
}: {
  creating: boolean;
  configured: boolean;
  done: () => void;
}) {
  const [password, setPassword] = useState(""),
    [confirm, setConfirm] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creating && password !== confirm) {
      setError("两次密码不一致");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api(
        creating ? "/setup/password" : "/login",
        json("POST", { password }),
      );
      setPassword("");
      setConfirm("");
      if (configured && location.pathname !== "/")
        history.replaceState(null, "", "/#/trial");
      done();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="password-form" onSubmit={submit}>
      <p>
        {creating
          ? "1 · 先设置管理员密码，保护记忆与设置。"
          : "请输入管理员密码。"}
        {!creating && !configured && "登录后继续完成首次设置。"}
      </p>
      <label>
        管理员密码
        <input
          required
          type="password"
          aria-label="管理员密码"
          minLength={8}
          maxLength={256}
          autoComplete={creating ? "new-password" : "current-password"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <small>至少 8 个字符。</small>
      </label>
      {creating && (
        <label>
          确认密码
          <input
            required
            type="password"
            minLength={8}
            maxLength={256}
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </label>
      )}
      {error && <Notice error>{error}</Notice>}
      <button className="primary" disabled={busy}>
        {busy ? "正在验证…" : creating ? "设置密码并继续" : "登录"}
      </button>
    </form>
  );
}
