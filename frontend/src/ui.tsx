import { useEffect, useRef, type ReactNode } from "react";
import type { Batch } from "./types";

export const time = (value?: string | null) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "暂无记录";
export const seconds = (value?: number | null) =>
  value == null ? "—" : `${(value / 1000).toFixed(1)} 秒`;
export const lifecycleLabel = (value: string) =>
  ({ active: "有效", forgotten: "已遗忘", deleted: "已删除" })[value] || value;
export const healthLabel = (value: string) =>
  ({
    normal: "正常",
    temporarily_unavailable: "暂时不可用",
    invalid_key: "密钥无效",
    configuration_error: "配置错误",
    account_problem: "账户异常",
    usage_limit: "达到今日用量上限",
  })[value] || value;
export const messageState = (value: string) =>
  ({
    pending: "已接收 · 待学习",
    batched: "已接收 · 批次处理中",
    learned: "学习已完成",
    abandoned: "学习放弃 · 未记住",
    refused: "内容拒绝 · 未记住",
  })[value] || value;
export function batchLabel(batch?: Batch | null) {
  if (!batch) return "暂无批次";
  if (batch.state === "succeeded")
    return batch.result?.created?.length || batch.result?.updated?.length
      ? `学习成功 · 新增 ${batch.result.created?.length || 0} / 更新 ${batch.result.updated?.length || 0}`
      : "学习成功，未形成新记忆";
  return (
    {
      running: "正在学习",
      waiting: batch.next_retry_at ? "失败后等待重试" : "等待学习",
      abandoned: "已放弃 · 未记住",
      refused: "已拒绝 · 未记住",
    }[batch.state] || batch.state
  );
}
export function Notice({
  children,
  error = false,
}: {
  children: ReactNode;
  error?: boolean;
}) {
  return (
    <div
      className={`notice ${error ? "error" : ""}`}
      role={error ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-mark" aria-hidden>
        ✳
      </span>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>(
      "button,input,textarea,select",
    );
    first?.focus();
    const keydown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close.current();
      }
      if (e.key === "Tab") {
        const targets = [
          ...(ref.current?.querySelectorAll<HTMLElement>(
            'button:not(:disabled),a[href],input,textarea,select,[tabindex="0"]',
          ) || []),
        ];
        const first = targets[0],
          last = targets[targets.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", keydown);
    return () => {
      document.body.style.overflow = oldOverflow;
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, []);
  return (
    <div className="overlay">
      <section
        ref={ref}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="dialog-title">
          <h2>{title}</h2>
          <button
            className="icon-button"
            aria-label="关闭详情"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
