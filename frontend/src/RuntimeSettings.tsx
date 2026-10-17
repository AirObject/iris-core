import { useState, type FormEvent } from "react";
import { Notice } from "./ui";
import { changedSettings, useSettingsDraft } from "./settings-draft";
import type { GoalConfig } from "./types";

export type LimitsConfig = {
  daily_token_limit: number | null;
  learning_concurrency: number;
  native_image_learning?: boolean;
  media_retention_days?: number;
};

export function LimitsSettings({
  value,
  busy,
  saveError,
  nativeImagesAvailable,
  chatEnabled,
  external,
  save,
}: {
  value: LimitsConfig;
  busy: boolean;
  saveError?: string;
  nativeImagesAvailable: boolean;
  chatEnabled: boolean;
  external: boolean;
  save: (
    changes: Partial<LimitsConfig>,
  ) => Promise<LimitsConfig | null | undefined>;
}) {
  const current = {
    daily_token_limit: value.daily_token_limit,
    learning_concurrency: value.learning_concurrency,
    native_image_learning: value.native_image_learning === true,
    media_retention_days: value.media_retention_days ?? 7,
  };
  const { draft, change, reset } = useSettingsDraft({
    daily_token_limit: current.daily_token_limit?.toString() || "",
    learning_concurrency: String(current.learning_concurrency),
    native_image_learning: current.native_image_learning,
    media_retention_days: String(current.media_retention_days),
  });
  const [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    const limit = draft.daily_token_limit.trim()
      ? Number(draft.daily_token_limit)
      : null;
    const concurrency = Number(draft.learning_concurrency);
    const days = Number(draft.media_retention_days);
    if (limit !== null && (!Number.isInteger(limit) || limit < 1)) {
      setError("每日 token 上限须为正整数，留空表示不限");
      return;
    }
    if (
      !draft.learning_concurrency.trim() ||
      !Number.isInteger(concurrency) ||
      concurrency < 1 ||
      concurrency > 32
    ) {
      setError("学习并发须为 1—32 的整数");
      return;
    }
    if (
      !draft.media_retention_days.trim() ||
      !Number.isInteger(days) ||
      days < 1 ||
      days > 36500
    ) {
      setError("原图保留天数须为 1—36500 的整数");
      return;
    }
    const changes = changedSettings(current, {
      daily_token_limit: limit,
      learning_concurrency: concurrency,
      native_image_learning: draft.native_image_learning,
      media_retention_days: days,
    });
    if (!Object.keys(changes).length) return;
    if (await save(changes)) {
      reset();
      setSaved(true);
    }
  }
  return (
    <section className="panel settings-panel" aria-label="用量与学习">
      <h2>用量与学习</h2>
      <form onSubmit={submit} noValidate onChange={() => setSaved(false)}>
        <fieldset className="lifecycle-fieldset" disabled={busy}>
          <label>
            每日 token 上限
            <input
              aria-label="每日 token 上限"
              type="number"
              min={1}
              step={1}
              value={draft.daily_token_limit}
              placeholder="不限"
              onChange={(e) => change("daily_token_limit", e.target.value)}
            />
          </label>
          <label>
            学习并发
            <input
              aria-label="学习并发"
              type="number"
              required
              min={1}
              max={32}
              step={1}
              value={draft.learning_concurrency}
              onChange={(e) => change("learning_concurrency", e.target.value)}
            />
          </label>
          <p className="muted">
            按角色时区每日恢复；在途调用和未报告的 token
            可能使实际用量超过上限。
          </p>
          <label>
            原图保留天数
            <input
              aria-label="原图保留天数"
              type="number"
              required
              min={1}
              max={36500}
              step={1}
              value={draft.media_retention_days}
              onChange={(e) => change("media_retention_days", e.target.value)}
            />
            <small>1—36500 的整数，默认 7 天；与消息保留天数分别设置。</small>
          </label>
          <p className="lifecycle-help">
            到期由每日维护只清理原图字节，理解文本保留。作为记忆来源的图片、正在理解的图片受保护。
          </p>
          <label className="check-label">
            <input
              type="checkbox"
              aria-describedby="native-image-learning-help native-image-learning-availability"
              checked={draft.native_image_learning}
              disabled={busy || !nativeImagesAvailable}
              onChange={(e) =>
                change("native_image_learning", e.target.checked)
              }
            />
            学习时直接看原图
          </label>
          <p className="lifecycle-help" id="native-image-learning-help">
            默认关闭。开启后，后续学习把原图交给对话模型；图片描述仍用于回复准备、检索与管理页。超限或不可用的图片沿用描述或占位。
          </p>
          <p className="lifecycle-help" id="native-image-learning-availability">
            {!nativeImagesAvailable
              ? `${!chatEnabled ? "当前对话模型未启用，无法直接看原图。" : "当前对话模型未启用图片输入（vision），此设置暂不生效。"}${external ? "请在外部模型配置中启用对话模型及 vision。" : "请先启用并保存对话模型的“支持图片输入（vision）”。"}`
              : external
                ? "这是运行设置，使用外部模型配置时也可以保存。"
                : "使用已保存的对话模型图片能力；保存后对后续学习生效。"}
          </p>
          {(error || saveError) && <Notice error>{error || saveError}</Notice>}
          {saved && <Notice>用量与学习设置已保存</Notice>}
          <button className="primary">保存用量与学习</button>
        </fieldset>
      </form>
    </section>
  );
}

