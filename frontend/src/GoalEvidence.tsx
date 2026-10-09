import { useState } from "react";
import { useData } from "./api";
import { Notice, Pagination, RoleTime } from "./ui";
import { SourceView } from "./Memory";
import { actorNames, operationNames } from "./Operations";
import {
  goalStates,
  goalKinds,
  goalOrigins,
  personName,
  entryName,
} from "./goal-ui";
import type {
  GoalCatalog,
  GoalDetail,
  GoalSource,
  GoalRevision,
  GoalHistoryStatus,
  Operation,
  Page,
} from "./types";

export function GoalSources({
  goal,
  openMemory,
}: {
  goal: GoalDetail;
  catalog: GoalCatalog | null;
  openMemory: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  const sources = useData<Page<GoalSource>>(
    `/goals/${goal.id}/sources?limit=30&offset=${offset}`,
  );
  return (
    <section className="panel goals-panel" aria-label="来源消息">
      <div className="panel-heading">
        <h2>来源消息</h2>
        <button className="text-button" onClick={sources.refresh}>
          刷新来源消息
        </button>
      </div>
      <p className="goal-help">
        前后文来自同一入口的相邻消息，只读查看不会成为新经历。
      </p>
      {sources.error && (
        <Notice error>
          {sources.error}
          <button onClick={sources.refresh}>重试来源消息</button>
        </Notice>
      )}
      {!sources.data && !sources.error && (
        <p role="status">正在读取来源消息…</p>
      )}
      {sources.data?.total === 0 && (
        <p className="goal-help">
          没有关联来源消息。宿主和管理员创建的目标可以没有对话依据。
        </p>
      )}
      {sources.data?.items.map((s) => (
        <div key={s.id}>
          {s.missing || !s.message ? (
            <div className="source">
              <p>来源消息 #{s.message_id}</p>
              <p className="goal-help">
                {s.notice || "来源消息已清理或不可用"}
              </p>
            </div>
          ) : (
            <SourceView
              source={{
                id: s.id,
                kind: "message",
                message_id: s.message_id,
                message: s.message,
                context: s.context,
              }}
              openMemory={openMemory}
            />
          )}
        </div>
      ))}
      {sources.data && (
        <Pagination
          total={sources.data.total}
          offset={offset}
          change={setOffset}
        />
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
  kind: "类型",
  origin: "来源",
  entry_id: "产生入口",
  people: "涉及的人",
  closed_at: "结束时间",
  closed_by: "结束操作者",
  merged_into: "合并到目标",
  source_message_ids: "来源消息",
  promise_memories: "承诺记忆及依据修订",
  merged_goal_ids: "合并进来的目标",
  duplicates: "可能重复关系",
  basis_annotations: "依据复核标注",
};
const revisionActions: Record<string, string> = {
  create: "创建目标",
  update: "更新目标",
  merge: "合并目标",
  duplicate_dismiss: "驳回重复",
  possible_duplicate: "标记可能重复",
  basis_clear: "清除依据标注",
  basis_review: "复核目标依据",
  dedup: "目标去重判断",
};
const revisionReasons: Record<string, string> = {
  "goal created": "创建目标",
  "goal updated": "更新目标",
  "goals merged": "合并目标",
  "possible duplicate identified": "发现可能重复的目标",
  "possible duplicate dismissed": "驳回可能重复关系",
  "goal basis reviewed": "复核目标依据",
  "basis warning cleared by administrator": "管理员清除依据标注",
};
const relationStates: Record<string, string> = {
  possible: "可能重复",
  dismissed: "已驳回",
  merged: "已合并",
  active: "生效",
  cleared: "已清除",
  superseded: "已被新标注替代",
  resolved: "依据已恢复或移除",
};
function Value({
  field,
  value,
  catalog,
}: {
  field: string;
  value: unknown;
  catalog?: GoalCatalog | null;
}) {
  if (value === undefined) return <>未记录</>;
  if (value === null)
    return <>{field === "reminder_minutes" ? "使用默认值" : "未设置"}</>;
  if (
    (field === "deadline" || field === "closed_at") &&
    typeof value === "string"
  )
    return <RoleTime value={value} />;
  if (field === "reminder_minutes") return <>{String(value)} 分钟</>;
  if (field === "people" && Array.isArray(value))
    return (
      <>
        {value.map((id) => personName(String(id), catalog)).join("、") || "无"}
      </>
    );
  if (field === "entry_id") return <>{entryName(String(value), catalog)}</>;
  if (field === "state")
    return <>{goalStates[value as keyof typeof goalStates] || String(value)}</>;
  if (field === "kind")
    return <>{goalKinds[value as keyof typeof goalKinds] || String(value)}</>;
  if (field === "origin")
    return (
      <>{goalOrigins[value as keyof typeof goalOrigins] || String(value)}</>
    );
  if (field === "closed_by")
    return <>{actorNames[String(value)] || String(value)}</>;
  if (Array.isArray(value))
    return (
      <>
        {value
          .map((v) =>
            typeof v === "object" && v && "memory_id" in v
              ? `记忆 #${v.memory_id}（依据修订 ${v.memory_revision}）`
              : `#${v}`,
          )
          .join("、") || "无"}
      </>
    );
  if (typeof value === "object")
    return (
      <>
        {Object.entries(value)
          .map(
            ([id, state]) =>
              `#${id}：${relationStates[String(state)] || String(state)}`,
          )
          .join("；") || "无"}
      </>
    );
  return <>{String(value)}</>;
}
export function GoalOperations({
  id,
  catalog,
}: {
  id: number;
  catalog?: GoalCatalog | null;
}) {
  const [offset, setOffset] = useState(0),
    [operationOffset, setOperationOffset] = useState(0);
  const revisions = useData<Page<GoalRevision> & GoalHistoryStatus>(
    `/goals/${id}/revisions?limit=30&offset=${offset}`,
  );
  const list = useData<Page<Operation>>(
    `/operations?object_type=goal&object_id=${id}&limit=30&offset=${operationOffset}`,
  );
  return (
    <section className="panel goals-panel">
      <div className="panel-heading">
        <h2>修订与操作记录</h2>
        <button
          className="text-button"
          onClick={() => {
            revisions.refresh();
            list.refresh();
          }}
        >
          刷新目标记录
        </button>
      </div>
      <section aria-label="修订历史">
        <h3>修订历史</h3>
        <p className="goal-help">
          每次仅展示发生变化的字段；依据标注和重复关系的变化也会留下修订。
        </p>
        {revisions.error && (
          <Notice error>
            {revisions.error}
            <button onClick={revisions.refresh}>重试修订历史</button>
          </Notice>
        )}
        {!revisions.data && !revisions.error && (
          <p role="status">正在读取修订历史…</p>
        )}
        {revisions.data?.history_status === "pre_migration_no_snapshot" && (
          <Notice>
            迁移前无快照：截至修订 {revisions.data.missing_through_revision}{" "}
            的历史值无法还原。
          </Notice>
        )}
        {revisions.data?.items.length === 0 && (
          <p className="goal-help">暂无修订快照。</p>
        )}
        {revisions.data?.items.map((r) => (
          <article className="goal-operation" key={r.id}>
            <h4>
              {revisionActions[r.action] ||
                operationNames[`goal_${r.action}`] ||
                r.action}{" "}
              ·{" "}
              {r.revision_before == null ? "新建" : `修订 ${r.revision_before}`}{" "}
              → {r.revision_after}
            </h4>
            <p>
              <RoleTime value={r.created_at} /> ·{" "}
              {actorNames[r.actor] || r.actor}
            </p>
            <p className="report-text">
              {revisionReasons[r.reason] || r.reason}
            </p>
            <dl className="revision-values">
              {Array.from(
                new Set([...Object.keys(r.before), ...Object.keys(r.after)]),
              ).map((field) => (
                <div key={field}>
                  <dt>{fieldNames[field] || field}</dt>
                  <dd>
                    <span className="muted">修改前</span>
                    <p>
                      <Value
                        field={field}
                        value={r.before[field]}
                        catalog={catalog}
                      />
                    </p>
                  </dd>
                  <dd>
                    <span className="muted">修改后</span>
                    <p>
                      <Value
                        field={field}
                        value={r.after[field]}
                        catalog={catalog}
                      />
                    </p>
                  </dd>
                </div>
              ))}
            </dl>
          </article>
        ))}
        {revisions.data && (
          <Pagination
            total={revisions.data.total}
            offset={offset}
            change={setOffset}
          />
        )}
      </section>
      <details className="goal-operations">
        <summary>操作记录</summary>
        {list.error && <Notice error>{list.error}</Notice>}
        {!list.data && !list.error && <p role="status">正在读取操作记录…</p>}
        {list.data?.items.length === 0 && (
          <p className="goal-help">暂无操作记录。</p>
        )}
        {list.data?.items.map((o) => (
          <article className="goal-operation" key={o.id}>
            <h3>{operationNames[o.action] || o.action}</h3>
            <p>
              <RoleTime value={o.created_at} /> ·{" "}
              {actorNames[o.actor] || o.actor}
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
            offset={operationOffset}
            change={setOperationOffset}
          />
        )}
      </details>
      <a
        className="text-button"
        href={`#/operations?object_type=goal&object_id=${id}`}
      >
        查看完整操作记录
      </a>
    </section>
  );
}
