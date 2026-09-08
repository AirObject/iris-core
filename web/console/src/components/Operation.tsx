import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { terminal, type Operation } from "../api/design";
import {
  Empty,
  ErrorNotice,
  Reason,
  TextData,
  useEnvironment,
  useQuery,
} from "./core";
export const pollingDelay = (attempt: number) =>
  Math.min(10000, [1000, 2000, 5000][attempt] ?? 10000);
export function OperationPanel({
  initial,
  onChange,
}: {
  initial: Operation;
  onChange?: (op: Operation) => void;
}) {
  const [op, setOp] = useState(initial);
  useEffect(() => setOp(initial), [initial]);
  const [error, setError] = useState<unknown>();
  const [reason, setReason] = useState("");
  const [problems, setProblems] = useState(false);
  const [busy, setBusy] = useState(false);
  const cancelKey = useRef(crypto.randomUUID());
  const { bootstrap } = useEnvironment();
  useEffect(() => {
    let stopped = false;
    let finished = terminal(initial.status);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let attempt = 0;
    const tick = async () => {
      if (stopped || document.hidden || finished) return;
      controller = new AbortController();
      try {
        const r = await api.request<Operation>(
          `/operations/${encodeURIComponent(initial.id)}`,
          { signal: controller.signal },
        );
        if (stopped) return;
        setOp(r.data);
        onChange?.(r.data);
        if (terminal(r.data.status)) {
          finished = true;
          return;
        }
      } catch (e) {
        if (controller.signal.aborted) return;
        setError(e);
      }
      if (!stopped)
        timer = setTimeout(() => void tick(), pollingDelay(attempt++));
    };
    const visibility = () => {
      clearTimeout(timer);
      controller?.abort();
      if (!document.hidden && !finished)
        timer = setTimeout(() => void tick(), pollingDelay(attempt));
    };
    if (!terminal(initial.status) && !document.hidden)
      timer = setTimeout(() => void tick(), pollingDelay(attempt++));
    document.addEventListener("visibilitychange", visibility);
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [initial.id, initial.status, onChange]);
  const cancel = async () => {
    setBusy(true);
    try {
      const r = await api.action<Operation>(`/operations/${op.id}:cancel`, {
        key: cancelKey.current,
        method: "POST",
        body: { reason_code: reason },
        userActivity: true,
      });
      setOp(r.data);
      onChange?.(r.data);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel operation">
      <h3>Operation · {op.kind}</h3>
      <p>
        <strong>{op.status}</strong> · {op.phase ?? "等待阶段信息"} · {op.id}
      </p>
      {op.progress && (
        <p>
          {op.progress.processed} / {op.progress.total ?? "未知总量"}{" "}
          {op.progress.unit}
        </p>
      )}
      {op.blocked_reason && (
        <p className="notice">阻塞原因：{op.blocked_reason}</p>
      )}
      {op.status === "cancelled_partial" && (
        <p className="notice">
          {op.kind === "memory_forget"
            ? "后续批次已停止。已提交的删除仍然生效，取消不会恢复这些内容。"
            : "后续批次已停止，已提交内容仍保留。可按报告进行受控补偿。"}
        </p>
      )}
      <ErrorNotice error={error} />
      {op.cancellable && (
        <>
          <Reason
            codes={op.reason_codes ?? []}
            value={reason}
            onChange={(value) => {
              setReason(value);
              cancelKey.current = crypto.randomUUID();
            }}
          />
          <button
            disabled={
              busy ||
              !reason ||
              bootstrap.read_only ||
              bootstrap.maintenance ||
              (op.available_actions !== undefined &&
                !op.available_actions.includes("cancel"))
            }
            onClick={() => void cancel()}
          >
            停止后续批次
          </button>
        </>
      )}
      {op.problems_count > 0 && (
        <button onClick={() => setProblems(!problems)}>
          问题列表 ({op.problems_count})
        </button>
      )}
      {problems && <Problems path={`/operations/${op.id}/problems`} />}
    </section>
  );
}
export function Problems({ path }: { path: string }) {
  const [cursor, setCursor] = useState("");
  const q = useQuery<unknown[]>(
    `${path}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  return (
    <>
      <ErrorNotice error={q.error} />
      {q.loading ? (
        <p>加载问题…</p>
      ) : q.data?.length ? (
        <TextData value={q.data} />
      ) : (
        <Empty />
      )}
      {q.meta?.page?.has_more && (
        <button onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}>
          下一页问题
        </button>
      )}
    </>
  );
}
