import { useEffect, useRef, useState } from "react";
import { ApiError, errorText, useData } from "./api";
import { Badge, Notice, fullTime, lifecycleLabel } from "./ui";
import { RevisionView, SourceView } from "./Memory";
import type { ProfileCategory, ProfileMemory } from "./entry-profile-types";
import type { MemoryDetail } from "./types";
import type { ProfileWrite } from "./EntryProfile";

const categories = { topic: "话题", saying: "说法", rule: "群规" };
const categoryLabel = (category: ProfileCategory) =>
  category ? categories[category] : "管理员说明";

export function ProfileMemoryCreate({
  entry,
  blocked,
  busy,
  write,
  cancel,
  created,
}: {
  entry: { id: string; name: string };
  blocked: boolean;
  busy: boolean;
  write: ProfileWrite;
  cancel: () => void;
  created: () => void;
}) {
  const [category, setCategory] = useState<ProfileCategory>("rule"),
    [content, setContent] = useState("");
  const [error, setError] = useState("");
  return (
    <form
      className="profile-memory-card"
      aria-label="人工新建画像记忆"
      onSubmit={async (e) => {
        e.preventDefault();
        if (blocked || !content.trim() || Array.from(content).length > 1000)
          return;
        setError("");
        try {
          await write("/memories", "POST", {
            kind: "入口画像",
            owner_entry_id: entry.id,
            category,
            content,
          });
          created();
        } catch (e) {
          setError(errorText(e));
        }
      }}
    >
      <h4>人工新建画像记忆</h4>
      <p>仅用于入口“{entry.name}”。人工内容无需模型依据。</p>
      {error && <Notice error>{error}</Notice>}
      <label>
        子类
        <select
          aria-label="子类"
          value={category || ""}
          disabled={busy}
          onChange={(e) =>
            setCategory((e.target.value || null) as ProfileCategory)
          }
        >
          {Object.entries(categories).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
          <option value="">管理员说明</option>
        </select>
      </label>
      <label>
        新画像正文
        <textarea
          aria-label="新画像正文"
          rows={4}
          maxLength={1000}
          value={content}
          disabled={busy}
          onChange={(e) => setContent(e.target.value)}
        />
      </label>
      <p className="fine-print">{Array.from(content).length} / 1000 字</p>
      <div className="button-row">
        <button
          disabled={
            blocked || !content.trim() || Array.from(content).length > 1000
          }
        >
          创建画像记忆
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={cancel}
        >
          取消新建
        </button>
      </div>
    </form>
  );
}

function ProfileMemoryEvidence({
  id,
  revision,
  message,
  openMemory,
  speakerNames,
}: {
  id: number;
  speakerNames: Record<string, string>;
  revision: number;
  message: number | null;
  openMemory: (id: number) => void;
}) {
  const detail = useData<MemoryDetail>(`/memories/${id}`);
  const target = useRef<HTMLElement>(null);
  useEffect(() => {
    detail.refresh();
  }, [revision, detail.refresh]);
  useEffect(() => {
    if (detail.data && message !== null) {
      target.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
      target.current?.focus({ preventScroll: true });
    }
  }, [detail.data, message]);
  return (
    <div className="profile-memory-evidence">
      {detail.error && (
        <Notice error>
          {detail.error}
          <button onClick={detail.refresh}>重试来源与修订</button>
        </Notice>
      )}
      {!detail.data ? (
        !detail.error && <p>正在读取来源与修订…</p>
      ) : (
        <>
          <h4>来源消息与前后文</h4>
          {!detail.data.sources.length && <p>没有保存的来源消息。</p>}
          {detail.data.sources.map((source) => (
            <section
              key={source.id}
              ref={source.message_id === message ? target : null}
              tabIndex={-1}
              className={
                source.message_id === message ? "profile-source-target" : ""
              }
              aria-label={
                source.message_id
                  ? `来源消息 #${source.message_id}`
                  : `来源 #${source.id}`
              }
            >
              {source.message_id && <h5>消息 #{source.message_id}</h5>}
              <SourceView
                key={`${source.id}:${source.message_id === message}`}
                source={source}
                openMemory={openMemory}
                initiallyOpen={source.message_id === message}
                speakerNames={speakerNames}
              />
            </section>
          ))}
          {message !== null &&
            !detail.data.sources.some((s) => s.message_id === message) && (
              <Notice>该来源已变化或不可用，请刷新画像核对。</Notice>
            )}
          <h4>修订历史 · {detail.data.revisions.length}</h4>
          {detail.data.revisions.length ? (
            detail.data.revisions.map((r) => (
              <RevisionView key={r.id} revision={r} />
            ))
          ) : (
            <p>还没有修订记录。</p>
          )}
        </>
      )}
    </div>
  );
}

export function ProfileMemoryCard({
  memory,
  speakerNames,
  displayed,
  blocked,
  busy,
  write,
  refresh,
  openMemory,
}: {
  memory: ProfileMemory;
  speakerNames: Record<string, string>;
  displayed: boolean;
  blocked: boolean;
  busy: boolean;
  write: ProfileWrite;
  refresh: () => void;
  openMemory: (id: number) => void;
}) {
  const [draft, setDraft] = useState<{
    content: string;
    revision: number;
  } | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [conflict, setConflict] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [details, setDetails] = useState(false),
    [message, setMessage] = useState<number | null>(null);
  const stale = !!draft && draft.revision !== memory.revision;
  const deleted = memory.lifecycle === "deleted";
  function failed(e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      setConflict(true);
      setError("这条画像记忆已变化，草稿已保留。请核对最新正文与修订后继续。");
    } else if (e instanceof ApiError && e.status === 404) {
      setConflict(true);
      setError(
        "这条画像记忆已不可用，草稿已保留。请复制需要保留的内容后关闭。",
      );
    } else setError(errorText(e));
  }
  async function change(
    method: string,
    suffix: string,
    body: unknown,
    result: string,
  ) {
    if (blocked) return;
    setError("");
    setNotice("");
    try {
      await write(`/memories/${memory.memory_id}${suffix}`, method, body);
      setDraft(null);
      setDeleting(null);
      setConflict(false);
      setNotice(result);
    } catch (e) {
      failed(e);
    }
  }
  return (
    <article
      id={`profile-memory-${memory.memory_id}`}
      className="profile-memory-card"
      aria-label={`画像记忆 #${memory.memory_id}`}
    >
      <div className="detail-badges">
        <Badge tone="purple">{categoryLabel(memory.category)}</Badge>
        <Badge>{memory.author === "admin" ? "人工" : "模型"}</Badge>
        {memory.manual_protected && <Badge>人工保护</Badge>}
        <Badge tone={deleted ? "danger" : ""}>
          {lifecycleLabel(memory.lifecycle)}
        </Badge>
        <span className="muted">
          #{memory.memory_id} · 修订 {memory.revision}
        </span>
      </div>
      <p className="entry-profile-content">{memory.body}</p>
      <dl className="profile-memory-metadata">
        <div>
          <dt>保留强度</dt>
          <dd>
            {memory.retention}
            {memory.pinned ? " · 已置顶" : ""}
          </dd>
        </div>
        <div>
          <dt>最近确认时间</dt>
          <dd>
            {memory.last_confirmed_at === undefined
              ? "接口未提供"
              : memory.last_confirmed_at === null
                ? "暂无确认记录"
                : fullTime(memory.last_confirmed_at)}
          </dd>
        </div>
        <div>
          <dt>宿主投影</dt>
          <dd>
            {displayed
              ? "已加入"
              : !memory.usable
                ? "当前不可用"
                : "当前未加入"}
          </dd>
        </div>
      </dl>
      <div aria-label="依据原话">
        {memory.sources.map((source, i) => (
          <blockquote key={`${source.ref}:${i}`}>
            <p>{source.excerpt}</p>
            <footer>
              {source.field === "quote" ? "转引原话" : "消息正文"} · 原作者：
              {speakerNames[source.speaker] || source.speaker} ·{" "}
              {fullTime(source.message.occurred_at)}
            </footer>
            <button
              className="text-button"
              disabled={busy}
              onClick={() => {
                setMessage(source.message.position);
                setDetails(true);
              }}
            >
              跳转来源消息 #{source.message.position}
            </button>
          </blockquote>
        ))}
        {!memory.sources.length && (
          <p className="muted">
            {memory.author === "admin"
              ? "管理员手写，无需模型依据。"
              : "当前没有可用的绑定原话，可在来源与修订历史中核对。"}
          </p>
        )}
      </div>
      {error && (
        <Notice error>
          {error}
          <button
            className="text-button"
            disabled={busy}
            onClick={() => {
              refresh();
              if (!draft && deleting === null) {
                setConflict(false);
                setError("");
              }
            }}
          >
            刷新画像记忆
          </button>
        </Notice>
      )}
      {notice && <Notice>{notice}</Notice>}
      {draft && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (
              !blocked &&
              !conflict &&
              !stale &&
              !deleted &&
              draft.content.trim()
            )
              void change(
                "PATCH",
                "",
                { expected_revision: draft.revision, content: draft.content },
                "修改已保存。",
              );
          }}
        >
          <label>
            画像正文
            <textarea
              aria-label="画像正文"
              value={draft.content}
              disabled={busy}
              rows={4}
              maxLength={1000}
              onChange={(e) => setDraft({ ...draft, content: e.target.value })}
            />
          </label>
          <p className="fine-print">
            草稿基于修订 {draft.revision} · {Array.from(draft.content).length} /
            1000 字
          </p>
          {(stale || conflict) && (
            <Notice>
              草稿未被替换。上方显示当前修订 {memory.revision}{" "}
              的正文，请核对后继续。
              <button
                type="button"
                disabled={blocked || deleted || (conflict && !stale)}
                onClick={() => {
                  setDraft({ ...draft, revision: memory.revision });
                  setConflict(false);
                  setError("");
                }}
              >
                已核对最新修订，保留草稿继续
              </button>
            </Notice>
          )}
          <div className="button-row">
            <button
              disabled={
                blocked ||
                conflict ||
                stale ||
                deleted ||
                !draft.content.trim() ||
                Array.from(draft.content).length > 1000
              }
            >
              保存修改
            </button>
            <button
              className="secondary"
              type="button"
              disabled={busy}
              onClick={() => {
                setDraft(null);
                setConflict(false);
                setError("");
              }}
            >
              取消编辑
            </button>
          </div>
        </form>
      )}
      {deleting !== null && (
        <div className="delete-confirm">
          <p>
            确认删除这条画像记忆？该标识将永久撤销，不能恢复；来源和修订历史保留。
          </p>
          {deleting !== memory.revision && (
            <Notice>这条记忆已变化，请取消并核对最新正文后重新操作。</Notice>
          )}
          <div className="button-row">
            <button
              className="danger-button"
              disabled={
                blocked || conflict || deleted || deleting !== memory.revision
              }
              onClick={() =>
                void change(
                  "DELETE",
                  "",
                  { expected_revision: deleting },
                  "画像记忆已删除，历史仍保留。",
                )
              }
            >
              确认删除
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => {
                setDeleting(null);
                setConflict(false);
                setError("");
              }}
            >
              取消删除
            </button>
          </div>
        </div>
      )}
      {deleted && (
        <p className="muted">
          已删除的记忆标识不可恢复。需要重新手写时，请使用人工新建。
        </p>
      )}
      <div className="button-row">
        {!deleted && (
          <button
            className="secondary"
            disabled={blocked || !!draft || deleting !== null}
            onClick={() => {
              setDraft({ content: memory.body, revision: memory.revision });
              setConflict(false);
              setError("");
              setNotice("");
            }}
          >
            编辑
          </button>
        )}
        {!deleted && (
          <button
            className="text-button danger-text"
            disabled={blocked || !!draft || deleting !== null}
            onClick={() => {
              setDeleting(memory.revision);
              setConflict(false);
              setError("");
              setNotice("");
            }}
          >
            删除
          </button>
        )}
        {memory.lifecycle === "forgotten" && (
          <button
            className="secondary"
            disabled={blocked || !!draft || deleting !== null || conflict}
            onClick={() =>
              void change(
                "POST",
                "/restore",
                { expected_revision: memory.revision },
                "画像记忆已恢复。",
              )
            }
          >
            恢复
          </button>
        )}
        <button
          className="text-button"
          aria-expanded={details}
          disabled={busy}
          onClick={() => {
            setDetails(!details);
            setMessage(null);
          }}
        >
          {details ? "收起来源与修订历史" : "来源与修订历史"}
        </button>
        <button
          className="text-button"
          disabled={busy || !!draft}
          onClick={() => openMemory(memory.memory_id)}
        >
          打开记忆详情
        </button>
      </div>
      {details && (
        <ProfileMemoryEvidence
          id={memory.memory_id}
          speakerNames={speakerNames}
          revision={memory.revision}
          message={message}
          openMemory={openMemory}
        />
      )}
    </article>
  );
}
