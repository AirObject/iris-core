import { useState } from "react";
import { api, ApiError, errorText, json, useData } from "./api";
import { Badge, Dialog, Notice, Pagination, RoleTime } from "./ui";
import { dedupNames, goalAutoReason, goalStates } from "./goal-ui";
import { consolidationReason } from "./consolidation-labels";
import type {
  Goal,
  GoalAutoAction,
  GoalBasisAnnotation,
  GoalDetail,
  GoalDuplicateJudgment,
  GoalEvidenceMessage,
  Page,
} from "./types";

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

const decisionNames: Record<string, string> = {
  completed: "完成",
  abandoned: "放弃",
  open: "未结束",
  invalid: "输出无效",
};
const outcomeNames: Record<string, string> = {
  merged: "已合并",
  separated: "已撤销可能重复",
  kept: "保留可能重复",
};
export function UndoMergeConfirm({
  judgment,
  busy,
  error,
  confirm,
  cancel,
}: {
  judgment: Pick<GoalDuplicateJudgment, "goal_a" | "goal_b" | "target_id">;
  busy: boolean;
  error?: string;
  confirm: () => void;
  cancel: () => void;
}) {
  const source =
    judgment.target_id === judgment.goal_a ? judgment.goal_b : judgment.goal_a;
  return (
    <Dialog title="撤销自动合并" closeDisabled={busy} onClose={cancel}>
      {error && <Notice error>{error}</Notice>}
      <div className="goal-confirm">
        <p>
          恢复目标 #{source}，保留目标 #{judgment.target_id}
          的截止时间、提前量、来源和提醒退回合并前的状态。
        </p>
        <p>
          保留目标在合并后被修改过时不能撤销。撤销后这两条目标不再被自动合并。
        </p>
        <div className="actions">
          <button className="primary" disabled={busy} onClick={confirm}>
            确认撤销合并
          </button>
          <button className="secondary" disabled={busy} onClick={cancel}>
            取消
          </button>
        </div>
      </div>
    </Dialog>
  );
}
export function GoalEvidenceList({
  messages,
}: {
  messages: GoalEvidenceMessage[];
}) {
  if (!messages.length) return <p className="goal-help">没有依据消息。</p>;
  return (
    <ul className="goal-evidence">
      {messages.map((m) => (
        <li key={m.id}>
          {m.missing ? (
            <span className="muted">
              消息 #{m.id}：{m.notice || "消息已清理或不可用"}
            </span>
          ) : (
            <>
              <span className="muted">
                消息 #{m.id} · {m.sender_name || m.sender_subject_id} ·{" "}
                {m.entry_name || m.entry_id} ·{" "}
                <RoleTime value={m.occurred_at || null} />
              </span>
              <p className="goal-content">{m.content}</p>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}
export function GoalAutoReviewPanel({
  goal,
  locked,
  reopen,
  undoMerge,
  openGoal,
}: {
  goal: GoalDetail;
  locked: boolean;
  reopen: () => void;
  undoMerge: (judgment: GoalDuplicateJudgment) => void;
  openGoal: (id: number) => void;
}) {
  const auto = goal.auto_review;
  if (
    !auto ||
    (!auto.closure && !auto.reviews.length && !auto.duplicate_judgments.length)
  )
    return null;
  return (
    <section className="panel goals-panel" aria-label="每日维护自动复核">
      <h2>每日维护自动复核</h2>
      {auto.closure && (
        <article className="goal-operation">
          <p>
            <Badge tone="purple">
              自动{decisionNames[auto.closure.decision]}
            </Badge>{" "}
            {goalAutoReason(auto.closure.reason)} ·{" "}
            <RoleTime value={auto.closure.created_at} />
          </p>
          <h3>依据消息</h3>
          <GoalEvidenceList messages={auto.closure.evidence_messages || []} />
          {auto.reopenable && (
            <div className="actions">
              <button disabled={locked} onClick={reopen}>
                重开目标
              </button>
            </div>
          )}
        </article>
      )}
      {auto.reviews.length > 0 && (
        <details>
          <summary>复核记录 · {auto.reviews.length} 条</summary>
          {auto.reviews.map((r) => (
            <p key={r.id}>
              <RoleTime value={r.created_at} /> · 修订 {r.goal_revision} · 判断
              {decisionNames[r.decision]} · {goalAutoReason(r.reason)}
              {r.evidence.length > 0 &&
                ` · 依据消息 ${r.evidence.map((id) => "#" + id).join("、")}`}
            </p>
          ))}
        </details>
      )}
      {auto.duplicate_judgments.length > 0 && (
        <details>
          <summary>可能重复补判 · {auto.duplicate_judgments.length} 条</summary>
          {auto.duplicate_judgments.map((j) => {
            const other = j.goal_a === goal.id ? j.goal_b : j.goal_a;
            return (
              <div key={j.id}>
                <p>
                  <RoleTime value={j.created_at} /> · 与目标 #{other} ·{" "}
                  {outcomeNames[j.outcome]} · {goalAutoReason(j.reason)}
                </p>
                <div className="actions">
                  <button
                    className="text-button"
                    onClick={() => openGoal(other)}
                  >
                    查看目标 #{other}
                  </button>
                  {j.undoable && (
                    <button disabled={locked} onClick={() => undoMerge(j)}>
                      撤销合并 #{j.id}
                    </button>
                  )}
                  {j.undone_at && <span className="muted">已撤销</span>}
                </div>
              </div>
            );
          })}
        </details>
      )}
      <p className="goal-help">
        只有学习产生的普通目标会按依据自动完成或放弃；宿主和管理员创建的目标不会被自动关闭。重开后，只有更晚的新消息才可能让它再次被关闭。
      </p>
    </section>
  );
}
export function ReopenConfirm({
  goal,
  busy,
  error,
  confirm,
  cancel,
}: {
  goal: Pick<Goal, "id" | "content" | "revision">;
  busy: boolean;
  error?: string;
  confirm: () => void;
  cancel: () => void;
}) {
  return (
    <Dialog title="重开目标" closeDisabled={busy} onClose={cancel}>
      {error && <Notice error>{error}</Notice>}
      <div className="goal-confirm">
        <p>{goal.content}</p>
        <p>
          重开后目标恢复未结束并重新安排提醒。之后每日维护只会参考比这次重开更晚的消息，不会凭原来的依据再次关闭它。
        </p>
        <div className="actions">
          <button className="primary" disabled={busy} onClick={confirm}>
            确认重开
          </button>
          <button className="secondary" disabled={busy} onClick={cancel}>
            取消
          </button>
        </div>
      </div>
    </Dialog>
  );
}
export function GoalAutoActions({
  openGoal,
  onChange,
}: {
  openGoal: (id: number) => void;
  onChange: () => void;
}) {
  const [open, setOpen] = useState(false),
    [offset, setOffset] = useState(0);
  const list = useData<Page<GoalAutoAction>>(
    open ? `/goal-auto-actions?limit=10&offset=${offset}` : null,
  );
  const [target, setTarget] = useState<
    Extract<GoalAutoAction, { type: "review" }>["goal"] | null
  >(null);
  const [merge, setMerge] = useState<Extract<
    GoalAutoAction,
    { type: "duplicate" }
  > | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  async function reopen() {
    if (!target || busy) return;
    setBusy(true);
    setError("");
    try {
      await api<Goal>(
        `/goals/${target.id}/reopen`,
        json("POST", { expected_revision: target.revision }),
      );
      setNotice(`目标 #${target.id} 已重开`);
      setTarget(null);
      list.refresh();
      onChange();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "目标已变化，请刷新后重新核对。"
          : errorText(e),
      );
    } finally {
      setBusy(false);
    }
  }
  async function undo() {
    if (!merge || busy) return;
    setBusy(true);
    setError("");
    try {
      await api<Goal>(
        `/goal-duplicate-judgments/${merge.id}/undo`,
        json("POST", { expected_revision: merge.target_revision }),
      );
      setNotice(`已撤销目标 #${merge.goal_a} 与 #${merge.goal_b} 的自动合并`);
      setMerge(null);
      list.refresh();
      onChange();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "保留目标在合并后已变化，不能撤销；请到目标页核对。"
          : errorText(e),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel goals-panel" aria-label="每日维护自动处理">
      <button
        className="text-button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        每日维护自动处理的目标
      </button>
      {open && (
        <>
          <p className="goal-help">
            列出每日维护按依据自动完成、放弃的目标，以及可能重复补判后合并或撤销标记的目标。
          </p>
          {notice && <Notice>{notice}</Notice>}
          {list.error && <Notice error>{list.error}</Notice>}
          {!list.data && !list.error && <p role="status">正在读取…</p>}
          {list.data?.items.length === 0 && (
            <p className="goal-help">暂无自动处理记录。</p>
          )}
          {list.data?.items.map((item) =>
            item.type === "review" ? (
              <article className="goal-operation" key={`r${item.id}`}>
                <p>
                  <Badge tone="purple">
                    自动{decisionNames[item.decision]}
                  </Badge>{" "}
                  目标 #{item.goal.id} · 当前{goalStates[item.goal.state]} ·{" "}
                  <RoleTime value={item.created_at} />
                  {!item.current && " · 已不是当前状态"}
                </p>
                <p className="goal-content">{item.goal.content}</p>
                <GoalEvidenceList messages={item.evidence_messages} />
                <div className="actions">
                  <button
                    className="text-button"
                    onClick={() => openGoal(item.goal.id)}
                  >
                    查看目标 #{item.goal.id}
                  </button>
                  {item.reopenable && (
                    <button
                      onClick={() => {
                        setError("");
                        setTarget(item.goal);
                      }}
                    >
                      重开目标 #{item.goal.id}
                    </button>
                  )}
                </div>
              </article>
            ) : (
              <article className="goal-operation" key={`d${item.id}`}>
                <p>
                  <Badge>{outcomeNames[item.outcome]}</Badge> 目标 #
                  {item.goal_a} 与 #{item.goal_b} ·{" "}
                  {goalAutoReason(item.reason)} ·{" "}
                  <RoleTime value={item.created_at} />
                </p>
                {item.goals.map((g) => (
                  <p className="goal-content" key={g.id}>
                    #{g.id}：{g.content}
                  </p>
                ))}
                <div className="actions">
                  {item.goals.map((g) => (
                    <button
                      className="text-button"
                      key={g.id}
                      onClick={() => openGoal(g.id)}
                    >
                      查看目标 #{g.id}
                    </button>
                  ))}
                  {item.undoable && (
                    <button
                      onClick={() => {
                        setError("");
                        setMerge(item);
                      }}
                    >
                      撤销合并 #{item.id}
                    </button>
                  )}
                  {item.undone_at && <span className="muted">已撤销</span>}
                </div>
              </article>
            ),
          )}
          {list.data && (
            <Pagination
              total={list.data.total}
              limit={10}
              offset={offset}
              change={setOffset}
            />
          )}
        </>
      )}
      {merge && (
        <UndoMergeConfirm
          judgment={merge}
          busy={busy}
          error={error}
          confirm={() => void undo()}
          cancel={() => setMerge(null)}
        />
      )}
      {target && (
        <ReopenConfirm
          goal={target}
          busy={busy}
          error={error}
          confirm={() => void reopen()}
          cancel={() => setTarget(null)}
        />
      )}
    </section>
  );
}
