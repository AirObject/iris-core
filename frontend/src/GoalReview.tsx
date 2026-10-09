import { Badge, RoleTime } from "./ui";
import { dedupNames } from "./goal-ui";
import { consolidationReason } from "./consolidation-labels";
import type { GoalBasisAnnotation, GoalDetail } from "./types";

export function GoalBasisReview({
  goal,
  locked,
  openMemory,
  clear,
}: {
  goal: GoalDetail;
  locked: boolean;
  openMemory: (id: number) => void;
  clear: (annotation: GoalBasisAnnotation) => void;
}) {
  if (!goal.basis_annotations?.length) return null;
  return (
    <section className="panel goals-panel" aria-label="目标依据复核">
      <h2>依据可能不成立</h2>
      <p className="goal-help">
        依据记忆已变化，请核对目标是否仍然成立；标注不会自动结束目标。
      </p>
      {goal.basis_annotations.map((a) => (
        <article className="goal-operation" key={a.id}>
          <p className="report-text">{a.text}</p>
          <p>
            依据修订 {a.basis_revision} · 观察到修订{" "}
            {a.observed_revision ?? "未知"}
          </p>
          <p className="muted">
            复核时间：
            <RoleTime value={a.created_at} />
          </p>
          <div className="actions">
            <button
              className="text-button"
              onClick={() => openMemory(a.memory_id)}
            >
              查看依据记忆 #{a.memory_id}
            </button>
            {a.observed_merged_into != null && (
              <button
                className="text-button"
                onClick={() => openMemory(a.observed_merged_into!)}
              >
                已合并到记忆 #{a.observed_merged_into}
              </button>
            )}
            {!goal.merged_into && (
              <button disabled={locked} onClick={() => clear(a)}>
                清除依据标注 #{a.id}
              </button>
            )}
          </div>
        </article>
      ))}
    </section>
  );
}
export function GoalDedupReview({
  goal,
  openGoal,
}: {
  goal: GoalDetail;
  openGoal: (id: number) => void;
}) {
  const review = goal.dedup_review;
  return (
    <section className="panel goals-panel" aria-label="目标去重判断">
      <h2>目标去重判断</h2>
      {!review ? (
        <p className="goal-help">
          没有异步复核任务记录。可能重复关系以当前目标标记为准。
        </p>
      ) : (
        <>
          <Badge tone={review.state === "done" ? "green" : "warning"}>
            {
              {
                pending: "待复核",
                running: "复核中",
                done: "已判断",
                cancelled: "已取消",
              }[review.state]
            }
          </Badge>
          <p>
            方法 {review.method} · 输入修订 {review.input_revision} · 尝试{" "}
            {review.attempts} 次
          </p>
          {review.result.status && (
            <p>
              判断结果：
              {dedupNames[review.result.status as keyof typeof dedupNames] ||
                review.result.status}
            </p>
          )}
          {review.result.reason && (
            <p>{consolidationReason(review.result.reason)}</p>
          )}
          {review.result.target_id != null && (
            <button
              className="text-button"
              onClick={() => openGoal(review.result.target_id!)}
            >
              查看判断关联目标 #{review.result.target_id}
            </button>
          )}
          {review.next_attempt_at && (
            <p>
              下次复核：
              <RoleTime value={review.next_attempt_at} />
            </p>
          )}
          <p className="muted">
            最后更新：
            <RoleTime value={review.updated_at} />
          </p>
          {["pending", "running"].includes(review.state) && (
            <p className="goal-help">
              已保留目标，等待模型复核；待复核不表示已经判定重复。
            </p>
          )}
        </>
      )}
      <p className="goal-help">
        管理员手动创建的目标不会自动合并，判断为重复也只标记“可能重复”。
      </p>
    </section>
  );
}
