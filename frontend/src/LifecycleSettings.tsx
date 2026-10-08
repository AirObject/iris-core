import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, errorText, json } from "./api";
import { Notice } from "./ui";
import type { LifecycleConfig } from "./types";

const numericFields = [
  ["forget_threshold", "遗忘阈值 F", 1, 99],
  ["restore_threshold", "恢复阈值 H", 2, 100],
  ["feedback_increment", "实际使用增幅", 0, 100],
  ["confirmation_increment", "再次确认增幅", 0, 100],
  ["decay_amount", "每次衰减幅度", 0, 100],
  ["dependency_penalty", "依据失效扣减", 0, 100],
  ["auto_delete_days", "遗忘后自动删除天数", 1, 36500],
  ["upcoming_delete_days", "即将删除窗口（天）", 1, 36500],
  ["message_retention_days", "消息保留天数", 1, 36500],
] as const;
type NumberKey = (typeof numericFields)[number][0];
type Draft = Omit<LifecycleConfig, NumberKey> & Record<NumberKey, string>;
function draftOf(value: LifecycleConfig): Draft {
  const numbers = Object.fromEntries(
    numericFields.map(([key]) => [key, String(value[key])]),
  ) as Record<NumberKey, string>;
  return { ...value, ...numbers };
}

export function LifecycleSettings({
  value,
  timezone,
}: {
  value: LifecycleConfig;
  timezone: string | null;
}) {
  const [draft, setDraft] = useState(() => draftOf(value));
  const dirty = useRef(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!dirty.current) setDraft(draftOf(value));
  }, [value]);
  function change<K extends keyof Draft>(key: K, next: Draft[K]) {
    dirty.current = true;
    setSaved(false);
    setDraft((d) => ({ ...d, [key]: next }));
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    for (const [key, label, min, max] of numericFields) {
      const number = Number(draft[key]);
      if (
        !draft[key].trim() ||
        !Number.isInteger(number) ||
        number < min ||
        number > max
      ) {
        setError(`${label}须为 ${min}—${max} 的整数`);
        return;
      }
    }
    if (Number(draft.restore_threshold) <= Number(draft.forget_threshold)) {
      setError("恢复阈值 H 必须大于遗忘阈值 F");
      return;
    }
    if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(draft.maintenance_time)) {
      setError("维护时间须为 HH:MM 格式，范围 00:00—23:59");
      return;
    }
    const payload = {
      ...draft,
      ...Object.fromEntries(
        numericFields.map(([key]) => [key, Number(draft[key])]),
      ),
    };
    setBusy(true);
    try {
      const result = await api<{ lifecycle: LifecycleConfig }>(
        "/settings/lifecycle",
        json("PATCH", payload),
      );
      setDraft(draftOf(result.lifecycle));
      dirty.current = false;
      setSaved(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel settings-panel full-width">
      <h2>记忆生命周期</h2>
      <p className="lifecycle-help">
        有效记忆低于 F 时遗忘，遗忘记忆达到 H
        时恢复；两阈值之间保持原状态。置顶不自动恢复。
      </p>
      <form onSubmit={submit} noValidate>
        <fieldset disabled={busy} className="lifecycle-fieldset">
          <div className="lifecycle-settings-grid">
            {numericFields.map(([key, label, min, max]) => (
              <label key={key}>
                {label}
                <input
                  aria-label={label}
                  type="number"
                  required
                  min={min}
                  max={max}
                  step={1}
                  value={draft[key]}
                  onChange={(e) => change(key, e.target.value)}
                />
                <small>
                  {min}—{max}，整数
                </small>
              </label>
            ))}
            <label>
              维护时间
              <input
                aria-label="维护时间"
                required
                type="text"
                inputMode="text"
                placeholder="03:00"
                maxLength={5}
                value={draft.maintenance_time}
                onChange={(e) => change("maintenance_time", e.target.value)}
              />
              <small>HH:MM · 角色时区 {timezone || "未设置"}</small>
            </label>
          </div>
          <label className="check-label">
            <input
              type="checkbox"
              checked={draft.auto_delete_enabled}
              onChange={(e) => change("auto_delete_enabled", e.target.checked)}
            />
            自动删除遗忘记忆
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={draft.abandoned_retry_enabled}
              onChange={(e) =>
                change("abandoned_retry_enabled", e.target.checked)
              }
            />
            自动重试已放弃批次
          </label>
          <p className="lifecycle-help">
            维护按实际执行次数衰减，遗忘和置顶记忆不衰减。依据失效时每个依据只扣一次，置顶期间暂缓扣减。自动重试仅处理角色时区前一天放弃的批次，每批最多一次；内容拒绝不自动重试。
          </p>
          <p className="lifecycle-help">
            关闭自动删除后仍保留天数设置，“即将删除”列表为空。消息只有超过保留期、已结束学习且没有受保护的引用时才清理。新设置用于以后接受的维护，当前运行沿用接受时的设置。
          </p>
          {error && <Notice error>{error}</Notice>}
          {saved && <Notice>生命周期设置已保存</Notice>}
          <div className="actions">
            <button className="primary">
              {busy ? "正在保存…" : "保存生命周期设置"}
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => {
                setDraft(draftOf(value));
                dirty.current = false;
                setError("");
                setSaved(false);
              }}
            >
              还原为最近读取的设置
            </button>
          </div>
        </fieldset>
      </form>
    </section>
  );
}
