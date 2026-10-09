import { useCallback, useEffect, useState } from "react";
import { api, json, useData } from "./api";
import { Notice, healthLabel } from "./ui";
import Access from "./Access";
import SettingsPage from "./Settings";
import Trial from "./Trial";
import { MemoryPage, MemoryDetail } from "./Memory";
import Operations from "./Operations";
import StatusPage from "./Status";
import LearningPage from "./Learning";
import People from "./People";
import StatePage from "./State";
import PersonaPage from "./Persona";
import type { Page, PersonSummary, Status } from "./types";

const routes = [
  { id: "trial", name: "试用对话", icon: "chat" },
  { id: "memories", name: "记忆", icon: "memory" },
  { id: "people", name: "人物", icon: "people" },
  { id: "learning", name: "入口与学习", icon: "learning" },
  { id: "persona", name: "persona 与自我", icon: "people" },
  { id: "state", name: "状态与目标", icon: "state" },
  { id: "status", name: "运行状态", icon: "status" },
  { id: "operations", name: "操作记录", icon: "memory" },
  { id: "settings", name: "设置", icon: "settings" },
];
function Icon({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {name === "chat" ? (
        <path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2 1.5-5A8 8 0 1 1 20 11.5ZM7 10h8M7 14h5" />
      ) : name === "people" ? (
        <>
          <circle cx="9" cy="7" r="3" />
          <path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 4v3" />
        </>
      ) : name === "learning" ? (
        <>
          <path d="M4 4h16v5H4zM4 14h7v6H4zM16 14h4v6h-4M8 9v5M18 9v5" />
        </>
      ) : name === "state" ? (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </>
      ) : name === "memory" ? (
        <>
          <rect x="5" y="3" width="14" height="18" rx="3" />
          <path d="M9 8h6M9 12h6M9 16h3" />
        </>
      ) : (
        <>
          <path d="M3 13h4l3-7 4 13 3-7h4" />
        </>
      )}
    </svg>
  );
}
export default function App() {
  return <Access>{(refresh) => <Workspace refreshSession={refresh} />}</Access>;
}
function Workspace({ refreshSession }: { refreshSession: () => void }) {
  const [route, setRoute] = useState(location.hash.slice(2) || "trial");
  const [selected, setSelected] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const status = useData<Status>("/status", 2000);
  const pendingPeople = useData<Page<PersonSummary>>(
    "/people?pending_only=true&limit=1&offset=0",
    5000,
  );
  const routePath = route.split("?")[0];
  const memoryChanged = () => {
    setVersion((v) => v + 1);
    status.refresh();
  };
  useEffect(() => {
    const changed = () => {
      setRoute(location.hash.slice(2) || "trial");
      setSelected(null);
    };
    addEventListener("hashchange", changed);
    return () => removeEventListener("hashchange", changed);
  }, []);
  const openMemory = useCallback((id: number) => setSelected(id), []);
  const closeMemory = useCallback(() => setSelected(null), []);
  const chat = status.data?.model_health.chat;
  return (
    <div className="app">
      <a
        className="skip-link"
        href="#main"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main")?.focus();
        }}
      >
        跳到主内容
      </a>
      <header className="app-header">
        <a href="#/trial" className="brand" aria-label="Iris 首页">
          <span className="brand-symbol" aria-hidden>
            ✳
          </span>
          <span>
            Iris<small>相处与记忆</small>
          </span>
        </a>
        <nav aria-label="主导航">
          {routes.map((r) => (
            <a
              key={r.id}
              href={`#/${r.id}`}
              aria-current={routePath === r.id ? "page" : undefined}
            >
              <Icon name={r.icon} />
              {r.name}
              {r.id === "people" && !!pendingPeople.data?.total && (
                <span className="nav-dot" aria-label="有待确认的人物联系" />
              )}
            </a>
          ))}
        </nav>
        <button
          className="text-button logout-button"
          onClick={async () => {
            await api("/logout", json("POST", {}));
            refreshSession();
          }}
        >
          退出登录
        </button>
        <span className="local-badge">
          <span aria-hidden>◉</span> 仅本机
        </span>
      </header>
      <main id="main" tabIndex={-1}>
        <div className="content-wrap">
          {!!pendingPeople.data?.total && routePath === "trial" && (
            <Notice>
              {pendingPeople.data.total} 位人物有待确认的“可能是同一人”联系。
              <a href="#/people?pending_only=true">查看待确认联系</a>
            </Notice>
          )}
          {pendingPeople.error && routePath === "trial" && (
            <Notice error>
              人物联系提示暂时无法更新。
              <button onClick={pendingPeople.refresh}>重试人物提示</button>
            </Notice>
          )}
          {status.error && (
            <Notice error>
              {status.error}。以下数据可能尚未更新。
              <button className="text-button" onClick={status.refresh}>
                重新连接
              </button>
            </Notice>
          )}
          {chat && chat.state !== "normal" && (
            <Notice error>
              对话模型：{healthLabel(chat.state)}。
              {chat.last_error?.includes("未配置")
                ? "未配置模型，暂不学习。"
                : "学习已暂停。"}
              消息仍可接收。<a href="#/status">查看运行状态</a>
            </Notice>
          )}
          {routePath === "settings" ? (
            <SettingsPage />
          ) : routePath === "memories" ? (
            <MemoryPage
              key={route}
              initialQuery={route.split("?")[1]}
              version={version}
              openMemory={openMemory}
              onChange={memoryChanged}
            />
          ) : routePath === "people" ? (
            <People
              key={route}
              initialQuery={route.split("?")[1]}
              onChange={() => {
                pendingPeople.refresh();
                memoryChanged();
              }}
            />
          ) : routePath === "operations" ? (
            <Operations key={route} initialQuery={route.split("?")[1]} />
          ) : routePath === "learning" ? (
            <LearningPage openMemory={openMemory} />
          ) : routePath === "state" ? (
            <StatePage
              key={route}
              initialQuery={route.split("?")[1]}
              openMemory={openMemory}
            />
          ) : routePath === "persona" ? (
            <PersonaPage
              key={route}
              initialQuery={route.split("?")[1]}
              openMemory={openMemory}
            />
          ) : routePath === "status" ? (
            <StatusPage
              key={route}
              initialQuery={route.split("?")[1] || ""}
              data={status.data}
              openMemory={openMemory}
            />
          ) : (
            <Trial
              status={status.data}
              openMemory={openMemory}
              refreshStatus={status.refresh}
            />
          )}
        </div>
      </main>
      <footer className="app-footer">Iris · 记忆有来源，相处有连续</footer>
      {selected !== null && (
        <MemoryDetail
          key={selected}
          id={selected}
          onClose={closeMemory}
          openMemory={openMemory}
          onChange={memoryChanged}
        />
      )}
    </div>
  );
}
