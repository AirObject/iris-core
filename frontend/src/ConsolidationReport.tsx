import { useState } from "react";
import { Badge, Pagination, RoleTime, seconds } from "./ui";
import { PersonaChecks } from "./PersonaDetails";
import { personaDegrees } from "./persona-labels";
import {
  consolidationReason,
  consolidationSwitches,
  suggestionKinds,
  suggestionStatuses,
} from "./consolidation-labels";
import type { MaintenanceItem, MaintenanceReport } from "./types";
import type { SourceExcerpts } from "./consolidation-types";

export function Excerpts({
  value,
  openMemory,
}: {
  value?: SourceExcerpts;
  openMemory: (id: number) => void;
}) {
  return (
    <details className="source consolidation-excerpts">
      <summary>查看来源摘录</summary>
      <p className="fine-print">
        本次模型判断时的来源摘录；完整来源与前后文可在记忆详情查看。
      </p>
      {!value?.length && <p>没有保存的来源摘录。</p>}
      {value?.map((group, index) => (
        <div key={`${group.memory_id}:${index}`}>
          <button
            className="text-button"
            onClick={() => openMemory(group.memory_id)}
          >
            记忆 #{group.memory_id}
          </button>
          {!group.sources.length && (
            <p className="quiet">没有可用的来源消息摘录。</p>
          )}
          {group.sources.map((s) => (
            <article className="consolidation-excerpt" key={s.id}>
              <p className="muted">
                消息 #{s.id} · {s.entry_name || s.entry_id} ·{" "}
                {(
                  { private: "私聊", group: "群聊", trial: "试用" } as Record<
                    string,
                    string
                  >
                )[s.entry_kind || ""] || s.entry_kind}{" "}
                · {s.sender_name || s.sender} ·{" "}
                <RoleTime value={s.at || s.occurred_at || null} />
              </p>
              <p className="fine-print">
                {(
                  {
                    message: "他人消息",
                    self_output: "角色输出",
                    action_result: "行动结果",
                    event: "事件",
                  } as Record<string, string>
                )[s.kind] || s.kind}
              </p>
              <blockquote>{s.text}</blockquote>
              {s.quote_content && (
                <div className="source-quote">
                  <small>原消息引用 {s.quote_author || ""}</small>
                  <blockquote>{s.quote_content}</blockquote>
                </div>
              )}
            </article>
          ))}
        </div>
      ))}
    </details>
  );
}

