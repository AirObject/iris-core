import { useState } from "react";
import { api, errorText, json, useData } from "./api";
import {
  Badge,
  Empty,
  Notice,
  batchLabel,
  lifecycleLabel,
  messageState,
  seconds,
  time,
} from "./ui";
import type { Batch, Entry, Message } from "./types";

type Summary = Batch & {
  entry_id: string;
  entry_name?: string;
  created_at: string;
  finished_at?: string;
  prompt_version: string;
  can_relearn: boolean;
  relearning: boolean;
};
type LearningEntry = Entry & {
  platform: string;
  pending_count: number;
  latest_batch?: Summary;
  current_batch?: Summary;
};
type Call = {
  id: number;
  purpose: string;
  model: string;
  duration_ms: number;
  result_category: string;
  error_summary?: string;
  finish_reason?: string;
  reasoning_effort?: string;
  timed_out?: boolean;
  created_at: string;
};
type Attempt = {
  id: number;
  number: number;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  parse_status: string;
  error?: string;
  raw_output?: string;
  repair_output?: string;
  calls: Call[];
};
type BatchMessage = Message & {
  missing?: boolean;
  quote_content?: string;
  quote_author_name?: string;
  scene_identity?: string;
};
type Detail = Omit<Summary, "result"> & {
  entry: LearningEntry;
  segments: Record<"history" | "target" | "future", BatchMessage[]>;
  attempts: Attempt[];
  unassigned_calls: Call[];
  result: {
    created?: number[];
    updated?: number[];
    confirmed?: number[];
    normalizations?: {
      section?: string;
      index?: number;
      field: string;
      before: unknown;
      after: unknown;
      reason: string;
    }[];
    dropped?: { section: string; item: unknown; reason: string }[];
  };
  memories: {
    id: number;
    content?: string;
    change: string;
    lifecycle: string;
    revision: number;
    missing?: boolean;
  }[];
};
type Gap = {
  entry_id: string;
  entry_name: string;
  batch_id: number;
  started_at: string;
  ended_at: string;
  reason: string;
  batch_state: string;
  relearning: boolean;
};
type Page<T> = { items: T[]; total: number; offset: number; limit: number };
const kindLabel = (value: string) =>
  ({ group: "群聊", private: "私聊" })[value] || value;
const messageKind = (value: string) =>
  ({
    message: "消息",
    self_output: "角色实际输出",
    action_result: "行动结果",
    event: "场景事件",
  })[value] || value;
const gapReason = (value: string) =>
  ({
    attempts_exhausted: "尝试用尽，未学习",
    content_rejection: "内容拒绝，未学习",
    interrupted: "学习中断",
  })[value] || value;
function pace(value: string) {
  const label = { realtime: "实时", standard: "标准", economy: "省流" }[value];
  if (label) return label;
  try {
    const v = JSON.parse(value);
    return `自定义 · ${v.count} 条 / 空闲 ${v.idle_seconds} 秒 / 最长 ${v.max_wait_seconds} 秒`;
  } catch {
    return value;
  }
}
const pretty = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);

