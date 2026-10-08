import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api, errorText, json, useData } from "./api";
import { Badge, Empty, Notice, Pagination, fullTime } from "./ui";
import type { Memory, MemoryDetail, Page, PurgeResult } from "./types";

const protectionReasons: Record<string, string> = {
  memory_source: "仍是其他记忆的来源",
  batch_segment: "等待或运行中的批次仍在使用",
  goal_source: "仍是目标的来源",
  subject_alias: "仍是主体别名的证据",
  subject_link: "仍是主体联系的证据",
  learning_request: "立即学习请求仍在使用",
};
export function PurgeSummary({ result }: { result: PurgeResult }) {
  return (
    <div className="lifecycle-stack">
      <Notice>已彻底清除记忆 #{result.memory_id}</Notice>
      <section>
        <h3>已删除的来源消息</h3>
        {result.deleted_message_ids.length ? (
          <ul>
            {result.deleted_message_ids.map((id) => (
              <li key={id}>消息 #{id} · 没有其他受保护的引用</li>
            ))}
          </ul>
        ) : (
          <p className="quiet">没有删除来源消息。</p>
        )}
      </section>
      <section>
        <h3>被保留的来源消息及原因</h3>
        {result.retained_messages.length ? (
          <ul>
            {result.retained_messages.map((m) => (
              <li key={m.message_id}>
                消息 #{m.message_id} ·{" "}
                {m.reasons.map((r) => protectionReasons[r] || r).join("；")}
              </li>
            ))}
          </ul>
        ) : (
          <p className="quiet">没有被保留的来源消息。</p>
        )}
      </section>
    </div>
  );
}

