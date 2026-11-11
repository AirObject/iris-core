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
export const consolidationBudgets = [
  ["max_calls", "记忆整理（合并／矛盾／依赖）每轮调用上限", 300],
  ["entry_profile_max_calls", "入口画像每轮调用上限", 60],
  ["person_summary_max_calls", "人物摘要每轮调用上限", 40],
  ["persona_max_calls", "persona 每轮调用上限", 30],
] as const;
export const consolidationResolutions = [
  ["report_only_v1", "只报告（默认）：不改动记忆，由管理员决定"],
  ["auto_v1", "自主处理：模型判断，程序按规则执行，事后可撤销"],
] as const;
export const autoRelations: Record<string, string> = {
  supersedes: "本人已更正，旧记忆进入遗忘",
  reported_vs_self: "本人说法与转述冲突，转述降低一档",
  coexist: "历史与当前并存，不作处理",
  undecided: "无法判定，两条都保留并标注争议",
};
export const autoVerdicts: Record<string, string> = {
  forget: "没有独立依据，进入遗忘",
  keep: "仍有独立依据，不作改动",
  undecided: "复核依据未通过校验，本次不处理",
};
export const consolidationSteps: Record<string, string> = {
  memory: "记忆整理（合并／矛盾／依赖）",
  entry_profile: "入口画像",
  person_summary: "人物摘要",
  persona: "persona",
};
export const consolidationSwitches = [
  ["merge_enabled", "合并重复记忆"],
  ["conflict_enabled", "矛盾检查与建议"],
  ["dependency_enabled", "派生依据复核与建议"],
  ["persona_enabled", "定期更新 persona"],
  ["person_summary_enabled", "定期更新人物摘要"],
  ["goal_review_enabled", "目标依据复核"],
] as const;
export function consolidationReason(reason: string) {
  const labels: Record<string, string> = {
    disabled: "本次设置已关闭",
    protected: "人工编辑或置顶的记忆，未自动处理",
    superseded: "已被替代的记忆，等管理员恢复后才恢复有效",
    superseded_member: "含已被替代且遗忘的记忆，不合并也不建议",
    missing_unique_detail: "合并正文缺少某条独有细节",
    numbers_changed: "合并正文的数字与原文不一致",
    negation_changed: "合并正文的否定与原文不一致",
    qualifiers_changed: "合并正文的限定词与原文不一致",
    subject_mentions_changed: "合并正文的主体引用与原文不一致",
    time_expressions_changed: "合并正文的时间表达与原文不一致",
    entity_lost: "合并正文丢失了人名或实体",
    entity_added: "合并正文新增了人名或实体",
    new_content_word: "合并正文出现了原文没有的词",
    content_word_lost: "合并正文缺少原文的词",
    attribution_differs: "两条的说话人、立场或涉及人不同",
    event_time_differs: "两条的事件时间不同",
    visibility_differs: "两条的可见范围不同",
    merge_not_allowed: "两条不符合合并条件",
    entry_profile_kind: "入口画像不做正文合并",
    invalid_reason: "模型理由未通过校验",
    model_items_disabled: "记忆模型整理项目均已关闭",
    usage_limit: "每日 token 上限已用尽",
    paused: "模型用途已暂停",
    interrupted: "运行已中断",
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
