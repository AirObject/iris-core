import { personaReason } from "./persona-labels";

export const suggestionKinds: Record<string, string> = {
  disputed: "争议",
  insufficient_support: "依据不足",
  superseded: "已被替代",
};
export const suggestionStatuses = {
  pending: "待确认",
  confirmed: "已确认",
  cleared: "已清除",
};
export const consolidationSwitches = [
  ["merge_enabled", "合并重复记忆"],
  ["conflict_enabled", "矛盾检查与建议"],
  ["dependency_enabled", "派生依据复核与建议"],
  ["persona_enabled", "定期更新 persona"],
  ["goal_review_enabled", "目标依据复核"],
] as const;
export function consolidationReason(reason: string) {
  const labels: Record<string, string> = {
    disabled: "本次设置已关闭",
    model_items_disabled: "记忆模型整理项目均已关闭",
    call_budget: "模型调用次数预算已用尽",
    persona_busy: "已有 persona 任务正在运行",
    checks_rejected: "persona 检查未通过，候选被拒绝",
    revision_conflict: "对象修订已变化，跳过本次写入",
    unsafe_write: "未通过写入保护校验",
    invalid_response: "模型返回格式无效",
    invalid_json: "模型返回的 JSON 无效",
    temporary_unavailable: "模型暂时不可用",
    cooldown: "尚未到定期更新时间",
    no_changes: "没有需要更新的变化",
    stale: "判断期间目标或依据已变化，等待重新复核",
    lease_expired: "本次判断任务已超时，等待重新复核",
    configuration_changed: "判断设置已变化，等待重新复核",
    goal_closed: "目标已结束，取消待复核任务",
    in_progress: "已有复核任务正在运行",
    queue_full: "判断排队已满",
    configuration: "模型尚未配置或配置无效",
    budget: "预算已耗尽",
    invalid: "模型输出未通过校验",
    source_unavailable: "来源已不可用",
    insufficient_new_memories: "新增自我记忆不足，尚未到更新条件",
    too_soon: "距离上次更新尚不足一个周期",
  };
  return labels[reason] || personaReason(reason);
}
