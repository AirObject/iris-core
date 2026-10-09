import { useEffect, useRef, useState, type FormEvent } from "react";
import { Notice } from "./ui";
import { consolidationSwitches } from "./consolidation-labels";
import type {
  ConsolidationConfig,
  GoalDedupConfig,
} from "./consolidation-types";

export function ConsolidationSettings({
  value,
  timezone,
  busy,
  save,
}: {
  value: ConsolidationConfig;
  timezone: string | null;
  busy: boolean;
  save: (value: ConsolidationConfig) => Promise<ConsolidationConfig | null>;
}) {
  const [draft, setDraft] = useState(() => ({
    ...value,
    max_calls: String(value.max_calls),
  }));
  const dirty = useRef(false);
  const [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!dirty.current)
      setDraft({ ...value, max_calls: String(value.max_calls) });
  }, [value]);
  function change<K extends keyof typeof draft>(
    key: K,
    next: (typeof draft)[K],
  ) {
    dirty.current = true;
    setSaved(false);
    setDraft((d) => ({ ...d, [key]: next }));
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(draft.maintenance_time)) {
      setError("整理运行时间须为 HH:MM，范围 00:00—23:59");
      return;
    }
    const calls = Number(draft.max_calls);
    if (
      !draft.max_calls.trim() ||
      !Number.isInteger(calls) ||
      calls < 0 ||
      calls > 50
    ) {
      setError("模型调用次数预算须为 0—50 的整数");
      return;
    }
    const result = await save({ ...draft, max_calls: calls });
    if (result) {
      dirty.current = false;
      setDraft({ ...result, max_calls: String(result.max_calls) });
      setSaved(true);
    }
  }
  return (
    <section
      className="panel settings-panel"
      aria-label="梦境整理设置"
      id="consolidation-settings"
    >
      <h2>梦境整理</h2>
      <p className="lifecycle-help">
        整理不中断接收和学习。修改只影响之后接受的新运行，已接受的运行与续跑沿用其设置快照。
      </p>
      <form onSubmit={submit} noValidate>
        <fieldset disabled={busy} className="lifecycle-fieldset">
          <div className="form-grid">
            <label>
              整理运行时间
              <input
                aria-label="整理运行时间"
                type="text"
                required
                maxLength={5}
                placeholder="03:00"
                value={draft.maintenance_time}
                onChange={(e) => change("maintenance_time", e.target.value)}
              />
              <small>
                HH:MM · 角色时区 {timezone || "未设置"}
                ，与每日记忆维护共用时间。
              </small>
            </label>
            <label>
              每次整理的模型调用次数预算
              <input
                aria-label="每次整理的模型调用次数预算"
                type="number"
                min={0}
                max={50}
                step={1}
                required
                value={draft.max_calls}
                onChange={(e) => change("max_calls", e.target.value)}
              />
              <small>0—50，包含重试、修正与 persona 生成及检查。</small>
            </label>
          </div>
          {consolidationSwitches.map(([key, label]) => (
            <label className="check-label" key={key}>
              <input
                type="checkbox"
                checked={draft[key]}
                onChange={(e) => change(key, e.target.checked)}
              />
              {label}
            </label>
          ))}
          <p className="lifecycle-help">
            预算为 0
            时不调用模型，记忆生命周期维护与开启的目标依据复核仍会执行。persona
            需满足更新条件且至少剩余 2 次调用名额。
          </p>
          <p className="lifecycle-help">
            矛盾和依赖复核只产生管理员可见的模型建议；目标依据复核不自动完成或放弃目标。
          </p>
          {error && <Notice error>{error}</Notice>}
          {saved && <Notice>梦境整理设置已保存</Notice>}
          <div className="actions">
            <button className="primary">保存梦境整理设置</button>
            <button
              type="button"
              onClick={() => {
                dirty.current = false;
                setDraft({ ...value, max_calls: String(value.max_calls) });
                setError("");
                setSaved(false);
              }}
            >
              还原整理设置
            </button>
          </div>
        </fieldset>
      </form>
      <a href="#/status">查看整理报告或手动运行</a>
    </section>
  );
}

