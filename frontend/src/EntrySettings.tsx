import { useState, type FormEvent } from "react";
import { api, errorText, json, useData } from "./api";
import { Dialog, Notice, fullTime } from "./ui";
import type {
  CustomPace,
  EntryFilters,
  EntrySettings,
  Person,
  QueueWait,
} from "./types";

const presets: Record<string, { name: string; value: CustomPace }> = {
  realtime: {
    name: "实时",
    value: { count: 4, idle_seconds: 5, max_wait_seconds: 60 },
  },
  standard: {
    name: "标准",
    value: { count: 12, idle_seconds: 600, max_wait_seconds: 3600 },
  },
  economy: {
    name: "省流",
    value: { count: 30, idle_seconds: 1800, max_wait_seconds: 14400 },
  },
};
export function paceLabel(value: string | CustomPace) {
  if (typeof value === "string" && presets[value]) return presets[value].name;
  try {
    const pace = typeof value === "string" ? JSON.parse(value) : value;
    return `自定义 · ${pace.count} 条 / 空闲 ${pace.idle_seconds} 秒 / 最长 ${pace.max_wait_seconds} 秒`;
  } catch {
    return "未知节奏";
  }
}
export function FilterSummary({ value }: { value?: EntryFilters }) {
  if (!value) return null;
  return (
    <div className="entry-filter-summary">
      <p>最短字数：{value.min_chars ? `${value.min_chars} 字` : "不限制"}</p>
      <p>
        提及过滤：
        {value.mention_only
          ? `只学习提到角色的消息及前后各 ${value.context_messages} 条`
          : "关闭"}
      </p>
      <p>每小时批次上限：{value.max_batches_per_hour || "不限制"}</p>
    </div>
  );
}
export function EntryQueueWait({ value }: { value?: QueueWait }) {
  if (!value) return null;
  return (
    <div className="entry-queue-wait">
      {value.reason && (
        <p className="queue-reason">
          {value.reason === "hourly_batch_limit"
            ? "达到每小时批次上限，消息排队等待"
            : value.reason === "filter_context"
              ? "等待提及窗口的后续消息或空闲定案"
              : value.reason}
        </p>
      )}
      {value.reason === "hourly_batch_limit" && value.retry_at && (
        <p>最早释放额度：{fullTime(value.retry_at)}（不是完成时间）</p>
      )}
      {value.batches_last_hour !== null && (
        <p className="muted">
          滚动 60 分钟已组成 {value.batches_last_hour} / {value.limit} 个批次
        </p>
      )}
      <p className="muted">
        待过滤定案 {value.filter_waiting_count} 条 · 已过滤{" "}
        {value.filtered_count} 条
      </p>
    </div>
  );
}
export function EntrySettingsDialog({
  entry,
  onClose,
  onSaved,
}: {
  entry: Person;
  onClose: () => void;
  onSaved: () => void;
}) {
  const data = useData<EntrySettings>(
    `/entries/${encodeURIComponent(entry.id)}/settings`,
  );
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      title={`入口设置 · ${entry.name}`}
      onClose={onClose}
      closeDisabled={busy}
    >
      {data.error && (
        <Notice error>
          {data.error}
          <button onClick={data.refresh}>重新加载</button>
        </Notice>
      )}
      {data.data ? (
        <EntryForm
          entry={entry}
          value={data.data}
          busy={busy}
          setBusy={setBusy}
          onSaved={onSaved}
        />
      ) : (
        <p role="status">正在读取入口设置…</p>
      )}
    </Dialog>
  );
}
function EntryForm({
  entry,
  value,
  busy,
  setBusy,
  onSaved,
}: {
  entry: Person;
  value: EntrySettings;
  busy: boolean;
  setBusy: (value: boolean) => void;
  onSaved: () => void;
}) {
  const [pace, setPace] = useState(
    typeof value.pace === "string" ? value.pace : "custom",
  );
  const initialPace =
    typeof value.pace === "string" ? presets[value.pace].value : value.pace;
  const [custom, setCustom] = useState({
    count: String(initialPace.count),
    idle_seconds: String(initialPace.idle_seconds),
    max_wait_seconds: String(initialPace.max_wait_seconds),
  });
  const [filters, setFilters] = useState({
    min_chars: String(value.filters.min_chars),
    mention_only: value.filters.mention_only,
    context_messages: String(value.filters.context_messages),
    max_batches_per_hour: String(value.filters.max_batches_per_hour),
  });
  const [error, setError] = useState("");
  const fields = [
    ["min_chars", "最短字数", 32768],
    ["context_messages", "前后各 K 条消息", 100],
    ["max_batches_per_hour", "每小时最多批次", 1000],
  ] as const;
  const paceFields = [
    ["count", "触发条数", 1000],
    ["idle_seconds", "空闲秒数", 86400],
    ["max_wait_seconds", "最长等待秒数", 604800],
  ] as const;
  async function save(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    for (const [key, label, max] of fields) {
      const n = Number(filters[key]);
      if (!filters[key].trim() || !Number.isInteger(n) || n < 0 || n > max) {
        setError(`${label}须为 0—${max} 的整数`);
        return;
      }
    }
    if (pace === "custom")
      for (const [key, label, max] of paceFields) {
        const n = Number(custom[key]);
        if (!custom[key].trim() || !Number.isInteger(n) || n < 1 || n > max) {
          setError(`${label}须为 1—${max} 的整数`);
          return;
        }
      }
    setBusy(true);
    setError("");
    try {
      await api(
        `/entries/${encodeURIComponent(entry.id)}/settings`,
        json("PATCH", {
          pace:
            pace === "custom"
              ? {
                  count: Number(custom.count),
                  idle_seconds: Number(custom.idle_seconds),
                  max_wait_seconds: Number(custom.max_wait_seconds),
                }
              : pace,
          filters: {
            min_chars: Number(filters.min_chars),
            mention_only: filters.mention_only,
            context_messages: Number(filters.context_messages),
            max_batches_per_hour: Number(filters.max_batches_per_hour),
          },
        }),
      );
      onSaved();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="entry-settings-form" onSubmit={save} noValidate>
      {error && <Notice error>{error}</Notice>}
      <fieldset className="lifecycle-fieldset" disabled={busy}>
        <label>
          学习节奏
          <select value={pace} onChange={(e) => setPace(e.target.value)}>
            {Object.entries(presets).map(([key, p]) => (
              <option key={key} value={key}>
                {p.name}
              </option>
            ))}
            <option value="custom">自定义</option>
          </select>
        </label>
        {pace === "custom" ? (
          <div className="form-grid">
            {paceFields.map(([key, label, max]) => (
              <label key={key}>
                {label}
                <input
                  type="number"
                  min={1}
                  max={max}
                  step={1}
                  required
                  value={custom[key]}
                  onChange={(e) =>
                    setCustom({ ...custom, [key]: e.target.value })
                  }
                />
              </label>
            ))}
          </div>
        ) : (
          <p className="muted">
            {presets[pace].value.count} 条 / 空闲{" "}
            {presets[pace].value.idle_seconds} 秒 / 最长等待{" "}
            {presets[pace].value.max_wait_seconds} 秒
          </p>
        )}
        <h3>高流量入口过滤</h3>
        <label>
          最短字数
          <input
            aria-label="最短字数"
            aria-describedby="min-chars-help"
            type="number"
            min={0}
            max={32768}
            step={1}
            required
            value={filters.min_chars}
            onChange={(e) =>
              setFilters({ ...filters, min_chars: e.target.value })
            }
          />
        </label>
        <small id="min-chars-help">
          0 表示关闭；按正文去掉首尾空白后的字符数计算。
        </small>
        <label className="check-label">
          <input
            type="checkbox"
            checked={filters.mention_only}
            onChange={(e) =>
              setFilters({ ...filters, mention_only: e.target.checked })
            }
          />
          只学习提到角色的消息
        </label>
        <p className="muted">
          正文中出现角色名或已确认的角色别名才算提及；引用内容、发送者名字和“记住”本身不算。
        </p>
        <label>
          前后各 K 条消息
          <input
            aria-label="前后各 K 条消息"
            aria-describedby="context-help"
            type="number"
            min={0}
            max={100}
            step={1}
            required
            disabled={!filters.mention_only}
            value={filters.context_messages}
            onChange={(e) =>
              setFilters({ ...filters, context_messages: e.target.value })
            }
          />
        </label>
        <small id="context-help">
          0
          表示仅学习提及消息；按本入口原始消息顺序计数，等待后续消息或空闲后定案。
        </small>
        <label>
          每小时最多批次
          <input
            aria-label="每小时最多批次"
            aria-describedby="hourly-help"
            type="number"
            min={0}
            max={1000}
            step={1}
            required
            value={filters.max_batches_per_hour}
            onChange={(e) =>
              setFilters({ ...filters, max_batches_per_hour: e.target.value })
            }
          />
        </label>
        <small id="hourly-help">
          0 表示关闭；按滚动 60
          分钟内新组成的批次计数。立即学习也受此上限及提及窗口限制。
        </small>
        <Notice>
          设置立即作用于尚未冻结的待处理消息，已组成批次保持原样。被过滤的消息仍保存并可用于近期对话，但不会进入学习、不算积压或记忆缺口；放宽或关闭过滤不会重新学习这些消息。
        </Notice>
      </fieldset>
      <button className="primary" disabled={busy}>
        {busy ? "正在保存…" : "保存入口设置"}
      </button>
    </form>
  );
}
