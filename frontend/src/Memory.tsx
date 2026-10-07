import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api, json, useData, errorText } from "./api";
import { Badge, Dialog, Empty, Notice, lifecycleLabel, time } from "./ui";
import type {
  Entry,
  Memory,
  MemoryDetail as Detail,
  Person,
  Revision,
  Source,
} from "./types";

const kinds = ["事件", "事实", "偏好", "关系", "观点", "计划", "自我", "其他"];
export function MemoryList({
  openMemory,
  version,
}: {
  openMemory: (id: number) => void;
  version: number;
}) {
  const catalog = useData<{ people: Person[]; entries: Entry[] }>("/catalog");
  const [filters, setFilters] = useState({
    text: "",
    person_id: "",
    kind: "",
    entry_id: "",
    time_from: "",
    time_to: "",
    lifecycle: "active",
    sort: "time",
  });
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const list = useData<{ items: Memory[]; total: number }>(
    `/memories?${query}&limit=30&offset=${offset}`,
  );
  useEffect(() => {
    list.refresh();
  }, [version, list.refresh]);
  function search(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
    setQuery(
      new URLSearchParams(
        Object.entries(filters).filter(([, v]) => !!v),
      ).toString(),
    );
    list.refresh();
  }
  const field = (key: keyof typeof filters, value: string) =>
    setFilters({ ...filters, [key]: value });
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">留下的经历，有迹可循</p>
          <h1>记忆</h1>
          <p>查看内容、追溯来源，或修正一处记录。</p>
        </div>
        <Badge>{list.data?.total ?? "—"} 条记忆</Badge>
      </div>
      <section className="panel">
        <form className="memory-filters" onSubmit={search}>
          <div className="search-row">
            <label className="search-field">
              搜索记忆
              <input
                value={filters.text}
                maxLength={1000}
                placeholder="输入正文或标签中的词…"
                onChange={(e) => field("text", e.target.value)}
              />
            </label>
            <button className="primary" type="submit">
              搜索
            </button>
          </div>
          <div className="filter-grid">
            <label>
              涉及的人
              <select
                value={filters.person_id}
                onChange={(e) => field("person_id", e.target.value)}
              >
                <option value="">全部人物</option>
                {catalog.data?.people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {(catalog.data?.people.filter((x) => x.name === p.name)
                      .length || 0) > 1
                      ? ` · ${p.id.slice(0, 6)}`
                      : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              记忆类型
              <select
                value={filters.kind}
                onChange={(e) => field("kind", e.target.value)}
              >
                <option value="">全部类型</option>
                {kinds.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
            </label>
            <label>
              出处入口
              <select
                value={filters.entry_id}
                onChange={(e) => field("entry_id", e.target.value)}
              >
                <option value="">全部入口</option>
                {catalog.data?.entries.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              状态
              <select
                value={filters.lifecycle}
                onChange={(e) => field("lifecycle", e.target.value)}
              >
                <option value="active">有效</option>
                <option value="forgotten">已遗忘（只读）</option>
                <option value="deleted">已删除（历史）</option>
                <option value="all">全部状态</option>
              </select>
            </label>
            <label>
              开始日期
              <input
                type="date"
                value={filters.time_from}
                onChange={(e) => field("time_from", e.target.value)}
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                value={filters.time_to}
                onChange={(e) => field("time_to", e.target.value)}
              />
            </label>
            <label>
              排序方式
              <select
                value={filters.sort}
                onChange={(e) => field("sort", e.target.value)}
              >
                <option value="time">更新时间 · 从新到旧</option>
                <option value="relevance">文本相关度</option>
                <option value="retention">保留强度 · 从高到低</option>
              </select>
            </label>
          </div>
          <p className="fine-print">
            日期按事件时间筛选；没有事件时间时使用创建时间。
          </p>
        </form>
      </section>
      {(list.error || catalog.error) && (
        <Notice error>{list.error || catalog.error}</Notice>
      )}
      <div className="list-heading">
        <span>{list.data ? `共 ${list.data.total} 条` : "正在读取…"}</span>
        <span>浏览列表不会增加召回或使用次数</span>
      </div>
      {list.data?.items.length === 0 && (
        <section className="panel">
          <Empty title="没有找到记忆">尝试换一个词，或调整筛选条件。</Empty>
        </section>
      )}
      <div className="memory-grid">
        {list.data?.items.map((m) => (
          <button
            key={m.id}
            className="memory-card panel"
            onClick={() => openMemory(m.id)}
          >
            <div className="memory-card-top">
              <Badge tone="purple">{m.kind}</Badge>
              <span className="muted">
                #{m.id} · {lifecycleLabel(m.lifecycle)}
              </span>
            </div>
            <p className="memory-content">{m.content}</p>
            <div className="person-line">
              {m.about.map((p) => p.name).join("、") || "未标注涉及人物"}
            </div>
            <div className="memory-card-bottom">
              <span>
                相信 {m.belief} · 保留 {m.retention}
              </span>
              <time>{time(m.updated_at)}</time>
              <span aria-hidden>↗</span>
            </div>
          </button>
        ))}
      </div>
      {list.data && list.data.total > 30 && (
        <div className="pagination">
          <button
            className="secondary"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 30))}
          >
            上一页
          </button>
          <span>第 {Math.floor(offset / 30) + 1} 页</span>
          <button
            className="secondary"
            disabled={offset + 30 >= list.data.total}
            onClick={() => setOffset(offset + 30)}
          >
            下一页
          </button>
        </div>
      )}
    </>
  );
}

