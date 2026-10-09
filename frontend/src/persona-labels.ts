export const personaSources: Record<string, string> = {
  initial_setting: "初始设定",
  periodic: "定期更新",
  regenerate: "重新生成",
  admin_edit: "管理员编辑",
  rollback: "回滚",
};
export const personaStatuses: Record<string, string> = {
  current: "当前",
  pending: "待确认",
  rejected: "已拒绝",
  superseded: "已被后续候选替代",
  history: "历史已发布",
};
export const personaDegrees: Record<string, string> = {
  small: "小",
  medium: "中",
  large: "大",
};
export const personaOrigins: Record<string, string> = {
  memory: "记忆依据",
  admin: "手写",
  initial_template: "初始模板",
};
export const personaReasons: Record<string, string> = {
  modified: "记忆已修改",
  forgotten: "记忆已遗忘",
  deleted: "记忆已删除或清除",
  no_longer_self_memory: "已不属于自我记忆",
  sources_changed: "来源已变化",
  daily_token_limit: "达到每日用量上限",
  no_self_evidence: "暂无有效自我记忆依据",
  not_due: "尚未满足定期更新条件",
  evidence_changed: "依据已变化",
  current_version_changed: "当前版本已变化",
  interrupted: "服务中断，任务未完成",
  worker_unavailable: "生成工作线程不可用",
  unconfigured: "尚未配置生成模型",
  unexpected_error: "生成时发生异常",
  timeout: "模型调用超时",
  invalid_output: "模型输出不符合要求",
  paused: "模型用途已暂停",
  usage_limit: "达到每日用量上限",
  temporarily_unavailable: "模型暂时不可用",
  configuration_error: "模型配置错误",
  invalid_key: "模型密钥无效",
  account_problem: "模型账户异常",
  rate_limited: "模型限流",
  "administrator rejected": "管理员拒绝采用",
  "sentences must be an array": "句子列表格式无效",
  "empty persona": "persona 正文为空",
  "persona exceeds 800 characters": "persona 正文超过 800 字",
  "residual memory or participant reference": "正文残留记忆或参与者编号",
  "under 300 characters: concise evidence-bound text is allowed":
    "不足 300 字：允许依据有限时保持简短",
  "initial template retains the pre-M3 length behavior":
    "初始模板沿用此前的长度规则",
  "nonempty text required": "句子不能为空",
  "one sentence per item required": "每项须为一句话",
  "invalid administrator attribution": "手写归属无效",
  "missing or unknown evidence": "缺少依据或依据未知",
  "invalid model verdict": "模型判断格式无效",
  "single-date/unknown-date evidence needs a scene qualifier":
    "单日或日期未知的依据需要场景限定",
  "invalid change degree": "变化程度无效",
  "model check must cover every sentence, in order":
    "模型检查须按顺序覆盖每句话",
  "model check reason required": "模型检查缺少理由",
};
export function personaReason(value: string): string {
  const sentence = value.match(/^sentence (\d+):\s*(.*)$/);
  return sentence
    ? `第 ${sentence[1]} 句：${personaReason(sentence[2])}`
    : personaReasons[value] || value;
}
export const activeAttempt = (state?: string) =>
  state === "queued" || state === "running";
export function attemptLabel(state: string, stage: string) {
  if (activeAttempt(state))
    return (
      (
        {
          queued: "等待生成",
          generating: "正在生成候选",
          checking: "正在检查候选",
        } as Record<string, string>
      )[stage] || "正在生成候选"
    );
  return (
    (
      {
        current: "已发布 persona",
        pending: "候选待确认，当前版本仍保留",
        rejected: "候选被拒绝，当前版本仍保留",
        skipped: "本次未生成候选",
        conflict: "生成期间版本或依据发生变化，未发布",
        failed: "生成失败，当前版本仍保留",
      } as Record<string, string>
    )[state] || state
  );
}
