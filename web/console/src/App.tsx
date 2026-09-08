import { useEffect, useRef, useState } from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { api, ApiError } from "./api/client";
import type { Bootstrap } from "./api/design";
import { OperationsPage } from "./pages/Operations";
import { PersonasPage } from "./pages/Personas";
import {
  Dialog,
  Empty,
  Environment,
  ErrorNotice,
  QueryState,
  TextData,
  useQuery,
} from "./components/core";
import { MemoryPage } from "./pages/Memory";
import { ExportsPage, ImportsPage, StatsPage } from "./pages/Transfers";
import { SettingsPage } from "./pages/Configuration";
import { ProvidersPage } from "./pages/Providers";
import {
  KeysPage,
  ManagementPage,
  ServiceCredentialsPage,
  Sessions,
} from "./pages/Management";
const modules = [
  ["memory", "记忆管理", "memory.read"],
  ["stats", "统计观测", "stats.read"],
  ["exports", "数据导出", "exports.write"],
  ["imports", "手动导入", "imports.write"],
  ["personas", "Persona 人格", "memory.read"],
  ["keys", "密钥与会话", "keys.manage"],
  ["service_credentials", "宿主凭据", "service_keys.manage"],
  ["providers", "Embedding Provider", "providers.manage"],
  ["settings", "运行参数", "settings.write"],
  ["retention", "保留与 Hold", "retention.manage"],
  ["operations", "批量操作", "memory.forget"],
  ["system", "系统运维", "system.read"],
  ["audit", "只读审计", "audit.read"],
] as const;
export function App() {
  const [version, setVersion] = useState(0);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<unknown>();
  const [reauth, setReauth] = useState<{
    resolve: () => void;
    reject: (e: unknown) => void;
  }>();
  useEffect(() => api.subscribe(() => setVersion((v) => v + 1)), []);
  useEffect(() => {
    void api
      .restore()
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) setError(e);
      })
      .finally(() => setReady(true));
    api.onReauth = () =>
      new Promise<void>((resolve, reject) => setReauth({ resolve, reject }));
    return () => {
      api.onReauth = null;
    };
  }, []);
  useEffect(() => {
    if (!api.session && reauth) {
      reauth.reject(new Error("会话已失效"));
      setReauth(undefined);
    }
  }, [version, reauth]);
  return (
    <BrowserRouter basename="/console">
      {__CONSOLE_MOCK__ && (
        <div className="mock-banner" role="status">
          模拟环境 · 仅供开发与测试 ·
          所有业务数据与操作均为模拟，未通过真实业务联调
        </div>
      )}
      {!ready ? (
        <Empty>恢复会话…</Empty>
      ) : api.session ? (
        <ConsoleShell key={api.epoch} epoch={api.epoch} />
      ) : (
        <Login error={error} />
      )}
      {reauth && (
        <Dialog
          title="敏感操作 · 重新认证"
          onClose={() => {
            reauth.reject(new Error("已取消重新认证"));
            setReauth(undefined);
          }}
        >
          <KeyForm
            label="重新认证并继续原动作"
            onSubmit={async (key) => {
              await api.reauth(key);
              reauth.resolve();
              setReauth(undefined);
            }}
          />
        </Dialog>
      )}
    </BrowserRouter>
  );
}
function Login({ error }: { error?: unknown }) {
  return (
    <main className="login">
      <div className="brand-mark">I</div>
      <p className="eyebrow">IRIS MEMORY CORE</p>
      <h1>管理控制台</h1>
      <p>使用离线签发的运营密钥建立同源会话。</p>
      <ErrorNotice error={error} />
      <KeyForm label="登录控制台" onSubmit={(key) => api.login(key)} />
      <p className="muted">
        密钥只用于本次认证，登录后由 HttpOnly Cookie
        承载会话。未发布的业务模块不会出现在真实环境导航中。
      </p>
      {__CONSOLE_MOCK__ && (
        <p>模拟登录：输入任意非空测试文本（不要使用真实密钥）。</p>
      )}
    </main>
  );
}
function KeyForm({
  label,
  onSubmit,
}: {
  label: string;
  onSubmit: (key: string) => Promise<void>;
}) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const lock = useRef(false);
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (lock.current) return;
        lock.current = true;
        setBusy(true);
        setError(undefined);
        const promise = onSubmit(key);
        setKey("");
        void promise.catch(setError).finally(() => {
          lock.current = false;
          setBusy(false);
        });
      }}
    >
      <label>
        运营密钥
        <input
          autoFocus
          autoComplete="off"
          type="password"
          required
          value={key}
          disabled={busy}
          onChange={(e) => setKey(e.target.value)}
        />
      </label>
      <ErrorNotice error={error} />
      <button className="primary" disabled={!key || busy}>
        {busy ? "认证中…" : label}
      </button>
    </form>
  );
}
function ConsoleShell({ epoch }: { epoch: number }) {
  const bootstrap = useQuery<Bootstrap>("/bootstrap");
  const location = useLocation();
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    document.querySelector<HTMLElement>("main h1")?.focus();
  }, [location.pathname]);
  useEffect(() => {
    const activity = (event: Event) => {
      if (event.isTrusted) void api.activity().catch(setError);
    };
    document.addEventListener("pointerdown", activity);
    document.addEventListener("keydown", activity);
    const expiry = api.session
      ? setTimeout(
          () => api.clear(),
          Math.max(0, Date.parse(api.session.session.expires_at) - Date.now()),
        )
      : undefined;
    return () => {
      document.removeEventListener("pointerdown", activity);
      document.removeEventListener("keydown", activity);
      clearTimeout(expiry);
    };
  }, []);
  return (
    <QueryState query={bootstrap}>
      {bootstrap.data && (
        <Environment.Provider value={{ bootstrap: bootstrap.data, epoch }}>
          <div className="shell">
            <a className="skip-link" href="#content">
              跳到正文
            </a>
            <aside>
              <Link className="brand" to="/">
                <span className="brand-mark">I</span>
                <span>
                  IRIS<small>MEMORY CORE</small>
                </span>
              </Link>
              <p className="nav-caption">管理工作区</p>
              <nav>
                <NavLink to="/" end>
                  控制台概览
                </NavLink>
                {modules
                  .filter(
                    ([id, , permission]) =>
                      bootstrap.data!.modules.includes(id) &&
                      (bootstrap.data!.permissions.includes(permission) ||
                        (id === "providers" &&
                          bootstrap.data!.permissions.includes("system.read"))),
                  )
                  .map(([id, label]) => (
                    <NavLink key={id} to={`/${id}`}>
                      {label}
                    </NavLink>
                  ))}
              </nav>
              <div className="sidebar-bottom">
                <span className="status-dot" /> 会话已建立
                <small>{api.session?.operator.label}</small>
              </div>
            </aside>
            <div className="main-column">
              <header>
                <span>
                  Console / {location.pathname.split("/")[1] || "overview"}
                </span>
                <div>
                  <span className="tag">
                    Contract {bootstrap.data.contract_version}
                  </span>
                  <Link to="/sessions">会话管理</Link>
                  <button onClick={() => void api.logout().catch(setError)}>
                    退出 / 切换会话
                  </button>
                </div>
              </header>
              <main id="content">
                <ErrorNotice error={error} />
                {bootstrap.data.read_only && (
                  <p className="notice">只读模式：写操作已禁用。</p>
                )}
                {bootstrap.data.maintenance && (
                  <p className="notice">维护模式：请等待维护结束。</p>
                )}
                {bootstrap.data.pending_restart && (
                  <p className="notice">存在待重启参数，尚未在所有实例生效。</p>
                )}
                <Routes>
                  <Route path="/sessions" element={<Sessions />} />
                  <Route
                    path="/"
                    element={<Overview bootstrap={bootstrap.data} />}
                  />
                  {modules
                    .filter(
                      ([id, , permission]) =>
                        bootstrap.data!.modules.includes(id) &&
                        (bootstrap.data!.permissions.includes(permission) ||
                          (id === "providers" &&
                            bootstrap.data!.permissions.includes(
                              "system.read",
                            ))),
                    )
                    .map(([id]) => (
                      <Route
                        key={id}
                        path={`${id}/*`}
                        element={
                          id === "memory" ? (
                            <Routes>
                              <Route
                                path=":collection?"
                                element={<MemoryPage />}
                              />
                            </Routes>
                          ) : id === "stats" ? (
                            <StatsPage />
                          ) : id === "exports" ? (
                            <ExportsPage />
                          ) : id === "imports" ? (
                            <ImportsPage />
                          ) : id === "keys" ? (
                            <KeysPage />
                          ) : id === "service_credentials" ? (
                            <ServiceCredentialsPage />
                          ) : id === "providers" ? (
                            <ProvidersPage />
                          ) : id === "settings" ? (
                            <SettingsPage />
                          ) : id === "operations" ? (
                            <OperationsPage />
                          ) : id === "personas" ? (
                            <PersonasPage />
                          ) : (
                            <ManagementPage kind={id} />
                          )
                        }
                      />
                    ))}
                  <Route
                    path="*"
                    element={
                      <Empty>
                        此模块未发布、未启用或当前会话无权限。
                        <Link to="/">返回概览</Link>
                      </Empty>
                    }
                  />
                </Routes>
              </main>
              <footer>
                IRIS MEMORY CORE · Console plane · 会话内数据 · UTC 原始时间保留
              </footer>
            </div>
          </div>
        </Environment.Provider>
      )}
    </QueryState>
  );
}
function Overview({ bootstrap }: { bootstrap: Bootstrap }) {
  return (
    <>
      <p className="eyebrow">WORKSPACE OVERVIEW</p>
      <h1 tabIndex={-1}>管理控制台</h1>
      <p className="lead">查看记忆、审核变更，让每一次操作都可追溯。</p>
      <div className="metric-grid">
        <article className="metric">
          <small>当前授权模块</small>
          <strong>{bootstrap.modules.length}</strong>
          <span>以 bootstrap 为准</span>
        </article>
        <article className="metric">
          <small>运行状态</small>
          <strong>
            {bootstrap.maintenance
              ? "维护"
              : bootstrap.read_only
                ? "只读"
                : "就绪"}
          </strong>
          <span>写入仍由服务端逐次授权</span>
        </article>
        <article className="metric">
          <small>数据导入</small>
          <strong>{bootstrap.import_in_progress ? "进行中" : "空闲"}</strong>
          <span>单文件上限 {bootstrap.upload_limits.file_bytes} 字节</span>
        </article>
      </div>
      {!bootstrap.modules.length ? (
        <section className="panel">
          <h2>业务模块尚未接入</h2>
          <p>
            会话和 bootstrap
            可用；后端尚未发布业务模块。此页面不会自动回退模拟数据。
          </p>
        </section>
      ) : (
        <section className="panel">
          <h2>可用工作区</h2>
          <div className="module-grid">
            {modules
              .filter(
                ([id, , permission]) =>
                  bootstrap.modules.includes(id) &&
                  (bootstrap.permissions.includes(permission) ||
                    (id === "providers" &&
                      bootstrap.permissions.includes("system.read"))),
              )
              .map(([id, label]) => (
                <Link key={id} to={`/${id}`}>
                  <strong>{label}</strong>
                  <span>进入工作区 →</span>
                </Link>
              ))}
          </div>
        </section>
      )}
      <details>
        <summary>会话与部署边界</summary>
        <TextData
          value={{
            permissions: bootstrap.permissions,
            display_timezone: bootstrap.display_timezone,
            upload_limits: bootstrap.upload_limits,
          }}
        />
      </details>
    </>
  );
}
