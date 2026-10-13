import type { ProfileBasis } from "./entry-profile-types";
export const degree = (value: string) =>
  ({ small: "小", medium: "中", large: "大" })[value] || value;
export const source = (value: string) =>
  ({
    periodic: "定期更新",
    regenerate: "重新生成",
    edit: "管理员编辑",
    rollback: "回滚",
  })[value] || value;
export const status = (value: string) =>
  ({
    published: "已发布",
    candidate: "待确认",
    rejected: "被拒绝",
    failed: "失败",
    conflict: "版本冲突",
    skipped: "已跳过",
  })[value] || value;
const reasons: Record<string, string> = {
  message_cleaned: "原消息已清理",
  message_changed: "原消息已变化",
  memory_unavailable: "记忆已失效或删除",
  memory_changed: "记忆已修改",
  visibility_changed: "记忆可见范围已变化",
  memory_sources_changed: "记忆来源已变化",
  disabled: "本入口画像已关闭",
  entry_kind: "私聊入口不生成画像",
  daily_limit: "今天已经尝试过生成，请在下一个角色本地日期重试",
  insufficient_messages: "首次生成需要最近 14 天至少 50 条消息",
  no_evidence: "没有可用的本入口依据",
  unconfigured: "尚未配置对话模型",
  daily_token_limit: "达到今日 token 上限",
  current_version_changed: "当前已发布版本已变化，结果未发布",
  settings_changed: "画像设置已变化，结果未发布",
  evidence_changed: "依据在生成期间发生变化，结果未发布",
  interrupted: "服务中断，任务未完成",
  unexpected_error: "生成失败，请查看运行状态",
  timeout: "模型调用超时",
  invalid_output: "模型输出格式无效，未发布",
  invalid_sentence: "候选句格式无效",
  too_many_deleted: "检查删除了超过 30% 的候选句",
  too_short: "正文低于旧版摘要的最少字数要求",
  too_long: "正文超过该版本的字数上限",
  superseded: "已被新候选或编辑取代",
  paused: "对话用途已暂停",
  invalid_items: "候选条目列表格式无效",
  invalid_item: "条目类型、名称或含义格式无效",
  invalid_quotes: "每个模型条目需要 1—3 条引语",
  invalid_quote: "引语格式无效或超过 100 字",
  invalid_quote_reference: "引语消息编号不在本入口材料中",
  quote_not_found: "引语不是消息正文或转引原话的连续片段",
  invalid_evidence_reference: "含义引用的记忆依据无效",
  duplicate_item: "条目重复",
  account_identifier: "包含账号标识或长数字号段",
  insufficient_topic_recurrence: "常聊话题缺少跨日期的独立消息正文依据",
  too_many_items: "条目数超过 6 条（包含手写条目）",
  token_budget: "超过画像的估算 token 预算",
  empty: "检查后没有可保留内容",
  administrator_rejected: "管理员拒绝",
};
export const reason = (value: string, format?: number) =>
  value === "too_many_deleted"
    ? format === 2
      ? "检查累计删除了超过 50% 的模型条目"
      : format === 1
        ? "检查删除了超过 30% 的候选句"
        : "检查删除比例超出该版本上限，请查看版本检查记录"
    : reasons[value] || value;
export const basisName = (basis: ProfileBasis) =>
  basis.memory_id
    ? `记忆 #${basis.memory_id} · 修订 ${basis.revision}`
    : `消息 #${basis.message_id}`;
