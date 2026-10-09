import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, json, useData, errorText } from "./api";
import { Badge, Empty, Notice, time, batchLabel, messageState } from "./ui";
import { EntryQueueWait, paceLabel } from "./EntrySettings";
import { JudgmentSummary, SubjectAnnotations } from "./RecallJudgment";
import { StateSummary } from "./State";
import type {
  Entry,
  TrialCatalog,
  TrialSnapshot,
  Prepared,
  Status,
  Message,
  Person,
} from "./types";

type Props = {
  status: Status | null;
  openMemory: (id: number) => void;
  refreshStatus: () => void;
};
export default function Trial(props: Props) {
  const catalog = useData<TrialCatalog>("/trial");
  const [selected, setSelected] = useState("");
  const [newEntry, setNewEntry] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("group");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const entries = catalog.data?.entries || [];
  const uniqueCatalog = useMemo(() => {
    if (!catalog.data) return null;
    // A merged person can own several trial accounts; the selector uses people.
    const speakers = new Map<string, Person>();
    for (const person of catalog.data.speakers) {
      const previous = speakers.get(person.id);
      speakers.set(person.id, {
        ...person,
        is_default: !!(person.is_default || previous?.is_default),
      });
    }
    return { ...catalog.data, speakers: [...speakers.values()] };
  }, [catalog.data]);
  useEffect(() => {
    if (!selected && entries.length) setSelected(entries[0].id);
  }, [selected, entries]);
  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const entry = await api<Entry>(
        "/trial/entries",
        json("POST", { name, kind }),
      );
      setSelected(entry.id);
      setNewEntry(false);
      setName("");
      catalog.refresh();
      props.refreshStatus();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">在相处中，慢慢记住</p>
          <h1>试用对话</h1>
          <p>聊一件小事，看看它如何成为记忆。</p>
        </div>
        <Badge tone="purple">
          {entries.find((e) => e.id === selected)
            ? `${paceLabel(entries.find((e) => e.id === selected)!.pace)}学习`
            : "试用入口"}
        </Badge>
      </div>
      {(catalog.error || error) && (
        <Notice error>{catalog.error || error}</Notice>
      )}
      <div className="entry-bar">
        <label>
          当前入口
          <select
            aria-label="当前入口"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            <option value="" disabled>
              选择试用入口
            </option>
            {entries.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </label>
        <button className="secondary" onClick={() => setNewEntry(!newEntry)}>
          ＋ 新建入口
        </button>
        <span className="muted entry-tip">记忆共同积累，近期消息各自保留</span>
      </div>
      {(newEntry || (catalog.data && !entries.length)) && (
        <form className="inline-form panel" onSubmit={create}>
          <label>
            入口名称
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如：试用群聊 A"
              required
              maxLength={100}
            />
          </label>
          <label>
            入口类型
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="group">群聊</option>
              <option value="private">私聊</option>
            </select>
          </label>
          <button className="primary" disabled={busy || !name.trim()}>
            创建入口
          </button>
        </form>
      )}
      {!catalog.data && !catalog.error && (
        <p className="muted">正在读取试用入口…</p>
      )}
      {selected && uniqueCatalog && (
        <Conversation
          key={selected}
          entryId={selected}
          catalog={uniqueCatalog}
          refreshCatalog={catalog.refresh}
          {...props}
        />
      )}
    </>
  );
}

