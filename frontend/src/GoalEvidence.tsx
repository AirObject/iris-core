import { useEffect, useRef, useState } from "react";
import { api, useData } from "./api";
import { Notice, Pagination, RoleTime } from "./ui";
import { SourceView } from "./Memory";
import { actorNames, operationNames } from "./Operations";
import { entryName, personName } from "./goal-ui";
import type {
  GoalCatalog,
  GoalDetail,
  MemoryDetail,
  Operation,
  Page,
  Source,
} from "./types";

export function GoalSources({
  goal,
  catalog,
  openMemory,
}: {
  goal: GoalDetail;
  catalog: GoalCatalog | null;
  openMemory: (id: number) => void;
}) {
  const [contexts, setContexts] = useState<Record<number, Source>>({});
  const [loaded, setLoaded] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function load() {
    if (busy) return;
    setBusy(true);
    setError("");
    const abort = new AbortController();
    controller.current = abort;
    const results = await Promise.allSettled(
      goal.promise_memories.map((m) =>
        api<MemoryDetail>(`/memories/${m.id}`, { signal: abort.signal }),
      ),
    );
    if (abort.signal.aborted) return;
    const found: Record<number, Source> = {};
    let failed = false;
    for (const result of results) {
      if (result.status === "rejected") {
        failed = true;
        continue;
      }
      for (const source of result.value.sources) {
        if (
          source.kind === "message" &&
          source.message_id &&
          source.message &&
          source.context?.length &&
          goal.sources.some((s) => s.id === source.message_id)
        )
          found[source.message_id] = source;
      }
    }
    setContexts(found);
    setLoaded(true);
    setBusy(false);
    if (failed) setError("部分同证据记忆读取失败，可重试载入前后文。");
  }
  return (
    <section className="panel goals-panel">
      <h2>来源消息</h2>
      {!goal.sources.length && (
        <p className="goal-help">
          没有关联来源消息。宿主和管理员创建的目标可以没有对话依据。
        </p>
      )}
      {!!goal.sources.length && !!goal.promise_memories.length && (
        <button
          className="text-button"
          disabled={busy}
          onClick={() => void load()}
        >
          {busy ? "正在读取前后文…" : "载入来源前后文"}
        </button>
      )}
      {error && <Notice error>{error}</Notice>}
      {goal.sources.map((source) =>
        contexts[source.id] ? (
          <SourceView
            key={source.id}
            source={contexts[source.id]}
            openMemory={openMemory}
          />
        ) : (
          <details key={source.id} className="source" open>
            <summary>
              来源消息 #{source.id} ·{" "}
              {source.sender_subject_id
                ? personName(source.sender_subject_id, catalog)
                : "未知发言人"}
            </summary>
            <p className="muted">
              {entryName(source.entry_id || null, catalog)} ·{" "}
              <RoleTime value={source.occurred_at || null} />
            </p>
            <blockquote>{source.content}</blockquote>
            {(loaded || !goal.promise_memories.length) && (
              <p className="goal-help">
                现有来源中没有可用的前后文；此处仅展示已保存的来源原文。
              </p>
            )}
          </details>
        ),
      )}
    </section>
  );
}
const fieldNames: Record<string, string> = {
  content: "正文",
  deadline: "截止时间",
  deadline_at: "截止时刻",
  reminder_minutes: "提醒提前量",
  state: "状态",
  closed_at: "结束时间",
  closed_by: "结束操作者",
};
export function GoalOperations({ id }: { id: number }) {
  const [offset, setOffset] = useState(0);
  const list = useData<Page<Operation>>(
    `/operations?object_type=goal&object_id=${id}&limit=30&offset=${offset}`,
  );
  return (
    <section className="panel goals-panel">
      <div className="panel-heading">
        <h2>修订与操作记录</h2>
        <button className="text-button" onClick={list.refresh}>
          刷新目标记录
        </button>
      </div>
      <p className="goal-help">
        记录修改字段和修改前修订号；历史正文及截止时间快照暂不可查看。
      </p>
      {list.error && <Notice error>{list.error}</Notice>}
      {!list.data && !list.error && <p role="status">正在读取操作记录…</p>}
      {list.data?.items.length === 0 && (
        <p className="goal-help">暂无操作记录。</p>
      )}
      {list.data?.items.map((o) => (
        <article className="goal-operation" key={o.id}>
          <h3>{operationNames[o.action] || o.action}</h3>
          <p>
            <RoleTime value={o.created_at} /> · {actorNames[o.actor] || o.actor}
          </p>
          {typeof o.details.revision_before === "number" && (
            <p>修改前修订 {o.details.revision_before}</p>
          )}
          {Array.isArray(o.details.fields) && (
            <p>
              修改字段：
              {o.details.fields
                .map((f) => fieldNames[String(f)] || String(f))
                .join("、")}
            </p>
          )}
          {typeof o.details.merged_id === "number" && (
            <p>合并进来的目标 #{o.details.merged_id}</p>
          )}
          {typeof o.details.other_id === "number" && (
            <p>驳回与目标 #{o.details.other_id} 的可能重复关系</p>
          )}
          {o.action === "goal_create" && <p>已创建目标。</p>}
        </article>
      ))}
      {list.data && (
        <Pagination
          total={list.data.total}
          offset={offset}
          change={setOffset}
        />
      )}
      <a
        className="text-button"
        href={`#/operations?object_type=goal&object_id=${id}`}
      >
        查看完整操作记录
      </a>
    </section>
  );
}
