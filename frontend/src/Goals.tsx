import { useState, type FormEvent } from "react";
import { api, errorText, json, useData } from "./api";
import { Dialog, Empty, Notice, Pagination } from "./ui";
import GoalDetails from "./GoalDetails";
import GoalForm from "./GoalForm";
import {
  GoalBadges,
  GoalFacts,
  GoalReceiptNotice,
  goalStates,
  goalKinds,
} from "./goal-ui";
import type { Goal, GoalCatalog, GoalReceipt, Page } from "./types";

const defaults = {
  state: "open",
  kind: "",
  overdue: "",
  due_soon: "",
  possible_duplicate: "",
  entry_id: "",
  deadline_from: "",
  deadline_to: "",
};
const encode = (filters: typeof defaults) =>
  new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v !== ""),
  ).toString();
export default function Goals({
  initialId = null,
  openMemory,
}: {
  initialId?: number | null;
  openMemory: (id: number) => void;
}) {
  const catalog = useData<GoalCatalog>("/catalog");
  const [selected, setSelected] = useState<number | null>(initialId);
  const [filters, setFilters] = useState(defaults),
    [query, setQuery] = useState(encode(defaults)),
    [offset, setOffset] = useState(0);
  const [filterError, setFilterError] = useState("");
  const [creating, setCreating] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [receipt, setReceipt] = useState<GoalReceipt | null>(null);
  const list = useData<Page<Goal>>(
    selected === null ? `/goals?${query}&limit=30&offset=${offset}` : null,
    offset === 0 ? 2000 : 0,
  );
  const change = (key: keyof typeof defaults, value: string) =>
    setFilters((f) => ({ ...f, [key]: value }));
  function filter(e: FormEvent) {
    e.preventDefault();
    if (
      filters.deadline_from &&
      filters.deadline_to &&
      filters.deadline_from > filters.deadline_to
    ) {
      setFilterError("开始日期不能晚于结束日期");
      return;
    }
    setFilterError("");
    setOffset(0);
    setQuery(encode(filters));
    list.refresh();
  }
  async function create(body: Record<string, unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<GoalReceipt>("/goals", json("POST", body));
      setReceipt(result);
      setCreating(false);
      list.refresh();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="goal-workspace">
      {catalog.error && (
        <Notice error>
          人物与入口名称读取失败：{catalog.error}
          <button className="text-button" onClick={catalog.refresh}>
            重试人物与入口
          </button>
        </Notice>
      )}
      {selected !== null ? (
        <GoalDetails
          key={selected}
          id={selected}
          catalog={catalog.data}
          openMemory={openMemory}
          openGoal={setSelected}
          onBack={() => setSelected(null)}
          onChange={list.refresh}
        />
      ) : (
        <>
          <section className="panel goals-panel">
            <div className="panel-heading">
              <h2>目标与询问</h2>
              <button
                className="primary"
                onClick={() => {
                  setError("");
                  setCreating(true);
                }}
              >
                创建目标或询问
              </button>
            </div>
            <p className="goal-help">
              所有入口共享目标。管理员创建的目标不自动合并，相似项仅标为“可能重复”。已过期只是标记，需要明确完成或放弃。
            </p>
            <form className="goal-filters" onSubmit={filter}>
              <label>
                目标状态
                <select
                  value={filters.state}
                  onChange={(e) => change("state", e.target.value)}
                >
                  <option value="">全部状态</option>
                  {Object.entries(goalStates).map(([v, l]) => (
                    <option value={v} key={v}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                目标类型
                <select
                  value={filters.kind}
                  onChange={(e) => change("kind", e.target.value)}
                >
                  <option value="">全部类型</option>
                  {Object.entries(goalKinds).map(([v, l]) => (
                    <option value={v} key={v}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
              {(
                [
                  ["overdue", "是否过期"],
                  ["due_soon", "是否临近"],
                  ["possible_duplicate", "是否可能重复"],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  {label}
                  <select
                    value={filters[key]}
                    onChange={(e) => change(key, e.target.value)}
                  >
                    <option value="">全部</option>
                    <option value="true">是</option>
                    <option value="false">否</option>
                  </select>
                </label>
              ))}
              <label>
                产生的入口
                <select
                  value={filters.entry_id}
                  onChange={(e) => change("entry_id", e.target.value)}
                >
                  <option value="">全部入口</option>
                  {catalog.data?.entries.map((e) => (
                    <option value={e.id} key={e.id}>
                      {e.name || e.id}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                截止日期从
                <input
                  type="date"
                  value={filters.deadline_from}
                  onChange={(e) => change("deadline_from", e.target.value)}
                />
              </label>
              <label>
                截止日期至
                <input
                  type="date"
                  value={filters.deadline_to}
                  onChange={(e) => change("deadline_to", e.target.value)}
                />
              </label>
              <p className="goal-help full-width">
                日期范围按角色时区解释，结束日期包含当天。临近范围包含已过期目标；选择“是否过期：否”可排除。
              </p>
              <div className="actions full-width">
                <button className="primary">筛选目标</button>
                <button
                  type="button"
                  className="secondary"
                  onClick={list.refresh}
                >
                  刷新目标
                </button>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setFilters(defaults);
                    setQuery(encode(defaults));
                    setOffset(0);
                    setFilterError("");
                    list.refresh();
                  }}
                >
                  重置筛选
                </button>
              </div>
            </form>
          </section>
          {receipt && (
            <GoalReceiptNotice receipt={receipt} openGoal={setSelected} />
          )}
          {(filterError || list.error) && (
            <Notice error>
              {filterError || list.error}
              {list.error &&
                list.data &&
                "。以下为上次读取结果，可能尚未更新。"}
            </Notice>
          )}
          <div className="list-heading">
            <span>
              {list.data
                ? `共 ${list.data.total} 个目标与询问`
                : list.error
                  ? "目标暂不可读取"
                  : "正在读取目标…"}
            </span>
          </div>
          {list.data?.items.length === 0 && (
            <Empty title="当前范围没有目标或询问">
              可以创建目标，或在试用对话中让角色答应一件事，学习后在这里查看。
            </Empty>
          )}
          <div className="goal-grid">
            {list.data?.items.map((goal) => (
              <article className="panel goals-panel" key={goal.id}>
                <GoalBadges goal={goal} />
                <h3 className="goal-content">{goal.content}</h3>
                <GoalFacts goal={goal} catalog={catalog.data} />
                <button
                  className="text-button"
                  onClick={() => setSelected(goal.id)}
                >
                  查看目标 #{goal.id}
                </button>
              </article>
            ))}
          </div>
          {list.data && (
            <Pagination
              total={list.data.total}
              offset={offset}
              change={setOffset}
            />
          )}
        </>
      )}
      {creating && (
        <Dialog
          title="创建目标或询问"
          onClose={() => setCreating(false)}
          closeDisabled={busy}
        >
          <p className="goal-help">
            手动创建只会标记可能重复，保留每一条目标供你核对。
          </p>
          {error && <Notice error>{error}</Notice>}
          <GoalForm
            catalog={catalog.data}
            busy={busy}
            submit={(body) => void create(body)}
            cancel={() => setCreating(false)}
          />
        </Dialog>
      )}
    </div>
  );
}
