import { useEffect, useRef, useState } from "react";
import { api, errorText, useData } from "./api";
import { Notice, RoleTime } from "./ui";
import type { PersonaAttempt, PersonaVersion } from "./persona-types";
import { activeAttempt, attemptLabel, personaReason } from "./persona-labels";

export function PersonaTask({
  initial,
  path,
  onFinish,
}: {
  initial: PersonaAttempt;
  path?: string;
  onFinish: (attempt: PersonaAttempt) => void;
}) {
  const [task, setTask] = useState(initial),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  const finish = useRef(onFinish);
  finish.current = onFinish;
  const inProgress = activeAttempt(initial.state);
  useEffect(() => {
    if (!inProgress) {
      setTask(initial);
      setError("");
      return;
    }
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let again = true;
      try {
        const next = await api<PersonaAttempt>(
          path || `/persona/attempts/${initial.id}`,
          { signal: controller.signal },
        );
        if (!active) return;
        setTask(next);
        setError("");
        if (!activeAttempt(next.state)) {
          again = false;
          finish.current(next);
        }
      } catch (e) {
        if (active && !controller.signal.aborted) setError(errorText(e));
      }
      if (active && again) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
    // Poll one accepted task until terminal; snapshot refreshes must not restart it.
  }, [initial.id, inProgress, path, retry]);
  const version = useData<PersonaVersion>(
    task.state === "rejected" && task.version_id
      ? `/persona/versions/${task.version_id}`
      : null,
  );
  return (
    <section className="panel persona-task" aria-label="最近生成任务">
      <h2>生成任务 #{task.id}</h2>
      <p role="status">
        {attemptLabel(task.state, task.stage)}
        {task.state === "current" && task.version_id
          ? ` · v${task.version_id}`
          : ""}
      </p>
      {activeAttempt(task.state) && (
        <p>
          请求已接受，尚未发布。页面会持续查询生成与检查结果；离开后任务仍在服务端运行。
        </p>
      )}
      {task.reason && (
        <Notice error={task.state === "failed" || task.state === "conflict"}>
          {personaReason(task.reason)}
        </Notice>
      )}
      <p className="muted">
        基于 v{task.base_version_id} · 接受时间：
        <RoleTime value={task.created_at} />
        {task.finished_at && (
          <>
            {" "}
            · 结束时间：
            <RoleTime value={task.finished_at} />
          </>
        )}
      </p>
      {error && (
        <Notice error>
          {error}。任务结果尚未获知，将继续查询。
          <button onClick={() => setRetry((v) => v + 1)}>立即查询任务</button>
        </Notice>
      )}
      {version.error && (
        <Notice error>
          {version.error}
          <button onClick={version.refresh}>重试拒绝原因</button>
        </Notice>
      )}
      {version.data && (
        <div>
          <h3>拒绝原因</h3>
          {version.data.rejection_reasons.length ? (
            <ul>
              {version.data.rejection_reasons.map((r, i) => (
                <li key={i}>{personaReason(r)}</li>
              ))}
            </ul>
          ) : (
            <p>未记录拒绝原因，请查看版本检查结果。</p>
          )}
        </div>
      )}
      {task.version_id && (
        <a href={`#/persona?tab=history&version=${task.version_id}`}>
          查看结果版本 v{task.version_id}
        </a>
      )}
      <p>
        <a
          href={`#/operations?object_type=persona_attempt&object_id=${task.id}`}
        >
          查看生成操作记录
        </a>
      </p>
    </section>
  );
}