export function MemoryControls({
  detail,
  busy,
  blocked,
  setBusy,
  saved,
  failed,
  purged,
  recreated,
}: {
  detail: MemoryDetail;
  busy: boolean;
  blocked: boolean;
  setBusy: (value: boolean) => void;
  saved: (detail: MemoryDetail) => void;
  failed: (error: unknown) => void;
  purged: (result: PurgeResult) => void;
  recreated: (id: number) => void;
}) {
  const [mode, setMode] = useState<
    "scores" | "forget" | "purge" | "recreate" | null
  >(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [importance, setImportance] = useState(""),
    [retention, setRetention] = useState("");
  const [scoreError, setScoreError] = useState("");
  const [selected, setSelected] = useState("");
  const disabled = busy || blocked;
  const historical = new Map<number, Record<string, unknown>>();
  // The backend prefers a revision's before snapshot when both sides exist.
  for (const r of detail.revisions) historical.set(r.revision_after, r.after);
  for (const r of detail.revisions) historical.set(r.revision_before, r.before);
  const snapshot = historical.get(Number(selected));
  async function change(action: string, payload: Record<string, unknown> = {}) {
    if (disabled) return;
    setBusy(true);
    try {
      const path = `/memories/${detail.id}/${action}`;
      const request = json(action === "lifecycle" ? "PATCH" : "POST", {
        expected_revision: detail.revision,
        ...payload,
      });
      if (action === "purge") purged(await api<PurgeResult>(path, request));
      else if (action === "recreate")
        recreated((await api<MemoryDetail>(path, request)).id);
      else saved(await api<MemoryDetail>(path, request));
      setMode(null);
      setAcknowledged(false);
    } catch (e) {
      failed(e);
    } finally {
      setBusy(false);
    }
  }
  function saveScores(event: FormEvent) {
    event.preventDefault();
    setScoreError("");
    if (
      [importance, retention].some(
        (v) =>
          !v.trim() ||
          !Number.isInteger(Number(v)) ||
          Number(v) < 0 ||
          Number(v) > 100,
      )
    ) {
      setScoreError("重要度和保留强度须为 0—100 的整数");
      return;
    }
    const payload: Record<string, number> = {};
    if (Number(importance) !== detail.importance)
      payload.importance = Number(importance);
    if (Number(retention) !== detail.retention)
      payload.retention = Number(retention);
    if (!Object.keys(payload).length) {
      setMode(null);
      return;
    }
    void change("lifecycle", payload);
  }
  const cancel = () => {
    setMode(null);
    setAcknowledged(false);
    setScoreError("");
  };
  return (
    <section
      className="detail-section lifecycle-stack"
      aria-label="生命周期操作"
    >
      <h3>生命周期操作</h3>
      {!mode && (
        <div className="actions">
          {detail.lifecycle !== "deleted" && (
            <>
              <button
                className="secondary"
                disabled={disabled}
                onClick={() =>
                  void change("lifecycle", { pinned: !detail.pinned })
                }
              >
                {detail.pinned ? "取消置顶" : "置顶记忆"}
              </button>
              {detail.lifecycle === "forgotten" ? (
                <button
                  className="secondary"
                  disabled={disabled}
                  onClick={() => void change("restore")}
                >
                  恢复记忆
                </button>
              ) : (
                <button
                  className="secondary"
                  disabled={disabled}
                  onClick={() => setMode("forget")}
                >
                  手动遗忘
                </button>
              )}
              <button
                className="secondary"
                disabled={disabled}
                onClick={() => {
                  setMode("scores");
                  setImportance(String(detail.importance));
                  setRetention(String(detail.retention));
                }}
              >
                调整分数
              </button>
            </>
          )}
          <button
            className="secondary"
            disabled={disabled || !historical.size}
            onClick={() => {
              setSelected("");
              setMode("recreate");
            }}
          >
            按旧内容新建
          </button>
          <button
            className="text-button danger-text"
            disabled={disabled}
            onClick={() => setMode("purge")}
          >
            彻底清除
          </button>
        </div>
      )}
      {!historical.size && (
        <p className="fine-print">尚无历史修订可用于“按旧内容新建”。</p>
      )}
      {!!detail.pinned && detail.lifecycle !== "deleted" && (
        <p className="lifecycle-help">
          置顶记忆不自动衰减、遗忘、扣减或删除。置顶不会自动恢复已遗忘的记忆。
        </p>
      )}
      {mode === "scores" && (
        <form onSubmit={saveScores} noValidate>
          <fieldset className="lifecycle-fieldset" disabled={disabled}>
            <div className="form-grid">
              <label>
                调整重要度
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  required
                  value={importance}
                  onChange={(e) => setImportance(e.target.value)}
                />
              </label>
              <label>
                调整保留强度
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  required
                  value={retention}
                  onChange={(e) => setRetention(e.target.value)}
                />
              </label>
            </div>
            <p className="lifecycle-help">
              保留强度调整会按当前阈值判断遗忘或恢复。
            </p>
            {scoreError && <Notice error>{scoreError}</Notice>}
            <div className="actions">
              <button className="primary">保存分数</button>
              <button className="secondary" type="button" onClick={cancel}>
                取消调整
              </button>
            </div>
          </fieldset>
        </form>
      )}
      {mode === "forget" && (
        <div className="delete-confirm">
          <p>
            确认手动遗忘？保留强度会降至遗忘阈值以下，并取消置顶；此后只参与深度召回。恢复时不会重新置顶。
          </p>
          <div className="actions">
            <button
              className="primary"
              disabled={disabled}
              onClick={() => void change("forget")}
            >
              确认遗忘
            </button>
            <button className="secondary" disabled={busy} onClick={cancel}>
              取消遗忘
            </button>
          </div>
        </div>
      )}
      {mode === "purge" && (
        <div className="purge-confirm">
          <h3>彻底清除不可撤销</h3>
          <p>
            将删除记忆正文、修订历史，以及只被这条记忆引用且没有其他受保护引用的来源消息。其他引用仍需要的消息会保留，执行后显示消息标识与原因。
          </p>
          <p>不清除批次尝试中的模型原始输出及调用记录里已有的内容。</p>
          <label className="check-label">
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={disabled}
              onChange={(e) => setAcknowledged(e.target.checked)}
            />
            我理解此操作不可撤销
          </label>
          <div className="actions">
            <button
              className="danger-button"
              disabled={disabled || !acknowledged}
              onClick={() => void change("purge", { confirm: true })}
            >
              再次确认并彻底清除
            </button>
            <button className="secondary" disabled={busy} onClick={cancel}>
              取消清除
            </button>
          </div>
        </div>
      )}
      {mode === "recreate" && (
        <div className="lifecycle-stack">
          <label>
            选择历史修订
            <select
              disabled={disabled}
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">请选择修订</option>
              {[...historical.keys()]
                .sort((a, b) => b - a)
                .map((r) => (
                  <option key={r} value={r}>
                    修订 {r}
                  </option>
                ))}
            </select>
          </label>
          {snapshot && (
            <div className="revision" data-testid="revision-preview">
              <p>{String(snapshot.content ?? "")}</p>
              <small>
                相信程度 {String(snapshot.belief ?? detail.belief)} · 重要度{" "}
                {String(snapshot.importance ?? detail.importance)}
              </small>
            </div>
          )}
          <p className="lifecycle-help">
            新建使用新标识，原记忆保持原状。正文和判断取所选修订，涉及人物、标签和来源沿用当前保存的关系；不继承置顶、遗忘时间或向量。
          </p>
          <div className="actions">
            <button
              className="primary"
              disabled={disabled || !snapshot}
              onClick={() =>
                void change("recreate", { source_revision: Number(selected) })
              }
            >
              确认新建
            </button>
            <button className="secondary" disabled={busy} onClick={cancel}>
              取消新建
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

type Upcoming = Page<Memory & { delete_after: string }> & { enabled: boolean };
export function UpcomingDeletion({
  version,
  openMemory,
  onChange,
}: {
  version: number;
  openMemory: (id: number) => void;
  onChange: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const list = useData<Upcoming>(
    `/memories/upcoming-deletion?limit=30&offset=${offset}`,
  );
  const [busy, setBusy] = useState<number | null>(null),
    [error, setError] = useState(""),
    [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);
  useEffect(() => {
    list.refresh();
  }, [version, list.refresh]);
  // Recover from an emptied last page after a restore or maintenance run.
  useEffect(() => {
    if (list.data && offset && offset >= list.data.total)
      setOffset(Math.max(0, Math.ceil(list.data.total / 30) * 30 - 30));
  }, [list.data, offset]);
  async function restore(memory: Memory) {
    if (busy !== null || conflict || list.loading || list.error) return;
    setBusy(memory.id);
    setError("");
    setMessage("");
    try {
      await api(
        `/memories/${memory.id}/restore`,
        json("POST", { expected_revision: memory.revision }),
      );
      setMessage(`已恢复记忆 #${memory.id}`);
      list.refresh();
      onChange();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 404)) {
        setConflict(true);
        setError("这条记忆已变化或已被删除，请刷新列表后再操作。");
      } else setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }
  return (
    <>
      <div className="panel-heading">
        <h2>即将删除</h2>
        <button
          className="secondary"
          disabled={busy !== null}
          onClick={() => {
            setConflict(false);
            setError("");
            list.refresh();
          }}
        >
          刷新列表
        </button>
      </div>
      <p className="learning-intro">
        包括遗忘期将满和已到期等待维护的记忆；置顶记忆不自动删除。到期后由维护执行删除，可以在此提前恢复。
      </p>
      {(error || list.error) && <Notice error>{error || list.error}</Notice>}
      {message && <Notice>{message}</Notice>}
      {!list.data && !list.error && (
        <p role="status">正在读取即将删除的记忆…</p>
      )}
      {list.data && !list.data.enabled && (
        <Notice>
          自动删除已关闭，当前没有即将删除的记忆。
          <a href="#/settings">前往生命周期设置</a>
        </Notice>
      )}
      {list.data?.enabled && !list.data.items.length && (
        <Empty title="暂无即将删除的记忆" />
      )}
      <div className="memory-grid">
        {list.data?.items.map((m) => (
          <article className="panel upcoming-card" key={m.id}>
            <div className="memory-card-top">
              <Badge>已遗忘</Badge>
              <span className="muted">
                #{m.id} · 保留强度 {m.retention}
              </span>
            </div>
            <button className="memory-preview" onClick={() => openMemory(m.id)}>
              {m.content}
            </button>
            <p>遗忘时间：{fullTime(m.forgotten_at)}</p>
            <p>删除到期时间：{fullTime(m.delete_after)}</p>
            <button
              className="secondary"
              aria-label={`恢复记忆 #${m.id}`}
              disabled={
                busy !== null || conflict || list.loading || !!list.error
              }
              onClick={() => void restore(m)}
            >
              {busy === m.id ? "正在恢复…" : "恢复记忆"}
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
  );
}
