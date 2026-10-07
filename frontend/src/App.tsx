import { useCallback, useEffect, useState } from "react";
import { useData } from "./api";
import { Notice, healthLabel } from "./ui";
import Trial from "./Trial";
import { MemoryList, MemoryDetail } from "./Memory";
import StatusPage from "./Status";
import type { Status } from "./types";

const routes = [
  { id: "trial", name: "试用对话", icon: "chat" },
  { id: "memories", name: "记忆", icon: "memory" },
  { id: "status", name: "运行状态", icon: "status" },
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
  const [route, setRoute] = useState(location.hash.slice(2) || "trial");
  const [selected, setSelected] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const status = useData<Status>("/status", 2000);
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
              aria-current={route === r.id ? "page" : undefined}
            >
              <Icon name={r.icon} />
              {r.name}
            </a>
          ))}
        </nav>
        <span className="local-badge">
          <span aria-hidden>◉</span> 仅本机
        </span>
      </header>
      <main id="main" tabIndex={-1}>
        <div className="content-wrap">
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
          {route === "memories" ? (
            <MemoryList version={version} openMemory={openMemory} />
          ) : route === "status" ? (
            <StatusPage data={status.data} />
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
          onChange={() => {
            setVersion((v) => v + 1);
            status.refresh();
          }}
        />
      )}
    </div>
  );
}