function ModelItems({
  name,
  items,
  openMemory,
}: {
  name: string;
  items: MaintenanceItem[];
  openMemory: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  return (
    <section className="detail-section" aria-label={name}>
      <h3>
        {name} <Badge>{items.length}</Badge>
      </h3>
      {name.includes("建议") && (
        <p className="fine-print">
          仅管理员可见。采纳只记录确认，不改正文和相信程度；已确认的建议也不进入宿主召回。
        </p>
      )}
      {!items.length && <p className="quiet">本次暂无记录。</p>}
      {items.slice(offset, offset + 30).map((item) => {
        const d = item.details;
        return (
          <article
            className="maintenance-item"
            key={`${item.phase}:${item.item_key}`}
          >
            {item.outcome === "merged" ? (
              <>
                <div className="actions">
                  {d.absorbed_ids?.map((id) => (
                    <button
                      key={id}
                      className="text-button"
                      onClick={() => openMemory(id)}
                    >
                      记忆 #{id}
                    </button>
                  ))}
                  <span>并入</span>
                  {d.result_id != null && (
                    <button
                      className="text-button"
                      onClick={() => openMemory(d.result_id!)}
                    >
                      记忆 #{d.result_id}
                    </button>
                  )}
                </div>
                <p>全部来源已归入保留记忆；被合并的记忆保留为占位。</p>
                <p>
                  来源消息：
                  {d.source_messages?.map((id) => `#${id}`).join("、") ||
                    "无消息来源"}{" "}
                  · 重定向派生依据 {d.redirected_dependencies ?? 0} 处
                </p>
              </>
            ) : (
              <>
                <Badge tone="warning">模型建议</Badge>{" "}
                <span>
                  {item.outcome === "conflicts" ? "矛盾检查" : "依赖复核"}
                </span>
                {d.suggestions?.map((s) => (
                  <div className="suggestion-summary" key={s.id}>
                    <p>
                      <Badge>{suggestionKinds[s.kind] || s.kind}</Badge>{" "}
                      <Badge>{suggestionStatuses[s.review_status]}</Badge> 建议
                      #{s.id}
                    </p>
                    <p>{s.text}</p>
                    {s.memory_id != null && (
                      <button
                        className="text-button"
                        onClick={() => openMemory(s.memory_id!)}
                      >
                        查看记忆 #{s.memory_id} 的建议
                      </button>
                    )}
                    {s.confirmed_at && (
                      <p className="muted">
                        管理员确认：
                        <RoleTime value={s.confirmed_at} />
                      </p>
                    )}
                    {s.cleared_at && (
                      <p className="muted">
                        管理员清除：
                        <RoleTime value={s.cleared_at} />
                      </p>
                    )}
                  </div>
                ))}
                {!d.suggestions?.length && item.memory_id != null && (
                  <button
                    className="text-button"
                    onClick={() => openMemory(item.memory_id!)}
                  >
                    查看记忆 #{item.memory_id}
                  </button>
                )}
              </>
            )}
            {d.report && <p className="report-text">{d.report}</p>}
            {!!d.before_memories?.length && (
              <details className="source">
                <summary>涉及的记忆与修订快照</summary>
                {d.before_memories.map((m) => {
                  const after = d.after_memories?.find((a) => a.id === m.id);
                  return (
                    <div key={m.id}>
                      <button
                        className="text-button"
                        onClick={() => openMemory(m.id)}
                      >
                        记忆 #{m.id}
                      </button>
                      <span>
                        {" "}
                        · 修订 {m.revision}
                        {after && ` → ${after.revision}`}
                      </span>
                      <p className="report-text">
                        {m.content || "正文已不可用"}
                      </p>
                      {after?.content !== m.content && (
                        <p className="report-text">
                          整理后：{after?.content || "正文已不可用"}
                        </p>
                      )}
                    </div>
                  );
                })}
              </details>
            )}
            <Excerpts value={d.source_excerpts} openMemory={openMemory} />
            <small>
              <RoleTime value={item.created_at} />
            </small>
          </article>
        );
      })}
      <Pagination total={items.length} offset={offset} change={setOffset} />
    </section>
  );
}

function GoalReviewReport({ data }: { data: MaintenanceReport }) {
  const [offset, setOffset] = useState(0);
  const items = data.items.filter((i) => i.outcome === "goals_reviewed");
  if (!data.goal_review && !items.length) return null;
  return (
    <section className="detail-section" aria-label="目标复核结果">
      <h3>目标复核结果</h3>
      <p>
        {data.goal_review?.enabled === false
          ? `未执行：${consolidationReason(data.goal_review.skip_reason || "disabled")}`
          : `已检查 ${data.goal_review?.checked ?? 0} 个目标，${items.length} 个目标的依据标注发生变化。`}
      </p>
      <p className="fine-print">
        复核只更新依据标注，不自动完成或放弃目标，也不改变提醒。
      </p>
      {items.slice(offset, offset + 30).map((i) => (
        <article className="maintenance-item" key={i.item_key}>
          <a href={`#/state?tab=goals&id=${i.object_id}`}>
            查看目标 #{i.object_id}
          </a>
          <p>
            新增或重新启用标注：
            {i.details.annotation_ids?.map((id) => `#${id}`).join("、") || "无"}
          </p>
          <p>
            结束的旧标注：
            {i.details.ended_annotation_ids?.map((id) => `#${id}`).join("、") ||
              "无"}
          </p>
          <small>
            <RoleTime value={i.created_at} />
          </small>
        </article>
      ))}
      <Pagination total={items.length} offset={offset} change={setOffset} />
    </section>
  );
}

export function ConsolidationReportSections({
  data,
  openMemory,
}: {
  data: MaintenanceReport;
  openMemory: (id: number) => void;
}) {
  const calls = data.summary.model_calls,
    p = data.persona;
  return (
    <>
      {data.consolidation && (
        <section className="detail-section" aria-label="本次整理与模型用量">
          <h3>本次整理与模型用量</h3>
          <p>
            后续待处理项目：{data.consolidation.deferred} 项
            {data.consolidation.skip_reason &&
              ` · ${consolidationReason(data.consolidation.skip_reason)}`}
          </p>
          {calls && (
            <>
              <p>
                模型调用 {calls.count} 次 · 输入 {calls.prompt_tokens ?? 0}{" "}
                token · 输出 {calls.completion_tokens ?? 0} token · 总耗时{" "}
                {seconds(calls.duration_ms ?? 0)}
              </p>
              <p className="fine-print">
                {calls.unknown_usage_calls ?? 0}{" "}
                次调用用量未知；用量为已知合计，包含实际重试及 persona 调用。
              </p>
            </>
          )}
          <details>
            <summary>本次接受任务时的整理设置</summary>
            <p>
              模型调用次数预算：
              {data.consolidation.settings.max_calls ?? "未记录"}
            </p>
            {consolidationSwitches.map(([key, label]) => (
              <p key={key}>
                {label}：
                {data.consolidation?.settings[key] == null
                  ? "未记录"
                  : data.consolidation.settings[key]
                    ? "开启"
                    : "关闭"}
              </p>
            ))}
            <p className="fine-print">设置修改只影响之后接受的新运行。</p>
          </details>
        </section>
      )}
      {(data.consolidation ||
        data.items.some((i) => i.phase === "consolidation")) && (
        <>
          <ModelItems
            name="记忆合并"
            items={data.items.filter((i) => i.outcome === "merged")}
            openMemory={openMemory}
          />
          <ModelItems
            name="整理建议（模型建议）"
            items={data.items.filter((i) =>
              ["conflicts", "dependencies_reviewed"].includes(i.outcome),
            )}
            openMemory={openMemory}
          />
        </>
      )}
      {p && (
        <section className="detail-section" aria-label="persona 变化">
          <h3>persona 变化</h3>
          <p>
            <Badge tone={p.status === "published" ? "green" : ""}>
              {(
                {
                  published: "已发布",
                  pending: "待确认，当前版本保留",
                  rejected: "已拒绝，当前版本保留",
                  not_due: "尚未到更新条件",
                  skipped: "已跳过",
                  failed: "失败",
                  conflict: "版本或依据冲突",
                  running: "更新中",
                } as Record<string, string>
              )[p.status] || p.status}
            </Badge>
            {p.change_degree &&
              ` · 变化程度：${personaDegrees[p.change_degree] || p.change_degree}`}
          </p>
          {p.reason && <p>{consolidationReason(p.reason)}</p>}
          {p.version_id && p.base_version_id ? (
            <a
              href={`#/persona?tab=history&version=${p.version_id}&before=${p.base_version_id}&after=${p.version_id}`}
            >
              对比 persona v{p.base_version_id} → v{p.version_id}
            </a>
          ) : (
            <a href="#/persona?tab=attempts">查看 persona 生成记录</a>
          )}
          {p.content && (
            <details>
              <summary>查看候选正文与检查</summary>
              <p className="persona-content">{p.content}</p>
              {p.checks && <PersonaChecks version={{ checks: p.checks }} />}
            </details>
          )}
        </section>
      )}
      <GoalReviewReport data={data} />
    </>
  );
}