export function GoalSettings({
  value,
  busy,
  saveError,
  save,
}: {
  value: GoalConfig;
  busy: boolean;
  saveError?: string;
  save: (
    changes: Partial<GoalConfig>,
  ) => Promise<GoalConfig | null | undefined>;
}) {
  const current = {
    ...value,
    internal_question_expiry_days: value.internal_question_expiry_days ?? 7,
  };
  const { draft, change, reset } = useSettingsDraft({
    default_reminder_minutes: String(current.default_reminder_minutes),
    overdue_reminders: current.overdue_reminders,
    internal_question_expiry_days: String(
      current.internal_question_expiry_days,
    ),
  });
  const [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    const minutes = Number(draft.default_reminder_minutes),
      days = Number(draft.internal_question_expiry_days);
    if (
      !draft.default_reminder_minutes.trim() ||
      !Number.isInteger(minutes) ||
      minutes < 0 ||
      minutes > 525600
    ) {
      setError("默认提醒提前量须为 0—525600 的整数");
      return;
    }
    if (
      !draft.internal_question_expiry_days.trim() ||
      !Number.isInteger(days) ||
      days < 1 ||
      days > 365
    ) {
      setError("学习产生的询问保留天数须为 1—365 的整数");
      return;
    }
    const changes = changedSettings(current, {
      default_reminder_minutes: minutes,
      overdue_reminders: draft.overdue_reminders,
      internal_question_expiry_days: days,
    });
    if (!Object.keys(changes).length) return;
    if (await save(changes)) {
      reset();
      setSaved(true);
    }
  }
  return (
    <section className="panel settings-panel" aria-label="目标与提醒">
      <h2>目标与提醒</h2>
      <p>
        未单独填写提前量的普通目标使用默认值。询问不安排提醒，过期不会自动放弃目标。
      </p>
      <form onSubmit={submit} noValidate onChange={() => setSaved(false)}>
        <fieldset className="lifecycle-fieldset" disabled={busy}>
          <label>
            默认提醒提前量（分钟）
            <input
              aria-label="默认提醒提前量（分钟）"
              type="number"
              min={0}
              max={525600}
              step={1}
              required
              value={draft.default_reminder_minutes}
              onChange={(e) =>
                change("default_reminder_minutes", e.target.value)
              }
            />
            <small>0—525600 的整数，默认 60 分钟；0 表示到期时提醒。</small>
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={draft.overdue_reminders}
              onChange={(e) => change("overdue_reminders", e.target.checked)}
            />
            启用过期提醒
          </label>
          <p className="lifecycle-help">
            过期提醒每日最多一次。
            <a href="#/state?tab=notifications">查看提醒</a>
            ；“已取走”只表示宿主拿到了。
          </p>
          <label>
            学习产生的询问保留天数
            <input
              aria-label="学习产生的询问保留天数"
              type="number"
              min={1}
              max={365}
              step={1}
              required
              value={draft.internal_question_expiry_days}
              onChange={(e) =>
                change("internal_question_expiry_days", e.target.value)
              }
            />
            <small>1—365 的整数，默认 7 天。</small>
          </label>
          <p className="lifecycle-help">
            只作用于学习产生且仍未结束的询问，到期由每日维护自动结束。宿主或管理员创建的询问和普通目标不受影响。
          </p>
          {(error || saveError) && <Notice error>{error || saveError}</Notice>}
          {saved && <Notice>目标与提醒设置已保存</Notice>}
          <button className="primary">保存目标与提醒设置</button>
        </fieldset>
      </form>
    </section>
  );
}
