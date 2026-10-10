import { useState } from "react";
import { api, ApiError, errorText, json } from "./api";
import { Dialog, Notice, lifecycleLabel } from "./ui";

type Scope = { subject_id: string } | { entry_id: string };
type Action = "forget" | "delete" | "purge";
type Preview = {
  snapshot_token: string;
  counts: Record<string, number>;
  total: number;
  applicable_count: number;
  pinned_count: number;
  excluded_pinned_count: number;
  examples: {
    id: number;
    content: string;
    lifecycle: string;
    pinned: boolean;
  }[];
};
type Result = {
  status: string;
  count: number;
  memory_ids: number[];
  skipped_count: number;
  deleted_message_ids: number[];
  retained_messages: { message_id: number; reasons: string[] }[];
};
const labels: Record<Action, string> = {
  forget: "遗忘",
  delete: "删除",
  purge: "彻底清除",
};
const protection: Record<string, string> = {
  memory_source: "仍是其他记忆的来源",
  batch_segment: "等待或运行中的批次仍在使用",
  goal_source: "仍是目标的来源",
  subject_alias: "仍是主体别名的证据",
  subject_link: "仍是主体联系的证据",
  learning_request: "立即学习请求仍在使用",
};

export function BulkMemoryDialog({
  scope,
  name,
  onClose,
  onChanged,
}: {
  scope: Scope;
  name: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [action, setAction] = useState<Action>("forget");
  const [includePinned, setIncludePinned] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [purgeStep, setPurgeStep] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const complete = result?.status === "completed";
  const invalidate = () => {
    setPreview(null);
    setError("");
    setPurgeStep(false);
    setAcknowledged(false);
  };
  async function loadPreview() {
    setBusy(true);
    invalidate();
    try {
      setPreview(
        await api<Preview>(
          "/memories/bulk/preview",
          json("POST", {
            ...scope,
            action,
            include_pinned: includePinned,
          }),
        ),
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function apply() {
    if (
      !preview ||
      busy ||
      !preview.applicable_count ||
      (action === "purge" && !acknowledged)
    )
      return;
    setBusy(true);
    setError("");
    try {
      setResult(
        await api<Result>(
          "/memories/bulk/apply",
          json("POST", {
            snapshot_token: preview.snapshot_token,
            action,
            confirm: action === "purge" && acknowledged,
          }),
        ),
      );
      setPreview(null);
      setPurgeStep(false);
      onChanged();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        invalidate();
        if (e.result) {
          setResult(e.result as Result);
          onChanged();
        }
        setError(e.message);
      } else {
        setError(
          `${errorText(e)}。执行结果尚未确认，可使用当前预览重试；若服务已重启，请先查看操作记录。`,
        );
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="批量遗忘／删除" onClose={onClose} closeDisabled={busy}>
      <div className="lifecycle-stack bulk-memory">
        <p>
          <strong>{name}</strong> ·{" "}
          {"subject_id" in scope
            ? `人物 ${scope.subject_id}`
            : `入口 ${scope.entry_id}`}
        </p>
        <p className="muted">
          {"subject_id" in scope
            ? "范围：说话人或涉及人物为此人的记忆。"
            : "范围：至少一条直接来源消息属于此入口的记忆。"}
        </p>
        {error && <Notice error>{error}</Notice>}
        {result && (
          <section aria-label="批量操作结果">
            <Notice error={!complete}>
              {complete
                ? `已批量${labels[action]} ${result.count} 条记忆。`
                : `操作未全部完成，已处理 ${result.count} 条记忆。`}
            </Notice>
            {result.skipped_count > 0 && (
              <p>跳过已删除的 {result.skipped_count} 条记忆。</p>
            )}
            <details>
              <summary>已处理记忆 ID（{result.memory_ids.length} 条）</summary>
              <p className="bulk-ids">
                {result.memory_ids.map((id) => `#${id}`).join("、") || "无"}
              </p>
            </details>
            {action === "purge" && (
              <>
                <p>
                  已删除来源消息 {result.deleted_message_ids.length} 条；保留{" "}
                  {result.retained_messages.length} 条。
                </p>
                <details>
                  <summary>来源消息处理明细</summary>
                  <p className="bulk-ids">
                    已删除：
                    {result.deleted_message_ids
                      .map((id) => `#${id}`)
                      .join("、") || "无"}
                  </p>
                  {result.retained_messages.map((item) => (
                    <p key={item.message_id}>
                      消息 #{item.message_id}：
                      {item.reasons.map((r) => protection[r] || r).join("；")}
                    </p>
                  ))}
                </details>
              </>
            )}
          </section>
        )}
        {!complete && (
          <>
            {!purgeStep && (
              <fieldset disabled={busy} className="bulk-options">
                <label>
                  目标操作
                  <select
                    value={action}
                    onChange={(e) => {
                      setAction(e.target.value as Action);
                      invalidate();
                    }}
                  >
                    <option value="forget">遗忘（可恢复）</option>
                    <option value="delete">删除（撤销对象）</option>
                    <option value="purge">彻底清除（不可撤销）</option>
                  </select>
                </label>
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={includePinned}
                    onChange={(e) => {
                      setIncludePinned(e.target.checked);
                      invalidate();
                    }}
                  />
                  包含置顶记忆
                </label>
                <p>
                  {action === "forget"
                    ? "遗忘会降低保留强度并取消置顶；仍可深度召回及恢复。"
                    : action === "delete"
                      ? "删除会撤销记忆对象，保留来源与历史；普通和深度召回均不再返回。"
                      : "彻底清除会清除正文、修订历史，以及没有其他受保护引用的来源消息。"}
                </p>
                <button
                  className="secondary"
                  onClick={() => void loadPreview()}
                >
                  {busy ? "正在处理…" : "预览范围"}
                </button>
              </fieldset>
            )}
            {preview && (
              <section aria-label="批量操作预览">
                <h3>
                  范围内 {preview.total} 条，本次处理 {preview.applicable_count}{" "}
                  条
                </h3>
                <p>
                  有效 {preview.counts.active} · 遗忘 {preview.counts.forgotten}{" "}
                  · 已删除 {preview.counts.deleted}
                </p>
                <p>
                  包含置顶 {preview.pinned_count} 条；排除置顶{" "}
                  {preview.excluded_pinned_count} 条。
                </p>
                <p className="muted">
                  已删除对象只参加彻底清除。预览有效期 15
                  分钟，记忆变化后需重新预览。
                </p>
                <ul className="bulk-examples">
                  {preview.examples.map((m) => (
                    <li key={m.id}>
                      <span className="muted">
                        #{m.id} · {lifecycleLabel(m.lifecycle)}
                        {m.pinned ? " · 置顶" : ""}
                      </span>
                      <p>{m.content}</p>
                    </li>
                  ))}
                </ul>
                {purgeStep ? (
                  <>
                    <Notice error>
                      即将彻底清除 {preview.applicable_count}{" "}
                      条记忆，此操作不可撤销。不清除批次尝试中的模型原始输出及调用记录里已有的内容。
                    </Notice>
                    <label className="check-label">
                      <input
                        type="checkbox"
                        disabled={busy}
                        checked={acknowledged}
                        onChange={(e) => setAcknowledged(e.target.checked)}
                      />
                      我理解此操作不可撤销
                    </label>
                    <div className="button-row">
                      <button
                        className="danger-button"
                        disabled={busy || !acknowledged}
                        onClick={() => void apply()}
                      >
                        再次确认并彻底清除
                      </button>
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() => {
                          setPurgeStep(false);
                          setAcknowledged(false);
                        }}
                      >
                        返回预览
                      </button>
                    </div>
                  </>
                ) : (
                  <button
                    className="danger-button"
                    disabled={busy || !preview.applicable_count}
                    onClick={() =>
                      action === "purge" ? setPurgeStep(true) : void apply()
                    }
                  >
                    确认批量{labels[action]}
                  </button>
                )}
              </section>
            )}
          </>
        )}
        {busy && <p role="status">正在处理，请等待结果…</p>}
        {!busy && (
          <a href="#/operations?object_type=memory_bulk" onClick={onClose}>
            查看批量操作记录
          </a>
        )}
        {complete && (
          <button className="primary" onClick={onClose}>
            完成
          </button>
        )}
      </div>
    </Dialog>
  );
}
