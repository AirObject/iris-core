import { GoalBasisReview, GoalDedupReview } from "./GoalReview";
import { useState } from "react";
import { api, ApiError, errorText, json, useData } from "./api";
import { Dialog, Notice, RoleTime, lifecycleLabel } from "./ui";
import GoalForm from "./GoalForm";
import Notifications from "./Notifications";
import { GoalSources, GoalOperations } from "./GoalEvidence";
import { GoalBadges, GoalFacts, reminderKinds } from "./goal-ui";
import { actorNames } from "./Operations";
import type {
  Goal,
  GoalCatalog,
  GoalDetail,
  GoalBasisAnnotation,
} from "./types";

type Action =
  | { kind: "completed" | "abandoned"; goal: Goal }
  | { kind: "merge"; goal: Goal; other: Goal }
  | { kind: "basis_clear"; goal: Goal; annotation: GoalBasisAnnotation };
function Duplicate({
  goal,
  otherId,
  locked,
  catalog,
  openGoal,
  merge,
  dismiss,
}: {
  goal: Goal;
  otherId: number;
  locked: boolean;
  catalog: GoalCatalog | null;
  openGoal: (id: number) => void;
  merge: (other: Goal) => void;
  dismiss: (other: Goal) => void;
}) {
  const other = useData<GoalDetail>(`/goals/${otherId}`);
  return (
    <article className="goal-duplicate">
      <h3>可能重复的目标 #{otherId}</h3>
      {other.error && (
        <Notice error>
          {other.error}
          <button onClick={other.refresh}>重试读取重复目标</button>
        </Notice>
      )}
      {!other.data && !other.error && <p>正在读取…</p>}
      {other.data && (
        <>
          <p className="goal-content">{other.data.content}</p>
          <GoalBadges goal={other.data} />
          <GoalFacts goal={other.data} catalog={catalog} />
          <div className="actions">
            <button className="secondary" onClick={() => openGoal(otherId)}>
              查看目标 #{otherId}
            </button>
            <button
              disabled={
                locked ||
                other.loading ||
                !!other.error ||
                goal.state !== "open" ||
                other.data.state !== "open" ||
                !!other.data.merged_into
              }
              onClick={() => merge(other.data!)}
            >
              合并目标 #{otherId}
            </button>
            <button
              disabled={
                locked ||
                other.loading ||
                !!other.error ||
                !!other.data.merged_into
              }
              onClick={() => dismiss(other.data!)}
            >
              驳回重复 #{otherId}
            </button>
          </div>
        </>
      )}
    </article>
  );
}
export default function GoalDetails({
  id,
  catalog,
  openMemory,
  openGoal,
  onBack,
  onChange,
}: {
  id: number;
  catalog: GoalCatalog | null;
  openMemory: (id: number) => void;
  openGoal: (id: number) => void;
  onBack: () => void;
  onChange: () => void;
}) {
  const [editing, setEditing] = useState<Goal | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false),
    [stale, setStale] = useState(false);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const snapshot = useData<GoalDetail>(
    `/goals/${id}`,
    editing || action || stale ? 0 : 2000,
  );
  const goal = snapshot.data;
  const locked = busy || stale || snapshot.loading || !!snapshot.error;
  function reload() {
    setEditing(null);
    setAction(null);
    setError("");
    setNotice("");
    setStale(false);
    snapshot.refresh();
  }
  async function write(
    path: string,
    body: Record<string, unknown>,
    method = "PATCH",
  ) {
    if (busy || stale) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<Goal>(path, json(method, body));
      setEditing(null);
      setAction(null);
      snapshot.refresh();
      onChange();
      if (path.endsWith("/merge")) {
        setNotice("已合并，以下为保留目标。");
        openGoal(result.id);
      } else
        setNotice(
          path.endsWith("/dismiss") ? "已驳回可能重复关系" : "目标已更新",
        );
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) {
        setStale(true);
        setAction(null);
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="goal-detail">
      <div className="actions">
        <button className="text-button" disabled={busy} onClick={onBack}>
          ← 返回目标列表
        </button>
        <button className="secondary" disabled={busy} onClick={reload}>
          {stale ? "载入最新目标" : "刷新目标详情"}
        </button>
      </div>
      {!action && (error || snapshot.error) && (
        <Notice error>
          {error || snapshot.error}
          {stale && "。请载入最新目标后重新核对；重新加载会放弃当前编辑草稿。"}
        </Notice>
      )}
      {notice && <Notice>{notice}</Notice>}
      {!goal && !snapshot.error && <p role="status">正在读取目标详情…</p>}
      {goal && (
        <>
          <section className="panel goals-panel">
            <div className="panel-heading">
              <h2>目标 #{goal.id}</h2>
              <span className="muted">修订 {goal.revision}</span>
            </div>
            <GoalBadges goal={goal} />
            {goal.merged_into && (
              <Notice>
                此目标已合并到 #{goal.merged_into}，原记录与依据保留。
                <button
                  className="text-button"
                  onClick={() => openGoal(goal.merged_into!)}
                >
                  查看保留目标 #{goal.merged_into}
                </button>
              </Notice>
            )}
            {editing ? (
              <GoalForm
                goal={editing}
                catalog={catalog}
                busy={busy}
                locked={stale || !!snapshot.error}
                cancel={() => setEditing(null)}
                submit={(body) =>
                  void write(`/goals/${id}`, {
                    ...body,
                    expected_revision: editing.revision,
                  })
                }
              />
            ) : (
              <>
                <p className="goal-content goal-detail-content">
                  {goal.content}
                </p>
                <GoalFacts goal={goal} catalog={catalog} />
                <div className="goal-time-lines">
                  <p>
                    创建时间：
                    <RoleTime value={goal.created_at} />
                  </p>
                  <p>
                    最后更新：
                    <RoleTime value={goal.updated_at} />
                  </p>
                  {goal.state !== "open" && (
                    <p>
                      结束时间：
                      <RoleTime value={goal.closed_at} /> ·{" "}
                      {goal.closed_by
                        ? actorNames[goal.closed_by] || goal.closed_by
                        : "操作者未知"}
                    </p>
                  )}
                </div>
                {goal.overdue && (
                  <p className="goal-help">
                    已过期只是标记，目标仍未结束。可修改截止时间，或明确完成、放弃。
                  </p>
                )}
                {!goal.merged_into && (
                  <div className="actions">
                    <button
                      className="secondary"
                      disabled={locked}
                      onClick={() => {
                        setEditing(goal);
                        setNotice("");
                      }}
                    >
                      编辑目标
                    </button>
                    {goal.state === "open" && (
                      <>
                        <button
                          disabled={locked}
                          onClick={() => setAction({ kind: "completed", goal })}
                        >
                          完成目标
                        </button>
                        <button
                          className="danger-text"
                          disabled={locked}
                          onClick={() => setAction({ kind: "abandoned", goal })}
                        >
                          放弃目标
                        </button>
                      </>
                    )}
                  </div>
                )}
              </>
            )}
          </section>
          <GoalBasisReview
            goal={goal}
            locked={locked || !!editing || !!action}
            openMemory={openMemory}
            clear={(annotation) =>
              setAction({ kind: "basis_clear", goal, annotation })
            }
          />
          <GoalDedupReview goal={goal} openGoal={openGoal} />
          {!goal.merged_into && goal.possible_duplicate_ids.length > 0 && (
            <section className="panel goals-panel">
              <h2>可能重复</h2>
              <p className="goal-help">
                相似不代表同一件事。核对双方内容、人物和时间后，再决定合并或驳回。
              </p>
              {goal.possible_duplicate_ids.map((otherId) => (
                <Duplicate
                  key={`${goal.revision}-${otherId}`}
                  goal={goal}
                  otherId={otherId}
                  locked={locked || !!editing}
                  catalog={catalog}
                  openGoal={openGoal}
                  merge={(other) => setAction({ kind: "merge", goal, other })}
                  dismiss={(other) =>
                    void write(
                      `/goals/${id}/duplicates/${other.id}/dismiss`,
                      {
                        expected_revision: goal.revision,
                        other_revision: other.revision,
                      },
                      "POST",
                    )
                  }
                />
              ))}
            </section>
          )}
          <GoalSources
            key={`sources-${goal.revision}`}
            goal={goal}
            catalog={catalog}
            openMemory={openMemory}
          />
          <section className="panel goals-panel">
            <h2>同证据的承诺记忆</h2>
            <p className="goal-help">
              与目标使用同一来源消息的角色记忆，保留当时依据修订。目标完成不会自动改写记忆。
            </p>
            {!goal.promise_memories.length && <p>暂无关联记忆。</p>}
            {goal.promise_memories.map((memory) => (
              <article className="goal-operation" key={memory.id}>
                <p className="goal-content">{memory.content}</p>
                <p className="muted">
                  依据修订 {memory.revision} · 当前修订{" "}
                  {memory.current_revision} · {lifecycleLabel(memory.lifecycle)}
                  {memory.revision !== memory.current_revision
                    ? " · 依据已变化"
                    : ""}
                </p>
                <button
                  className="text-button"
                  onClick={() => openMemory(memory.id)}
                >
                  查看记忆 #{memory.id}
                </button>
              </article>
            ))}
          </section>
          <section className="panel goals-panel">
            <h2>合并进来的目标</h2>
            {!goal.merged_goals.length && (
              <p className="goal-help">暂无合并记录。</p>
            )}
            {goal.merged_goals.map((merged) => (
              <article className="goal-operation" key={merged.id}>
                <p className="goal-content">{merged.content}</p>
                <GoalFacts goal={merged} catalog={catalog} />
                <button
                  className="text-button"
                  onClick={() => openGoal(merged.id)}
                >
                  查看原目标 #{merged.id}
                </button>
              </article>
            ))}
          </section>
          <Notifications goalId={goal.id} openGoal={openGoal} />
          <section className="panel goals-panel">
            <details>
              <summary>提醒计划 · {goal.reminder_plans.length} 条</summary>
              <p className="goal-help">
                计划到点才发布提醒，尚未发布的计划不属于“待取走”提醒。
              </p>
              {goal.reminder_plans.map((plan) => (
                <div className="goal-operation" key={plan.id}>
                  <p>
                    {reminderKinds[plan.reminder_kind]} ·{" "}
                    {(
                      {
                        scheduled: "待发布",
                        published: "已发布",
                        skipped: "已跳过",
                        cancelled: "已取消",
                      } as Record<string, string>
                    )[plan.status] || plan.status}
                  </p>
                  <RoleTime value={plan.scheduled_at} />
                </div>
              ))}
            </details>
          </section>
          <GoalOperations
            key={`operations-${goal.revision}`}
            id={goal.id}
            catalog={catalog}
          />
        </>
      )}
      {action && (
        <Dialog
          title={
            action.kind === "basis_clear"
              ? "清除目标依据标注"
              : action.kind === "merge"
                ? "确认合并目标"
                : action.kind === "completed"
                  ? "完成目标"
                  : "放弃目标"
          }
          closeDisabled={busy}
          onClose={() => setAction(null)}
        >
          {error && <Notice error>{error}</Notice>}
          {action.kind === "basis_clear" ? (
            <div className="goal-confirm">
              <p>{action.annotation.text}</p>
              <p>
                清除只撤下这次观察的标注并保留修订记录，不改变目标正文、状态、截止时间或提醒；依据再次变化时仍可能出现新标注。
              </p>
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy || stale}
                  onClick={() =>
                    void write(
                      `/goals/${id}/basis-annotations/${action.annotation.id}`,
                      { expected_revision: action.goal.revision },
                      "DELETE",
                    )
                  }
                >
                  确认清除依据标注
                </button>
                <button disabled={busy} onClick={() => setAction(null)}>
                  取消
                </button>
              </div>
            </div>
          ) : action.kind === "merge" ? (
            <div className="goal-confirm">
              <p>
                保留较早的目标 #
                {
                  [action.goal, action.other].sort(
                    (a, b) =>
                      Date.parse(a.created_at) - Date.parse(b.created_at) ||
                      a.id - b.id,
                  )[0].id
                }
                ，另一条保留为指向它的合并记录。
              </p>
              <p>
                来源与待取提醒归入保留目标，重复提醒可能取消；已取走的记录仍保留。保留目标的正文不自动拼接；明确截止时间补入空值，两方都设提前量时采用较大的分钟数。
              </p>
              {[action.goal, action.other].map((g) => (
                <div key={g.id}>
                  <h3>
                    目标 #{g.id} · 修订 {g.revision}
                  </h3>
                  <p className="goal-content">{g.content}</p>
                  <GoalFacts goal={g} catalog={catalog} />
                </div>
              ))}
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy || stale}
                  onClick={() =>
                    void write(
                      `/goals/${id}/duplicates/${action.other.id}/merge`,
                      {
                        expected_revision: action.goal.revision,
                        other_revision: action.other.revision,
                      },
                      "POST",
                    )
                  }
                >
                  确认合并
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => setAction(null)}
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <div className="goal-confirm">
              <p>{action.goal.content}</p>
              <p>
                确认{action.kind === "completed" ? "完成" : "放弃"}
                后，尚未取走的提醒会取消，已取走的记录保留。这是明确改变目标状态。
              </p>
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy || stale}
                  onClick={() =>
                    void write(`/goals/${id}`, {
                      state: action.kind,
                      expected_revision: action.goal.revision,
                    })
                  }
                >
                  {action.kind === "completed" ? "确认完成" : "确认放弃"}
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => setAction(null)}
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </Dialog>
      )}
    </div>
  );
}