function Conversation({
  entryId,
  catalog,
  refreshCatalog,
  status,
  openMemory,
  refreshStatus,
}: Props & {
  entryId: string;
  catalog: TrialCatalog;
  refreshCatalog: () => void;
}) {
  const snapshot = useData<TrialSnapshot>(`/trial/entries/${entryId}`, 2000);
  const [speaker, setSpeaker] = useState(
    catalog.speakers.find((s) => s.is_default)?.id || "",
  );
  useEffect(() => {
    if (!speaker)
      setSpeaker(catalog.speakers.find((s) => s.is_default)?.id || "");
  }, [speaker, catalog.speakers]);
  const [speakerForm, setSpeakerForm] = useState(false);
  const [speakerName, setSpeakerName] = useState("");
  const [draft, setDraft] = useState("");
  const repliesKey = `iris.trial.replies.${entryId}`;
  const [replies, setReplies] = useState(() => {
    try {
      return window.localStorage.getItem(repliesKey) === "true";
    } catch {
      return false;
    }
  });
  function changeReplies(enabled: boolean) {
    setReplies(enabled);
    try {
      window.localStorage.setItem(repliesKey, String(enabled));
    } catch {
      // The current page can still use the choice when storage is unavailable.
    }
  }
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState<number | null>(null);
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [prepareNote, setPrepareNote] = useState("");
  const [older, setOlder] = useState<Message[]>([]);
  const [moreOlder, setMoreOlder] = useState<boolean | null>(null);
  const [paging, setPaging] = useState(false);
  const [pendingSend, setPendingSend] = useState<{
    content: string;
    speaker: string;
    key: string;
  } | null>(null);
  const data = snapshot.data;
  const state = status?.entries.find((e) => e.entry_id === entryId);
  const allMessages = [
    ...new Map(
      [...older, ...(data?.messages || [])].map((m) => [m.id, m]),
    ).values(),
  ].sort((a, b) => a.id - b.id);
  const messagePane = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const lastMessageId = allMessages.at(-1)?.id;
  useEffect(() => {
    if (followLatest.current && messagePane.current)
      messagePane.current.scrollTop = messagePane.current.scrollHeight;
  }, [lastMessageId]);
  async function addSpeaker(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const added = await api<Person>(
        "/trial/speakers",
        json("POST", { name: speakerName }),
      );
      refreshCatalog();
      setSpeaker(added.id);
      setSpeakerForm(false);
      setSpeakerName("");
    } catch (e) {
      setError(errorText(e));
    }
  }
  async function prepareOrReply(mid: number, reply: boolean) {
    setPhase(reply ? "角色正在回复…" : "正在准备回复材料…");
    setPrepareNote("");
    try {
      if (reply) {
        setRetry(mid);
        const result = await api<{ prepared: Prepared | null }>(
          `/trial/entries/${entryId}/reply`,
          json("POST", { message_id: mid }),
        );
        setPrepared(result.prepared);
        if (!result.prepared)
          setPrepareNote(
            "后端未返回当时的回复准备结果（已发布的回复可能被复用），无法展示召回判断状态。",
          );
        setRetry(null);
      } else {
        setPrepared(
          await api<Prepared>(
            `/trial/entries/${entryId}/prepare`,
            json("POST"),
          ),
        );
      }
    } catch (e) {
      setPrepareNote("本次回复准备结果未返回，无法展示召回判断状态。");
      throw e;
    }
  }
  async function send(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim() || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    setPrepared(null);
    setPrepareNote("");
    setRetry(null);
    setPhase("正在发送…");
    // Reuse the same dedupe key if transport failed before a receipt arrived.
    const pending =
      pendingSend?.content === draft && pendingSend.speaker === speaker
        ? pendingSend
        : { content: draft, speaker, key: crypto.randomUUID() };
    setPendingSend(pending);
    try {
      const receipt = await api<{ message_id: number }>(
        `/trial/entries/${entryId}/messages`,
        json("POST", {
          speaker_id: speaker,
          content: draft,
          dedupe_key: pending.key,
        }),
      );
      setDraft("");
      setPendingSend(null);
      setNotice("消息已接收，学习结果请看右侧批次。");
      snapshot.refresh();
      refreshStatus();
      await prepareOrReply(receipt.message_id, replies);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      setPhase("");
      snapshot.refresh();
      refreshStatus();
    }
  }
  async function retryReply() {
    if (!retry) return;
    setBusy(true);
    setError("");
    try {
      await prepareOrReply(retry, true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      setPhase("");
      snapshot.refresh();
      refreshStatus();
    }
  }
  async function learn() {
    setError("");
    try {
      const result = await api<{ paused: boolean }>(
        `/trial/entries/${entryId}/learn`,
        json("POST"),
      );
      setNotice(
        result.paused
          ? "已请求立即学习；模型暂停中，恢复后处理。"
          : "已请求立即学习，完成情况请看批次状态。",
      );
      refreshStatus();
    } catch (e) {
      setError(errorText(e));
    }
  }
  async function earlier() {
    setPaging(true);
    try {
      const result = await api<TrialSnapshot>(
        `/trial/entries/${entryId}?before=${allMessages[0].id}`,
      );
      setOlder([...result.messages, ...older]);
      setMoreOlder(result.has_older);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setPaging(false);
    }
  }
  return (
    <div className="trial-grid">
      <section className="panel conversation">
        <div className="panel-heading">
          <div>
            <h2>{data?.entry.name || "正在读取对话…"}</h2>
            <span className="muted">
              {data?.entry.kind === "private" ? "私聊" : "群聊"} · 文字试用
            </span>
          </div>
          <span className="live-label">
            <i />{" "}
            {snapshot.error ? "连接中断" : data ? "入口已就绪" : "正在连接"}
          </span>
        </div>
        <div
          className="messages"
          aria-label="对话记录"
          ref={messagePane}
          onScroll={(e) => {
            const node = e.currentTarget;
            followLatest.current =
              node.scrollHeight - node.scrollTop - node.clientHeight < 60;
          }}
        >
          {(moreOlder ?? data?.has_older) && (
            <button
              className="text-button older"
              onClick={earlier}
              disabled={paging}
            >
              查看更早消息
            </button>
          )}
          {!allMessages.length && (
            <Empty title="从一句话开始">
              例如「我下周三去上海出差」。实时节奏会在短暂停顿后开始学习。
            </Empty>
          )}
          {allMessages.map((m) => (
            <article
              className={`message ${m.kind === "self_output" ? "role" : "user"}`}
              key={m.id}
            >
              <div className="message-meta">
                <strong>
                  {m.kind === "self_output" ? catalog.role_name : m.sender_name}
                </strong>
                <time>{time(m.received_at)}</time>
              </div>
              <div className="bubble">{m.content}</div>
              <div
                className={`message-state ${["abandoned", "refused"].includes(m.learning_state) ? "danger-text" : ""}`}
              >
                {messageState(m.learning_state)}
                {m.batch_id && ` · 批次 #${m.batch_id}`}
                {m.kind === "self_output" ? " · 角色实际输出" : ""}
              </div>
            </article>
          ))}
        </div>
        <div className="composer">
          {(error || snapshot.error) && (
            <Notice error>{error || snapshot.error}</Notice>
          )}
          {notice && (
            <p className="receipt" role="status">
              {notice}
            </p>
          )}
          {retry && !busy && (
            <button className="secondary" onClick={retryReply}>
              重试角色回复
            </button>
          )}
          {speakerForm && (
            <form className="inline-form compact" onSubmit={addSpeaker}>
              <label>
                虚拟发言人姓名
                <input
                  value={speakerName}
                  maxLength={100}
                  required
                  onChange={(e) => setSpeakerName(e.target.value)}
                />
              </label>
              <button className="secondary" disabled={!speakerName.trim()}>
                添加发言人
              </button>
            </form>
          )}
          <form onSubmit={send}>
            <div className="composer-controls">
              <label className="speaker-label">
                发言人
                <select
                  aria-label="发言人"
                  disabled={busy}
                  value={speaker}
                  onChange={(e) => setSpeaker(e.target.value)}
                >
                  {catalog.speakers.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                      {catalog.speakers.filter((x) => x.name === s.name)
                        .length > 1
                        ? ` · ${s.id.slice(0, 6)}`
                        : ""}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="text-button"
                onClick={() => setSpeakerForm(!speakerForm)}
              >
                ＋ 添加
              </button>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={replies}
                  disabled={busy}
                  onChange={(e) => changeReplies(e.target.checked)}
                />
                <span>角色回复</span>
              </label>
            </div>
            <textarea
              aria-label="消息内容"
              placeholder="说说正在发生的事…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={3}
              disabled={busy}
            />
            <div className="composer-bottom">
              <span className="muted">{phase || "接收后自动排队学习"}</span>
              <button
                className="primary"
                disabled={busy || !draft.trim() || !speaker}
                aria-label="发送消息"
              >
                {busy ? "处理中…" : "发送"} <span aria-hidden>↑</span>
              </button>
            </div>
          </form>
        </div>
      </section>
      <aside className="trial-aside" aria-label="学习与回复上下文">
        <section className="panel">
          <div className="panel-heading">
            <h2>学习动态</h2>
            <button className="text-button" onClick={learn}>
              立即学习
            </button>
          </div>
          <div className="queue-summary">
            <strong>{state?.pending_count ?? "—"}</strong>
            <span>条待学习</span>
            <Badge tone={state?.current_batch ? "purple" : ""}>
              {state?.current_batch
                ? `批次 #${state.current_batch.id}`
                : "等待新消息"}
            </Badge>
          </div>
          {state?.current_batch && (
            <p className="batch-state">
              {batchLabel(state.current_batch)}
              {state.current_batch.next_retry_at && (
                <small>
                  下次尝试：{time(state.current_batch.next_retry_at)}
                </small>
              )}
            </p>
          )}
          {state?.latest_batch && (
            <div className="batch-result">
              <span className="muted">最近结束 · #{state.latest_batch.id}</span>
              <p>{batchLabel(state.latest_batch)}</p>
              {state.latest_batch.last_error && (
                <small>{state.latest_batch.last_error}</small>
              )}
            </div>
          )}
          <p className="fine-print">学习成功也可能没有值得记住的内容。</p>
          <EntryQueueWait value={state?.queue_wait} />
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>新形成与更新</h2>
            <Badge>{data?.recent_memories.length || 0}</Badge>
          </div>
          <p className="muted section-help">本入口最近 12 条，点击查看来源</p>
          {!data?.recent_memories.length ? (
            <p className="quiet">暂时还没有形成记忆。</p>
          ) : (
            data.recent_memories.map((m) => (
              <button
                className="memory-preview"
                key={m.id}
                onClick={() => openMemory(m.id)}
              >
                <span className="tiny-label">
                  {m.kind} · #{m.id}
                </span>
                <span>{m.content}</span>
                <small>
                  {time(m.updated_at)} <span aria-hidden>↗</span>
                </small>
              </button>
            ))
          )}
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>这次回复准备</h2>
            <span className="muted">
              {prepared
                ? `${prepared.memories.length} 条召回`
                : prepareNote
                  ? "未返回材料"
                  : busy
                    ? "准备中"
                    : "等待发送"}
            </span>
          </div>
          {!prepared ? (
            <p className="quiet">
              {prepareNote ||
                (busy ? "正在准备回复材料…" : "发送消息后展示召回结果。")}
            </p>
          ) : (
            <>
              <JudgmentSummary value={prepared.judgment} />
              {prepared.memories.length === 0 && (
                <p className="quiet">
                  没有额外召回记忆，近期原文仍在对话上下文中。
                </p>
              )}
              {prepared.memories.map((m) => (
                <div key={m.id}>
                  <button
                    className="memory-preview"
                    onClick={() => openMemory(m.id)}
                  >
                    <Badge tone="purple">
                      {m.reason === "person_highlight"
                        ? "人物要点"
                        : "与当前内容相关"}
                    </Badge>
                    <span>{m.content}</span>
                  </button>
                  <SubjectAnnotations value={m.subject_annotations} />
                </div>
              ))}
              <p className="fine-print">
                被召回不等于被使用，也不代表内容已证实。
              </p>
              {prepared.hints
                .filter((h) => h.code !== "recall_judgment")
                .map((h, i) => (
                  <p key={i} className="fine-print">
                    {h.message}
                  </p>
                ))}
            </>
          )}
        </section>
        <section className="panel context-panel">
          <h2>角色上下文</h2>
          <details open>
            <summary>
              Persona{" "}
              <span className="muted">
                {data?.persona.version
                  ? `v${data.persona.version}`
                  : "暂无版本"}
              </span>
            </summary>
            <p>
              {data?.persona.content ||
                "尚未创建 persona。可先使用现有命令行设置角色。"}
            </p>
          </details>
          <details open>
            <summary>当前状态</summary>
            <StateSummary />
          </details>
          <details>
            <summary>
              未结束目标{" "}
              <span className="muted">{data?.goals.length || 0}</span>
            </summary>
            {data?.goals.length ? (
              data.goals.map((g) => (
                <div className="goal" key={g.id}>
                  <p>{g.content}</p>
                  <small>
                    {g.overdue ? "已过期 · " : g.due_soon ? "即将到期 · " : ""}
                    {g.deadline ? time(g.deadline) : "未设截止时间"} ·{" "}
                    {g.state === "in_progress" ? "进行中" : "待处理"}
                  </small>
                </div>
              ))
            ) : (
              <p className="quiet">暂无目标。</p>
            )}
          </details>
        </section>
      </aside>
    </div>
  );
}
