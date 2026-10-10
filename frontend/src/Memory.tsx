import { MessageMedia } from "./Media";
import { MemorySuggestions } from "./MemorySuggestions";
import { memoryVisibilityLabel } from "./Visibility";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api, json, useData, errorText } from "./api";
import {
  Badge,
  Dialog,
  Empty,
  Notice,
  Pagination,
  lifecycleLabel,
  time,
  fullTime,
} from "./ui";
import {
  MemoryControls,
  PurgeSummary,
  UpcomingDeletion,
} from "./MemoryLifecycle";
import type {
  Entry,
  Memory,
  MemoryDetail as Detail,
  Person,
  Revision,
  Source,
  PurgeResult,
} from "./types";

const kinds = ["事件", "事实", "偏好", "关系", "观点", "计划", "自我", "其他"];
export function MemoryPage({
  openMemory,
  version,
  onChange,
  initialQuery,
}: {
  openMemory: (id: number) => void;
  version: number;
  onChange: () => void;
  initialQuery?: string;
}) {
  const [tab, setTab] = useState("all");
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">留下的经历，有迹可循</p>
          <h1>记忆</h1>
          <p>查看内容、追溯来源，管理记忆的保留与遗忘。</p>
        </div>
      </div>
      <div className="learning-tabs" aria-label="记忆视图">
        <button aria-pressed={tab === "all"} onClick={() => setTab("all")}>
          记忆列表
        </button>
        <button
          aria-pressed={tab === "upcoming"}
          onClick={() => setTab("upcoming")}
        >
          即将删除
        </button>
      </div>
      {tab === "all" ? (
        <MemoryList
          openMemory={openMemory}
          version={version}
          initialQuery={initialQuery}
        />
      ) : (
        <UpcomingDeletion
          openMemory={openMemory}
          version={version}
          onChange={onChange}
        />
      )}
    </>
  );
}
export function MemoryList({
  openMemory,
  version,
  initialQuery = "",
}: {
  openMemory: (id: number) => void;
  version: number;
  initialQuery?: string;
}) {
  const catalog = useData<{ people: Person[]; entries: Entry[] }>("/catalog");
  const initial = new URLSearchParams(initialQuery);
  const [filters, setFilters] = useState({
    text: "",
    person_id: initial.get("person_id") || "",
    kind: "",
    entry_id: "",
    time_from: "",
    time_to: "",
    lifecycle: initial.get("lifecycle") === "all" ? "all" : "active",
    pinned: "",
    sort: "time",
  });
  const [query, setQuery] = useState(() =>
    new URLSearchParams(
      Object.entries(filters).filter(([, v]) => !!v),
    ).toString(),
  );
  const [offset, setOffset] = useState(0);
  const [filterError, setFilterError] = useState("");
  const list = useData<{ items: Memory[]; total: number }>(
    `/memories?${query}&limit=30&offset=${offset}`,
  );
  useEffect(() => {
    list.refresh();
  }, [version, list.refresh]);
  useEffect(() => {
    if (list.data && offset && offset >= list.data.total)
      setOffset(Math.max(0, Math.ceil(list.data.total / 30) * 30 - 30));
  }, [list.data, offset]);
  function search(e: FormEvent) {
    e.preventDefault();
    if (
      filters.time_from &&
      filters.time_to &&
      filters.time_from > filters.time_to
    ) {
      setFilterError("开始日期不能晚于结束日期");
      return;
    }
    setFilterError("");
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
                <option value="forgotten">已遗忘</option>
                <option value="deleted">已删除（历史）</option>
                <option value="all">全部状态</option>
              </select>
            </label>
            <label>
              置顶筛选
              <select
                aria-label="置顶筛选"
                value={filters.pinned}
                onChange={(e) => field("pinned", e.target.value)}
              >
                <option value="">全部</option>
                <option value="true">已置顶</option>
                <option value="false">未置顶</option>
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
      {filterError && <Notice error>{filterError}</Notice>}
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
              <Badge tone={m.pinned ? "purple" : ""}>
                {m.pinned ? "已置顶" : "未置顶"}
              </Badge>
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
                相信 {m.belief} · 保留强度 {m.retention}
              </span>
              <time>{time(m.updated_at)}</time>
              <span aria-hidden>↗</span>
            </div>
          </button>
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
  const catalog = useData<{ entries: Person[] }>("/catalog");
  const [saved, setSaved] = useState<Detail | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [purgeResult, setPurgeResult] = useState<PurgeResult | null>(null);
  const [controlsVersion, setControlsVersion] = useState(0);
  const detail = purgeResult ? null : saved || query.data;
  const navigateMemory = (memoryId: number) => {
    if (!busy) openMemory(memoryId);
  };
  function failed(e: unknown) {
    if (e instanceof ApiError && e.code === "revision_conflict") {
      setConflict(true);
      setError(
        "其他操作已更新了这条记忆。草稿暂时保留；请刷新并载入最新修订，再核对后操作。载入会替换当前草稿。",
      );
    } else if (e instanceof ApiError && e.status === 404) {
      setConflict(true);
      setError("这条记忆已删除或已彻底清除，请刷新或返回列表查看。");
    } else setError(errorText(e));
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    if (!detail || busy || conflict) return;
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
    if (!detail || busy || conflict) return;
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
      setControlsVersion((v) => v + 1);
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
      setControlsVersion((v) => v + 1);
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
    <Dialog title={`记忆 #${id}`} onClose={onClose} closeDisabled={busy}>
      {(error || query.error) && <Notice error>{error || query.error}</Notice>}
      {conflict && (
        <button className="secondary" disabled={busy} onClick={reload}>
          载入最新修订
        </button>
      )}
      {purgeResult && <PurgeSummary result={purgeResult} />}
      {!detail && !purgeResult && !query.error && (
        <p className="quiet">正在读取详情…</p>
      )}
      {detail && (
        <div className="memory-detail">
          <div className="detail-badges">
            <Badge tone="purple">{detail.kind}</Badge>
            <Badge>{detail.stance}</Badge>
            <Badge tone={detail.lifecycle === "deleted" ? "danger" : ""}>
              {lifecycleLabel(detail.lifecycle)}
            </Badge>
            <Badge tone={detail.pinned ? "purple" : ""}>
              {detail.pinned ? "已置顶" : "未置顶"}
            </Badge>
            <span className="muted">修订 {detail.revision}</span>
          </div>
          {detail.merged_into != null && (
            <Notice>
              <button
                className="text-button"
                disabled={busy}
                onClick={() => navigateMemory(detail.merged_into!)}
              >
                已合并到 #{detail.merged_into}
              </button>
              <p>
                全部来源已归入保留记忆；此处是原记忆占位，不能恢复为有效记忆。
              </p>
            </Notice>
          )}
          {detail.lifecycle === "deleted" && detail.merged_into == null && (
            <Notice>
              此标识已撤销，不再参与召回；来源和修订历史仍然保留。
            </Notice>
          )}
          {editing && detail.lifecycle !== "deleted" ? (
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
          <MemoryControls
            key={`${detail.id}:${detail.revision}:${controlsVersion}`}
            detail={detail}
            busy={busy}
            blocked={conflict || editing || deleting}
            setBusy={setBusy}
            failed={failed}
            saved={(latest) => {
              setSaved(latest);
              setError("");
              onChange();
            }}
            purged={(result) => {
              setPurgeResult(result);
              setSaved(null);
              setError("");
              onChange();
            }}
            recreated={(newId) => {
              onChange();
              openMemory(newId);
            }}
          />
          <dl className="metadata">
            <div>
              <dt>记忆可见范围</dt>
              <dd>
                {memoryVisibilityLabel(
                  detail.visibility,
                  catalog.data?.entries || [],
                )}
              </dd>
            </div>
            {detail.lifecycle === "forgotten" && (
              <div>
                <dt>遗忘时间</dt>
                <dd>{fullTime(detail.forgotten_at)}</dd>
              </div>
            )}
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
          <MemorySuggestions
            key={`suggestions-${detail.id}:${detail.revision}:${controlsVersion}`}
            detail={detail}
            busy={busy}
            blocked={conflict || editing || deleting}
            setBusy={setBusy}
            saved={(latest) => {
              setSaved(latest);
              setError("");
              onChange();
            }}
            failed={failed}
            openMemory={navigateMemory}
            close={onClose}
          />
          <section className="detail-section">
            <h3>来源与前后文</h3>
            {detail.sources.length ? (
              detail.sources.map((s) => (
                <SourceView key={s.id} source={s} openMemory={navigateMemory} />
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
                  disabled={busy}
                  onClick={() => navigateMemory(m.id)}
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
          <a
            className="operation-link"
            href={`#/operations?object_type=memory&object_id=${id}`}
            onClick={(e) => {
              if (busy) e.preventDefault();
              else onClose();
            }}
          >
            查看此记忆的操作记录
          </a>
          {detail.lifecycle !== "deleted" && (
            <div className="detail-footer">
              {deleting ? (
                <div className="delete-confirm">
                  <p>
                    确认删除这条记忆？此标识将永久撤销，共享的来源消息与其他记忆保留。
                  </p>
                  <div className="actions">
                    <button
                      className="danger-button"
                      disabled={busy || conflict}
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
                    disabled={busy || conflict || editing}
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
                    disabled={busy || conflict || editing}
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
export function SourceView({
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
          <MessageMedia media={s.message.media} />
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
                <MessageMedia media={m.media} />
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
        {time(r.created_at)} ·{" "}
        {(
          { admin: "管理员", learning: "学习", maintenance: "维护" } as Record<
            string,
            string
          >
        )[r.actor] || r.actor}{" "}
        · 修订 {r.revision_before} → {r.revision_after}
      </small>
      <p>
        <span className="muted">修改前</span> {String(r.before.content || "")}
      </p>
      <p className="muted">
        相信程度 {String(r.before.belief ?? "—")} →{" "}
        {String(r.after.belief ?? "—")} · 重要度{" "}
        {String(r.before.importance ?? "—")} →{" "}
        {String(r.after.importance ?? "—")}
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