export default function LearningPage({
  openMemory,
}: {
  openMemory: (id: number) => void;
}) {
  const [tab, setTab] = useState<"entries" | "batches" | "gaps">("entries");
  const [selected, setSelected] = useState<number | null>(null);
  const [filter, setFilter] = useState({
    entry_id: "",
    time_from: "",
    time_to: "",
  });
  const [applied, setApplied] = useState(filter);
  const [offset, setOffset] = useState(0);
  const entries = useData<{ items: LearningEntry[] }>("/entries", 5000);
  const params = new URLSearchParams({ limit: "30", offset: String(offset) });
  for (const [key, value] of Object.entries(applied))
    if (value) params.set(key, value);
  const batches = useData<Page<Summary>>(
    tab === "batches" && selected === null ? `/batches?${params}` : null,
    3000,
  );
  const gaps = useData<Page<Gap>>(
    tab === "gaps" && selected === null ? `/memory-gaps?${params}` : null,
    3000,
  );
  const listing = tab === "batches" ? batches : gaps;
  const refresh = () => {
    entries.refresh();
    batches.refresh();
    gaps.refresh();
  };
  function switchTab(value: typeof tab) {
    setTab(value);
    setSelected(null);
    setOffset(0);
  }
  function entryBatches(id: string) {
    const value = { entry_id: id, time_from: "", time_to: "" };
    setFilter(value);
    setApplied(value);
    switchTab("batches");
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">从消息到记忆</p>
          <h1>入口与学习</h1>
          <p>查看每个入口的学习进度，追溯批次结果与尚未记住的消息。</p>
        </div>
      </div>
      <div className="learning-tabs" role="group" aria-label="入口与学习视图">
        {(
          [
            ["entries", "入口列表"],
            ["batches", "学习批次"],
            ["gaps", "记忆缺口"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            aria-pressed={tab === id}
            onClick={() => switchTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {entries.error && (
        <Notice error>
          {entries.error}
          <button onClick={entries.refresh}>重试</button>
        </Notice>
      )}
      {selected !== null ? (
        <BatchDetail
          key={selected}
          id={selected}
          openMemory={openMemory}
          onBack={() => setSelected(null)}
          onRelearn={refresh}
        />
      ) : tab === "entries" ? (
        <>
          <p className="learning-intro">
            已接收的消息可能仍在等待学习；批次结束后，也可能没有形成记忆。
          </p>
          {!entries.data ? (
            <p>正在读取入口…</p>
          ) : !entries.data.items.length ? (
            <Empty title="还没有入口">
              可在试用对话页创建入口，或由宿主发送消息。
            </Empty>
          ) : (
            <div className="entry-grid">
              {entries.data.items.map((entry) => (
                <article className="panel learning-entry" key={entry.id}>
                  <div className="section-title">
                    <h2>{entry.name}</h2>
                    <Badge>{kindLabel(entry.kind)}</Badge>
                  </div>
                  <p className="muted">
                    {entry.platform === "iris-trial"
                      ? "试用入口"
                      : `宿主入口 · ${entry.platform}`}
                  </p>
                  <div className="learning-entry-meta">
                    <Badge>{pace(entry.pace)}</Badge>
                    <strong>待学习 {entry.pending_count} 条</strong>
                  </div>
                  <p>
                    最近批次
                    {entry.latest_batch ? ` #${entry.latest_batch.id}` : ""}
                  </p>
                  <p>{batchLabel(entry.latest_batch)}</p>
                  {entry.current_batch &&
                    entry.current_batch.id !== entry.latest_batch?.id && (
                      <p>
                        当前处理 #{entry.current_batch.id} ·{" "}
                        {batchLabel(entry.current_batch)}
                      </p>
                    )}
                  <button
                    className="text-button"
                    aria-label={`查看批次 · ${entry.name}`}
                    onClick={() => entryBatches(entry.id)}
                  >
                    查看批次 →
                  </button>
                </article>
              ))}
            </div>
          )}
        </>
      ) : (
        <>
          <p className="learning-intro">
            {tab === "gaps"
              ? "放弃或内容拒绝的批次留下记忆缺口。重新学习成功后才会消失；模型暂停期间的普通积压不算缺口。"
              : "批次保留冻结的三段消息。成功可以形成零条记忆；失败重试和放弃、拒绝会分别显示。"}
          </p>
          <form
            className="learning-filters"
            onSubmit={(event) => {
              event.preventDefault();
              setApplied({ ...filter });
              setOffset(0);
            }}
          >
            <label>
              筛选入口
              <select
                value={filter.entry_id}
                onChange={(e) =>
                  setFilter({ ...filter, entry_id: e.target.value })
                }
              >
                <option value="">全部入口</option>
                {entries.data?.items.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              开始日期
              <input
                type="date"
                value={filter.time_from}
                onChange={(e) =>
                  setFilter({ ...filter, time_from: e.target.value })
                }
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                value={filter.time_to}
                min={filter.time_from || undefined}
                onChange={(e) =>
                  setFilter({ ...filter, time_to: e.target.value })
                }
              />
            </label>
            <button className="secondary" type="submit">
              筛选
            </button>
          </form>
          <p className="muted learning-date-help">
            日期按角色时区；
            {tab === "gaps"
              ? "显示与日期范围重叠的消息区间。"
              : "按批次组成时间筛选。"}
          </p>
          {listing.error && (
            <Notice error>
              {listing.error}
              <button onClick={listing.refresh}>重试</button>
            </Notice>
          )}
          {!listing.data ? (
            <p>正在读取…</p>
          ) : (
            <>
              {listing.data.total === 0 ? (
                <Empty
                  title={
                    tab === "gaps" ? "当前范围没有记忆缺口" : "当前范围没有批次"
                  }
                />
              ) : (
                <div className="learning-list">
                  {tab === "batches"
                    ? batches.data?.items.map((batch) => (
                        <article key={batch.id} className="panel learning-row">
                          <div>
                            <strong>
                              #{batch.id} · {batch.entry_name}
                            </strong>
                            <p>
                              {batch.relearning
                                ? "重新学习中"
                                : batchLabel(batch)}
                            </p>
                            <small>
                              {time(batch.created_at)} · 本轮已计{" "}
                              {batch.attempt_count} 次尝试
                            </small>
                          </div>
                          <button
                            className="text-button"
                            aria-label={`查看批次 #${batch.id}`}
                            onClick={() => setSelected(batch.id)}
                          >
                            查看详情 →
                          </button>
                        </article>
                      ))
                    : gaps.data?.items.map((gap) => (
                        <article
                          key={gap.batch_id}
                          className="panel learning-row"
                        >
                          <div>
                            <strong>
                              {gap.entry_name} · 批次 #{gap.batch_id}
                            </strong>
                            <p>
                              {time(gap.started_at)} — {time(gap.ended_at)}
                            </p>
                            <p>{gapReason(gap.reason)}</p>
                            {gap.relearning && <Badge>重新学习中</Badge>}
                          </div>
                          <button
                            className="text-button"
                            aria-label={`查看批次 #${gap.batch_id}`}
                            onClick={() => setSelected(gap.batch_id)}
                          >
                            查看详情 →
                          </button>
                        </article>
                      ))}
                </div>
              )}
              <div className="learning-pagination">
                <span>共 {listing.data.total} 条</span>
                <button
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 30))}
                >
                  上一页
                </button>
                <button
                  disabled={offset + 30 >= listing.data.total}
                  onClick={() => setOffset(offset + 30)}
                >
                  下一页
                </button>
              </div>
            </>
          )}
        </>
      )}
    </>
  );
}

function Calls({ calls }: { calls: Call[] }) {
  if (!calls.length)
    return (
      <p className="muted">没有可关联的调用记录；结束原因与推理档位未知。</p>
    );
  return (
    <div className="learning-calls">
      {calls.map((call) => (
        <div key={call.id}>
          <strong>
            {call.purpose === "learning_repair" ? "JSON 修正请求" : "学习请求"}{" "}
            · {call.model}
          </strong>
          <dl>
            <div>
              <dt>请求结果</dt>
              <dd>
                {call.result_category === "success"
                  ? "已返回正文"
                  : call.result_category}
                {call.timed_out ? " · 超时" : ""}
              </dd>
            </div>
            <div>
              <dt>耗时</dt>
              <dd>{seconds(call.duration_ms)}</dd>
            </div>
            <div>
              <dt>结束原因（finish_reason）</dt>
              <dd>{call.finish_reason || "未提供"}</dd>
            </div>
            <div>
              <dt>推理档位</dt>
              <dd>{call.reasoning_effort || "未记录 / 服务商默认"}</dd>
            </div>
          </dl>
          {call.error_summary && <p>{call.error_summary}</p>}
        </div>
      ))}
    </div>
  );
}
function Body({ label, value }: { label: string; value?: string }) {
  return value == null ? (
    <p className="muted">{label}：没有记录</p>
  ) : (
    <details className="learning-output">
      <summary>{label}</summary>
      <pre>{value || "（空正文）"}</pre>
    </details>
  );
}
function BatchDetail({
  id,
  onBack,
  openMemory,
  onRelearn,
}: {
  id: number;
  onBack: () => void;
  openMemory: (id: number) => void;
  onRelearn: () => void;
}) {
  const snapshot = useData<Detail>(`/batches/${id}`, 3000);
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [error, setError] = useState("");
  const batch = snapshot.data;
  async function relearn() {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/batches/${id}/relearn`, json("POST", {}));
      setNotice(
        "已重新排队，尚未记住。模型恢复且本入口在途批次结束后开始学习。",
      );
      onRelearn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      snapshot.refresh();
    }
  }
  return (
    <section aria-label={`批次 #${id}`} className="batch-detail">
      <button className="text-button" onClick={onBack}>
        ← 返回列表
      </button>
      {(snapshot.error || error) && (
        <Notice error>{error || snapshot.error}</Notice>
      )}
      {notice && <Notice>{notice}</Notice>}
      {!batch ? (
        <p>正在读取批次…</p>
      ) : (
        <>
          <div className="panel batch-overview">
            <div className="section-title">
              <h2>
                批次 #{id} · {batch.entry.name}
              </h2>
              <Badge>
                {batch.relearning ? "重新学习中" : batchLabel(batch)}
              </Badge>
            </div>
            <p>{batchLabel(batch)}</p>
            <p className="muted">
              组成于 {time(batch.created_at)}
              {batch.finished_at
                ? ` · 结束于 ${time(batch.finished_at)}`
                : ""}{" "}
              · 本轮已计 {batch.attempt_count} / 4 次尝试
            </p>
            {batch.next_retry_at && (
              <p>下次重试：{time(batch.next_retry_at)}</p>
            )}
            {batch.last_error && <p>最近错误：{batch.last_error}</p>}
            {batch.can_relearn && (
              <div className="relearn-action">
                <button className="primary" disabled={busy} onClick={relearn}>
                  {busy ? "正在提交…" : "重新学习"}
                </button>
                <p>
                  保留原批次范围和历史记录，重新获得 4
                  次尝试；成功前缺口持续存在。
                </p>
              </div>
            )}
            {batch.state === "succeeded" && !batch.memories.length && (
              <p>本批已学习，形成或更新了 0 条记忆。这是正常的成功结果。</p>
            )}
            {batch.state !== "succeeded" && (
              <p>尚未成功学习，不能把这些消息当作已经记住。</p>
            )}
          </div>
          <section>
            <h2>三段消息</h2>
            <p className="muted">
              三段范围已冻结；历史与后续只帮助理解，只有目标段可以形成记忆。这里保留消息原文。
            </p>
            <div className="batch-segments">
              {(
                [
                  ["history", "历史段", "只帮助理解"],
                  ["target", "目标段", "只从这里学习"],
                  ["future", "后续段", "以后再学习"],
                ] as const
              ).map(([key, label, help]) => (
                <section className={`panel segment segment-${key}`} key={key}>
                  <div className="section-title">
                    <h3>
                      {label} · {batch.segments[key].length} 条
                    </h3>
                    <small>{help}</small>
                  </div>
                  {!batch.segments[key].length && (
                    <p className="muted">本段为空</p>
                  )}
                  {batch.segments[key].map((message) => (
                    <article className="batch-message" key={message.id}>
                      {message.missing ? (
                        <p>消息 #{message.id} 已不可用</p>
                      ) : (
                        <>
                          <div>
                            <strong>
                              {message.sender_name || message.sender_subject_id}
                            </strong>{" "}
                            <Badge>{messageKind(message.kind)}</Badge>
                          </div>
                          <small>
                            #{message.id} · {time(message.occurred_at)}
                            {message.scene_identity
                              ? ` · ${message.scene_identity}`
                              : ""}
                          </small>
                          {message.content.length > 600 ? (
                            <details>
                              <summary>
                                {message.content.slice(0, 120)}…（展开原文）
                              </summary>
                              <p className="message-body">{message.content}</p>
                            </details>
                          ) : (
                            <p className="message-body">{message.content}</p>
                          )}
                          {message.quote_content && (
                            <blockquote>
                              {message.quote_author_name || "引用"}：
                              {message.quote_content}
                            </blockquote>
                          )}
                          <small>{messageState(message.learning_state)}</small>
                        </>
                      )}
                    </article>
                  ))}
                </section>
              ))}
            </div>
          </section>
          <section>
            <h2>每次尝试</h2>
            <p className="muted">
              历次尝试持续编号；服务暂停时的失败记录不一定扣除尝试额度。模型返回正文不代表已经写入记忆。
            </p>
            {!batch.attempts.length && (
              <Empty
                title={
                  batch.state === "running"
                    ? "本次尝试仍在进行"
                    : "尚无已完成的尝试"
                }
              />
            )}
            {batch.attempts.map((attempt) => (
              <article key={attempt.id} className="panel batch-attempt">
                <div className="section-title">
                  <h3>第 {attempt.number} 次尝试</h3>
                  <Badge>
                    {attempt.parse_status === "failed"
                      ? "失败 · 未写入"
                      : "学习成功"}
                  </Badge>
                </div>
                <p>
                  {time(attempt.started_at)} — {time(attempt.finished_at)} ·
                  总耗时 {seconds(attempt.duration_ms)}
                </p>
                <p>
                  解析结果：
                  {{
                    direct: "直接解析",
                    repaired: "修正后解析",
                    failed: "失败",
                  }[attempt.parse_status] || attempt.parse_status}
                </p>
                {attempt.error && <p>诊断：{attempt.error}</p>}
                <Calls calls={attempt.calls} />
                <Body label="模型原始正文" value={attempt.raw_output} />
                <Body label="JSON 修正后的正文" value={attempt.repair_output} />
              </article>
            ))}
            {!!batch.unassigned_calls.length && (
              <details className="panel batch-attempt">
                <summary>
                  尚未归入已完成尝试的调用 · {batch.unassigned_calls.length} 条
                </summary>
                <Calls calls={batch.unassigned_calls} />
              </details>
            )}
          </section>
          <div className="batch-results">
            <section className="panel">
              <h2>规整记录 · {batch.result.normalizations?.length || 0} 条</h2>
              {!batch.result.normalizations?.length && (
                <p className="muted">没有规整记录。</p>
              )}
              {batch.result.normalizations?.map((item, index) => (
                <article className="result-item" key={index}>
                  <strong>{item.field}</strong>
                  <p>{item.reason}</p>
                  <small>
                    {item.section}
                    {item.index != null ? ` · 第 ${item.index + 1} 项` : ""}
                  </small>
                  <pre>
                    {pretty(item.before)} → {pretty(item.after)}
                  </pre>
                </article>
              ))}
            </section>
            <section className="panel">
              <h2>被丢弃的条目 · {batch.result.dropped?.length || 0} 条</h2>
              {!batch.result.dropped?.length && (
                <p className="muted">
                  没有已记录的丢弃条目；批次失败时请查看原始输出和错误。
                </p>
              )}
              {batch.result.dropped?.map((item, index) => (
                <article className="result-item" key={index}>
                  <strong>{item.reason}</strong>
                  <small>{item.section}</small>
                  <pre>{pretty(item.item)}</pre>
                </article>
              ))}
            </section>
          </div>
          <section className="panel">
            <h2>记忆结果 · {batch.memories.length} 条</h2>
            <p className="muted">
              下列正文为记忆当前版本；详情中可查看修订历史。
            </p>
            {!batch.memories.length && (
              <p>
                {batch.state === "succeeded"
                  ? "本批没有新增、更新或确认记忆。"
                  : "尚无成功写入的记忆。"}
              </p>
            )}
            {batch.memories.map((memory) => (
              <div
                className="learning-row"
                key={`${memory.change}-${memory.id}`}
              >
                <Badge>
                  {{ created: "新增", updated: "更新", confirmed: "再次确认" }[
                    memory.change
                  ] || memory.change}
                </Badge>
                {memory.missing ? (
                  <span>记忆 #{memory.id} 已不可用</span>
                ) : (
                  <button
                    className="text-button"
                    onClick={() => openMemory(memory.id)}
                  >
                    #{memory.id} · {memory.content}
                  </button>
                )}
                <small>
                  {lifecycleLabel(memory.lifecycle)} · 修订 {memory.revision}
                </small>
              </div>
            ))}
          </section>
        </>
      )}
    </section>
  );
}
