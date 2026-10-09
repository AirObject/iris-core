import { useState } from "react";
import { useData } from "./api";
import {
  Badge,
  Empty,
  Notice,
  Pagination,
  RoleTime,
  lifecycleLabel,
} from "./ui";
import {
  PersonaDiff,
  PersonaVersionView,
  type PersonaCatalog,
  type PersonaActions,
} from "./PersonaDetails";
import type { PersonaSummary, PersonaAttempt } from "./persona-types";
import type { Page, Memory } from "./types";
import {
  personaStatuses,
  personaSources,
  personaDegrees,
  personaReason,
  attemptLabel,
} from "./persona-labels";

export function PersonaHistory({
  initialVersion,
  currentID,
  catalog,
  openMemory,
  onAction,
  locked,
}: {
  initialVersion?: number;
  currentID?: number;
  catalog: PersonaCatalog | null;
  openMemory: (id: number) => void;
  onAction: PersonaActions;
  locked: boolean;
}) {
  const [selected, setSelected] = useState(initialVersion);
  const [filters, setFilters] = useState({ status: "", source: "" }),
    [query, setQuery] = useState(""),
    [offset, setOffset] = useState(0);
  const list = useData<Page<PersonaSummary>>(
    `/persona/versions?${query}limit=30&offset=${offset}`,
    5000,
  );
  const [before, setBefore] = useState(""),
    [after, setAfter] = useState(currentID?.toString() || ""),
    [error, setError] = useState("");
  const [comparison, setComparison] = useState<{
      before: number;
      after: number;
    } | null>(null),
    [compareKey, setCompareKey] = useState(0);
  return (
    <>
      <section className="panel persona-history">
        <h2>版本历史</h2>
        <p>
          可选择任意两个版本逐句对比。只有已经发布过的版本可以回滚；回滚会新建版本。
        </p>
        <form
          className="persona-filters"
          onSubmit={(e) => {
            e.preventDefault();
            const q = new URLSearchParams();
            if (filters.status) q.set("status", filters.status);
            if (filters.source) q.set("source", filters.source);
            setQuery(q.size ? `${q}&` : "");
            setOffset(0);
            list.refresh();
          }}
        >
          <label>
            版本状态
            <select
              value={filters.status}
              onChange={(e) =>
                setFilters({ ...filters, status: e.target.value })
              }
            >
              <option value="">全部状态</option>
              {Object.entries(personaStatuses).map(([v, t]) => (
                <option key={v} value={v}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label>
            版本来源
            <select
              value={filters.source}
              onChange={(e) =>
                setFilters({ ...filters, source: e.target.value })
              }
            >
              <option value="">全部来源</option>
              {Object.entries(personaSources).map(([v, t]) => (
                <option key={v} value={v}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <button>筛选版本</button>
        </form>
        {list.error && (
          <Notice error>
            {list.error}
            <button onClick={list.refresh}>重试版本列表</button>
          </Notice>
        )}
        {list.loading && <p>正在读取版本…</p>}
        {list.data?.items.map((v) => (
          <article className="persona-history-row" key={v.id}>
            <div>
              <strong>v{v.id}</strong>{" "}
              <Badge>{personaStatuses[v.status]}</Badge> ·{" "}
              {personaSources[v.source]} · 变化{" "}
              {v.change_degree ? personaDegrees[v.change_degree] : "未记录"}
              <p className="muted">
                <RoleTime value={v.generated_at} />
              </p>
              <p className="persona-content">{v.content}</p>
            </div>
            <div className="persona-actions">
              <button onClick={() => setSelected(v.id)}>
                查看版本 v{v.id}
              </button>
              <button onClick={() => setBefore(String(v.id))}>
                v{v.id} 作为对比前版
              </button>
              <button onClick={() => setAfter(String(v.id))}>
                v{v.id} 作为对比后版
              </button>
            </div>
          </article>
        ))}
        {list.data?.total === 0 && <Empty title="没有符合筛选的版本" />}
        {list.data && (
          <Pagination
            total={list.data.total}
            offset={offset}
            change={setOffset}
          />
        )}
      </section>
      <section className="panel persona-comparison">
        <h2>对比任意版本</h2>
        <p>从列表选择，或输入版本 ID；可以跨页选择，也可以查看被拒绝的候选。</p>
        <form
          noValidate
          className="persona-filters"
          onSubmit={(e) => {
            e.preventDefault();
            if (
              !/^\d+$/.test(before) ||
              !/^\d+$/.test(after) ||
              !Number.isSafeInteger(Number(before)) ||
              !Number.isSafeInteger(Number(after)) ||
              Number(before) < 1 ||
              Number(after) < 1
            ) {
              setError("请输入有效的正整数版本 ID");
              return;
            }
            setError("");
            setComparison({ before: Number(before), after: Number(after) });
            setCompareKey((k) => k + 1);
          }}
        >
          <label>
            对比前版本 ID
            <input
              type="number"
              min={1}
              step={1}
              value={before}
              onChange={(e) => setBefore(e.target.value)}
            />
          </label>
          <label>
            对比后版本 ID
            <input
              type="number"
              min={1}
              step={1}
              value={after}
              onChange={(e) => setAfter(e.target.value)}
            />
          </label>
          <button>对比版本</button>
        </form>
        {error && <Notice error>{error}</Notice>}
        {comparison && <PersonaDiff key={compareKey} {...comparison} />}
      </section>
      {selected && (
        <PersonaVersionView
          key={selected}
          id={selected}
          title="版本详情"
          catalog={catalog}
          openMemory={openMemory}
          onAction={onAction}
          locked={locked}
          currentID={currentID}
          history
        />
      )}
    </>
  );
}
export function PersonaMemories({
  openMemory,
}: {
  openMemory: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  const list = useData<Page<Memory>>(
    `/persona/self-memories?limit=30&offset=${offset}`,
  );
  return (
    <section className="panel persona-memories">
      <h2>自我记忆</h2>
      <p>有效的自我设定与经历是 persona 的材料；打开或翻页不会触发召回。</p>
      {list.error && (
        <Notice error>
          {list.error}
          <button onClick={list.refresh}>重试自我记忆</button>
        </Notice>
      )}
      {list.loading && <p>正在读取自我记忆…</p>}
      {list.data?.items.map((m) => (
        <article key={m.id} className="persona-history-row">
          <p className="persona-content">{m.content}</p>
          <p className="muted">
            #{m.id} · 修订 {m.revision} · {m.stance} ·{" "}
            {lifecycleLabel(m.lifecycle)} · <RoleTime value={m.created_at} />
          </p>
          <button className="text-button" onClick={() => openMemory(m.id)}>
            查看自我记忆 #{m.id}
          </button>
        </article>
      ))}
      {list.data?.total === 0 && (
        <Empty title="暂无有效自我记忆">
          在持续对话中积累经历，或在设置中完善初始背景。
        </Empty>
      )}
      {list.data && (
        <Pagination
          total={list.data.total}
          offset={offset}
          change={setOffset}
        />
      )}
    </section>
  );
}
export function PersonaAttempts() {
  const [offset, setOffset] = useState(0);
  const list = useData<Page<PersonaAttempt>>(
    `/persona/attempts?limit=30&offset=${offset}`,
    2000,
  );
  return (
    <section className="panel persona-attempts">
      <h2>生成记录</h2>
      <p>包括未生成版本的跳过、冲突与失败；生成被拒绝不改变当前 persona。</p>
      {list.error && (
        <Notice error>
          {list.error}
          <button onClick={list.refresh}>重试生成记录</button>
        </Notice>
      )}
      {list.loading && <p>正在读取生成记录…</p>}
      {list.data?.items.map((a) => (
        <article className="persona-history-row" key={a.id}>
          <h3>
            任务 #{a.id} · {attemptLabel(a.state, a.stage)}
          </h3>
          <p>
            {personaSources[a.source] || a.source} · 基于 v{a.base_version_id}
          </p>
          <p className="muted">
            接受：
            <RoleTime value={a.created_at} /> · 结束：
            <RoleTime value={a.finished_at} />
          </p>
          {a.reason && <p>{personaReason(a.reason)}</p>}
          {a.version_id && (
            <a href={`#/persona?tab=history&version=${a.version_id}`}>
              查看版本 v{a.version_id}
            </a>
          )}
          <p>
            <a
              href={`#/operations?object_type=persona_attempt&object_id=${a.id}`}
            >
              查看任务操作记录
            </a>
          </p>
        </article>
      ))}
      {list.data?.total === 0 && <Empty title="暂无生成任务" />}
      {list.data && (
        <Pagination
          total={list.data.total}
          offset={offset}
          change={setOffset}
        />
      )}
    </section>
  );
}