export function GoalDedupSettings({
  value,
  busy,
  save,
}: {
  value: GoalDedupConfig;
  busy: boolean;
  save: (value: GoalDedupConfig) => Promise<GoalDedupConfig | null>;
}) {
  const draftOf = (v: GoalDedupConfig) => ({
    enabled: v.enabled,
    budget_seconds: String(v.budget_seconds),
    concurrency: String(v.concurrency),
    queue_limit: String(v.queue_limit),
  });
  const [draft, setDraft] = useState(() => draftOf(value));
  const dirty = useRef(false);
  const [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!dirty.current) setDraft(draftOf(value));
  }, [value]);
  function change<K extends keyof typeof draft>(
    key: K,
    next: (typeof draft)[K],
  ) {
    dirty.current = true;
    setSaved(false);
    setDraft((d) => ({ ...d, [key]: next }));
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    const budget = Number(draft.budget_seconds),
      concurrency = Number(draft.concurrency),
      queue = Number(draft.queue_limit);
    if (
      !draft.budget_seconds.trim() ||
      !Number.isFinite(budget) ||
      budget <= 0 ||
      budget > 10
    ) {
      setError("目标去重判断预算须大于 0 且不超过 10 秒");
      return;
    }
    if (
      !draft.concurrency.trim() ||
      !Number.isInteger(concurrency) ||
      concurrency < 1 ||
      concurrency > 8
    ) {
      setError("目标去重判断并发须为 1—8 的整数");
      return;
    }
    if (
      !draft.queue_limit.trim() ||
      !Number.isInteger(queue) ||
      queue < 0 ||
      queue > 64
    ) {
      setError("目标去重判断排队上限须为 0—64 的整数");
      return;
    }
    const result = await save({
      enabled: draft.enabled,
      budget_seconds: budget,
      concurrency,
      queue_limit: queue,
    });
    if (result) {
      dirty.current = false;
      setDraft(draftOf(result));
      setSaved(true);
    }
  }
  return (
    <form
      className="judge-settings"
      onSubmit={submit}
      noValidate
      aria-label="目标去重判断设置"
    >
      <h3>目标判断开关与排队</h3>
      <fieldset disabled={busy} className="lifecycle-fieldset">
        <label className="check-label">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => change("enabled", e.target.checked)}
          />
          启用目标去重判断
        </label>
        <label>
          目标去重方法
          <input
            aria-label="目标去重方法"
            readOnly
            value={
              draft.enabled ? "C2（批量判断与可能重复提示）" : "A（确定性规则）"
            }
          />
          <small>方法由后端固定；管理接口目前不提供方法切换。</small>
        </label>
        <p className="lifecycle-help">
          关闭后使用确定性规则。开启时若模型暂时不可用，先保留目标并等待复核；管理员创建的目标始终只标可能重复，不自动合并。清除独立模型配置只恢复沿用，不会关闭判断。
        </p>
        <div className="form-grid">
          <label>
            目标去重判断预算（秒）
            <input
              type="number"
              aria-label="目标去重判断预算（秒）"
              min={0.01}
              max={10}
              step="any"
              required
              value={draft.budget_seconds}
              onChange={(e) => change("budget_seconds", e.target.value)}
            />
            <small>大于 0 且不超过 10，含排队和模型调用。</small>
          </label>
          <label>
            目标去重判断并发
            <input
              type="number"
              min={1}
              max={8}
              step={1}
              required
              value={draft.concurrency}
              onChange={(e) => change("concurrency", e.target.value)}
            />
          </label>
          <label>
            目标去重判断排队上限
            <input
              type="number"
              min={0}
              max={64}
              step={1}
              required
              value={draft.queue_limit}
              onChange={(e) => change("queue_limit", e.target.value)}
            />
          </label>
        </div>
        {error && <Notice error>{error}</Notice>}
        {saved && <Notice>目标去重判断设置已保存</Notice>}
        <div className="actions">
          <button className="primary">保存目标去重判断设置</button>
          <button
            type="button"
            onClick={() => {
              dirty.current = false;
              setDraft(draftOf(value));
              setError("");
              setSaved(false);
            }}
          >
            还原目标判断设置
          </button>
        </div>
      </fieldset>
    </form>
  );
}