export function MemoryDetail({
  id,
  onClose,
  onChange,
  openMemory,
}: {
  id: number;
  onClose: () => void;
  onChange: () => void;
  openMemory: (id: number) => void;
}) {
  const query = useData<Detail>(`/memories/${id}`);
  const [saved, setSaved] = useState<Detail | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const detail = saved || query.data;
  function failed(e: unknown) {
    if (e instanceof ApiError && e.code === "revision_conflict") {
      setConflict(true);
      setError(
        "其他操作已更新了这条记忆。你的草稿已保留；请载入最新修订后再编辑。",
      );
    } else setError(errorText(e));
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    if (!detail) return;
    setBusy(true);
    setError("");
    try {
      setSaved(
        await api<Detail>(
          `/memories/${id}`,
          json("PATCH", { expected_revision: detail.revision, content: draft }),
        ),
      );
      setEditing(false);
      setConflict(false);
      onChange();
    } catch (e) {
      failed(e);
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!detail) return;
    setBusy(true);
    setError("");
    try {
      setSaved(
        await api<Detail>(
          `/memories/${id}`,
          json("DELETE", { expected_revision: detail.revision }),
        ),
      );
      setDeleting(false);
      setEditing(false);
      onChange();
    } catch (e) {
      failed(e);
    } finally {
      setBusy(false);
    }
  }
  async function reload() {
    setBusy(true);
    try {
      const latest = await api<Detail>(`/memories/${id}`);
      setSaved(latest);
      setDraft(latest.content);
      setConflict(false);
      setError("");
      setDeleting(false);
    } catch (e) {
      failed(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={`记忆 #${id}`} onClose={onClose}>
      {(error || query.error) && <Notice error>{error || query.error}</Notice>}
      {conflict && (
        <button className="secondary" disabled={busy} onClick={reload}>
          载入最新修订
        </button>
      )}
      {!detail && !query.error && <p className="quiet">正在读取详情…</p>}
      {detail && (
        <div className="memory-detail">
          <div className="detail-badges">
            <Badge tone="purple">{detail.kind}</Badge>
            <Badge>{detail.stance}</Badge>
            <Badge tone={detail.lifecycle === "deleted" ? "danger" : ""}>
              {lifecycleLabel(detail.lifecycle)}
            </Badge>
            <span className="muted">修订 {detail.revision}</span>
          </div>
          {detail.lifecycle === "deleted" && (
            <Notice>
              此标识已撤销，不再参与召回；来源和修订历史仍然保留。
            </Notice>
          )}
          {editing && detail.lifecycle === "active" ? (
            <form onSubmit={save}>
              <label>
                记忆正文
                <textarea
                  value={draft}
                  maxLength={1000}
                  rows={5}
                  onChange={(e) => setDraft(e.target.value)}
                />
              </label>
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy || conflict || !draft.trim()}
                >
                  保存修改
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() => setEditing(false)}
                >
                  取消编辑
                </button>
                <small>{draft.length} / 1000 字</small>
              </div>
            </form>
          ) : (
            <p className="detail-content">{detail.content}</p>
          )}
          <div className="score-grid">
            <div>
              <strong>{detail.belief}</strong>
              <span>相信程度</span>
            </div>
            <div>
              <strong>{detail.importance}</strong>
              <span>重要度</span>
            </div>
            <div>
              <strong>{detail.retention}</strong>
              <span>保留强度</span>
            </div>
          </div>
          <dl className="metadata">
            <div>
              <dt>说话人</dt>
              <dd>{detail.speaker.name}</dd>
            </div>
            <div>
              <dt>涉及的人</dt>
              <dd>{detail.about.map((p) => p.name).join("、") || "未标注"}</dd>
            </div>
            <div>
              <dt>事件时间</dt>
              <dd>{detail.event_time || "未标注"}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{time(detail.updated_at)}</dd>
            </div>
            <div>
              <dt>被召回</dt>
              <dd>{detail.recall_count} 次</dd>
            </div>
            <div>
              <dt>被使用</dt>
              <dd>{detail.used_count} 次</dd>
            </div>
          </dl>
          <p className="fine-print">
            被召回不等于被使用；使用次数不代表内容可信度。
          </p>
          {!!detail.tags.length && (
            <div className="tag-list">
              {detail.tags.map((t) => (
                <Badge key={t}># {t}</Badge>
              ))}
            </div>
          )}
          <section className="detail-section">
            <h3>来源与前后文</h3>
            {detail.sources.length ? (
              detail.sources.map((s) => (
                <SourceView key={s.id} source={s} openMemory={openMemory} />
              ))
            ) : (
              <p className="quiet">没有保存的来源消息。</p>
            )}
          </section>
          <section className="detail-section">
            <h3>基于它形成的记忆</h3>
            {detail.derived_memories.length ? (
              detail.derived_memories.map((m) => (
                <button
                  key={m.id}
                  className="memory-preview"
                  onClick={() => openMemory(m.id)}
                >
                  <span>
                    #{m.id} · {m.content}
                  </span>
                  <small>
                    {lifecycleLabel(m.lifecycle)}
                    {m.needs_review ? " · 来源已变化，待复核" : ""}
                  </small>
                </button>
              ))
            ) : (
              <p className="quiet">暂无派生记忆。</p>
            )}
          </section>
          <details className="history">
            <summary>
              修订历史 <Badge>{detail.revisions.length}</Badge>
            </summary>
            {detail.revisions.length ? (
              detail.revisions.map((r) => (
                <RevisionView key={r.id} revision={r} />
              ))
            ) : (
              <p className="quiet">还没有修订记录。</p>
            )}
          </details>
          <details className="history">
            <summary>
              人工操作记录 <Badge>{detail.operations.length}</Badge>
            </summary>
            {detail.operations.map((o) => (
              <p key={o.id} className="operation">
                {time(o.created_at)} · 管理员 ·{" "}
                {o.action === "delete" ? "删除记忆" : "编辑正文"}
              </p>
            ))}
            {!detail.operations.length && (
              <p className="quiet">暂无人工操作。</p>
            )}
          </details>
          {detail.lifecycle === "active" && (
            <div className="detail-footer">
              {deleting ? (
                <div className="delete-confirm">
                  <p>
                    确认删除这条记忆？此标识将永久撤销，共享的来源消息与其他记忆保留。
                  </p>
                  <div className="actions">
                    <button
                      className="danger-button"
                      disabled={busy}
                      onClick={remove}
                    >
                      确认删除
                    </button>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => setDeleting(false)}
                    >
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <div className="actions">
                  <button
                    className="secondary"
                    disabled={busy || editing}
                    onClick={() => {
                      setEditing(true);
                      setDraft(detail.content);
                      setError("");
                    }}
                  >
                    编辑正文
                  </button>
                  <button
                    className="text-button danger-text"
                    onClick={() => setDeleting(true)}
                  >
                    删除记忆
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
function SourceView({
  source: s,
  openMemory,
}: {
  source: Source;
  openMemory: (id: number) => void;
}) {
  return (
    <details className="source">
      <summary>
        {s.kind === "message"
          ? `来源消息 · ${s.message?.sender_name || "原文已不可用"}`
          : s.kind === "memory"
            ? `来源记忆 #${s.memory?.id || ""}`
            : "初始背景"}
      </summary>
      {s.message && (
        <>
          <p className="muted">
            {s.message.entry_name || s.message.entry_id} ·{" "}
            {time(s.message.occurred_at)}
          </p>
          <blockquote>{s.message.content}</blockquote>
          <details className="source-context">
            <summary>查看前后文</summary>
            {s.context?.map((m) => (
              <div
                key={m.id}
                className={`context-message ${m.id === s.message_id ? "highlight" : ""}`}
              >
                <small>
                  {m.sender_name} · {time(m.occurred_at)}
                </small>
                <p>{m.content}</p>
              </div>
            ))}
          </details>
        </>
      )}
      {s.memory && (
        <>
          <button
            className="memory-preview"
            onClick={() => openMemory(s.memory!.id)}
          >
            {s.memory.content}
          </button>
          <p className="fine-print">
            依据修订 {s.source_revision} · {lifecycleLabel(s.memory.lifecycle)}
            {s.needs_review ? " · 来源已变化，待复核" : ""}
          </p>
        </>
      )}
      {s.kind === "initial_setting" && <p>{s.note || "角色初始背景材料"}</p>}
    </details>
  );
}
function RevisionView({ revision: r }: { revision: Revision }) {
  return (
    <article className="revision">
      <small>
        {time(r.created_at)} · {r.actor === "admin" ? "管理员" : "学习"} · 修订{" "}
        {r.revision_before} → {r.revision_after}
      </small>
      <p>
        <span className="muted">修改前</span> {String(r.before.content || "")}
      </p>
      <p>
        <span className="muted">修改后</span>{" "}
        {r.after.lifecycle === "deleted"
          ? "已删除（撤销对象）"
          : String(r.after.content || "")}
      </p>
    </article>
  );
}
