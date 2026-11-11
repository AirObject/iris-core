import { Badge, RoleTime } from "./ui";
import type { Goal, GoalCatalog, GoalReceipt } from "./types";

export const goalStates = {
  open: "未结束",
  completed: "已完成",
  abandoned: "已放弃",
};
export const goalKinds = { normal: "普通目标", question: "询问" };
export const goalOrigins = { internal: "内部", host: "宿主", admin: "管理员" };
export const reminderKinds = {
  soon: "临近提醒",
  due: "到期提醒",
  overdue: "过期提醒",
  immediate: "即时提醒",
};
export const notificationStates = {
  pending: "待取走",
  taken: "已取走",
  cancelled: "已取消",
};
export const dedupNames = {
  created: "新建",
  possible_duplicate: "可能重复，已分别保留",
  merged: "已合并",
  pending: "去重待复核，已保留",
};
export const personName = (id: string, catalog?: GoalCatalog | null) => {
  const person = catalog?.people.find((p) => p.id === id);
  return person ? `${person.name}（${id}）` : id;
};
export const entryName = (id: string | null, catalog?: GoalCatalog | null) =>
  id ? catalog?.entries.find((e) => e.id === id)?.name || id : "未指定";
export function GoalBadges({ goal }: { goal: Goal }) {
  return (
    <div className="detail-badges goal-badges">
      <Badge>{goalKinds[goal.kind]}</Badge>
      <Badge>{goalStates[goal.state]}</Badge>
      <Badge>{goalOrigins[goal.origin]}</Badge>
      {goal.overdue ? (
        <Badge tone="danger">已过期</Badge>
      ) : goal.due_soon ? (
        <Badge tone="purple">临近截止</Badge>
      ) : null}
      {goal.possible_duplicate && <Badge tone="warning">可能重复</Badge>}
      {goal.basis_needs_review && <Badge tone="warning">依据可能不成立</Badge>}
      {goal.auto_closed && (
        <Badge tone="purple">
          {goal.state === "abandoned" ? "维护自动放弃" : "维护自动完成"}
        </Badge>
      )}
      {goal.merged_into && <Badge>已合并</Badge>}
    </div>
  );
}
export function GoalDeadline({ goal }: { goal: Goal }) {
  return goal.kind === "question" ? (
    <>询问不设截止时间</>
  ) : !goal.deadline ? (
    <>未设截止时间</>
  ) : goal.deadline_unresolved ? (
    <span className="danger-text">
      {goal.deadline}（时间待明确，不安排提醒）
    </span>
  ) : (
    <RoleTime value={goal.deadline} />
  );
}
export function GoalFacts({
  goal,
  catalog,
}: {
  goal: Goal;
  catalog?: GoalCatalog | null;
}) {
  return (
    <dl className="goal-facts">
      <div>
        <dt>截止时间</dt>
        <dd>
          <GoalDeadline goal={goal} />
        </dd>
      </div>
      <div>
        <dt>提醒提前量</dt>
        <dd>
          {goal.kind === "question"
            ? "询问不安排提醒"
            : `${goal.effective_reminder_minutes} 分钟${goal.reminder_minutes === null ? "（使用默认值）" : ""}`}
        </dd>
      </div>
      <div>
        <dt>涉及的人</dt>
        <dd>
          {goal.people.map((id) => personName(id, catalog)).join("、") ||
            "未指定"}
        </dd>
      </div>
      <div>
        <dt>产生入口</dt>
        <dd>{entryName(goal.entry_id, catalog)}</dd>
      </div>
    </dl>
  );
}
export function GoalReceiptNotice({
  receipt,
  openGoal,
}: {
  receipt: GoalReceipt;
  openGoal: (id: number) => void;
}) {
  return (
    <div className="notice" role="status">
      <p>
        已创建目标 #{receipt.submitted_id} · {dedupNames[receipt.dedup.status]}
      </p>
      <button className="text-button" onClick={() => openGoal(receipt.goal.id)}>
        查看目标 #{receipt.goal.id}
      </button>
      {receipt.dedup.target_id &&
        receipt.dedup.target_id !== receipt.goal.id && (
          <button
            className="text-button"
            onClick={() => openGoal(receipt.dedup.target_id!)}
          >
            查看可能重复的目标 #{receipt.dedup.target_id}
          </button>
        )}
    </div>
  );
}
export const goalAutoReasons: Record<string, string> = {
  auto_completed: "依据明确，自动完成",
  auto_abandoned: "依据明确，自动放弃",
  decision_open: "判断仍未结束",
  invalid_output: "模型输出无效，保持未结束",
  content_rejection: "模型拒绝处理，保持未结束",
  evidence_empty: "未给出依据，保持未结束",
  evidence_outside_material: "依据不在材料内，保持未结束",
  evidence_missing: "依据消息已不存在，保持未结束",
  evidence_not_after_creation: "依据不晚于目标创建或重开，保持未结束",
  evidence_sender_not_involved: "依据不是涉及的人或角色所说，保持未结束",
  evidence_kind_not_allowed: "依据只有场景事件，保持未结束",
  auto_merged: "判为重复，已合并到较早的目标",
  auto_different: "判为不重复，已撤销可能重复标记",
  admin_not_merged: "判为重复，但含管理员创建的目标，不合并并撤销标记",
  uncertain: "仍不确定，保留可能重复标记",
  same_ambiguous: "判为重复但还有其他歧义，保留标记",
  partner_not_in_material: "对方不在去重材料中，保留标记",
  partner_not_judged: "没有得到对方的判断，保留标记",
  merge_blocked: "不满足合并条件，保留标记",
  no_candidates: "没有新的候选消息，未调用模型",
  already_judged: "目标与材料未变，未再次调用模型",
  deferred: "超出本次调用上限，留到下次",
  revision_conflict: "目标在判断期间已变化，下次再处理",
  dedup_judge_disabled: "目标去重判断已关闭",
  review_pending: "创建时的去重复核尚未结束",
  pair_changed: "可能重复关系已变化",
  disabled: "开关已关闭",
  timeout: "超时，下次再试",
  rate_limited: "限流，下次再试",
  paused: "模型用途已暂停",
  unconfigured: "模型未配置",
};
export const goalAutoReason = (value: string | null | undefined) =>
  value ? goalAutoReasons[value] || value : "";
