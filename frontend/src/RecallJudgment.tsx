import { Badge, seconds } from "./ui";
import type { Judgment, Prepared } from "./types";

const reasons: Record<string, string> = {
  no_candidates: "没有需要判断的相关候选",
  host_disabled: "本次请求已关闭判断",
  configuration_disabled: "设置中已关闭判断",
  unconfigured: "未配置判断模型",
  configuration_error: "模型配置错误",
  configuration: "模型配置或响应无效",
  configuration_changed: "调用期间模型配置发生变化",
  invalid_key: "密钥无效",
  authentication: "密钥无效",
  account_problem: "模型账户异常",
  account: "模型账户异常",
  usage_limit: "达到每日 token 上限",
  rate_limited: "服务商限流，正在退避",
  temporarily_unavailable: "模型用途已暂停",
  paused: "模型用途已暂停",
  queue_full: "判断排队已满",
  queue_timeout: "排队超时",
  timeout: "超过判断总预算",
  retryable: "模型连接暂时失败",
  invalid_output: "判断输出格式无效",
  content_rejection: "模型拒绝判断内容",
};
export function JudgmentSummary({ value }: { value?: Judgment }) {
  if (!value) return <p className="fine-print">本次未返回召回判断状态。</p>;
  return (
    <div className="judgment-summary" aria-label="本次召回判断">
      <p>
        <strong>召回判断：</strong>
        <Badge
          tone={
            value.status === "degraded"
              ? "danger"
              : value.status === "applied"
                ? "green"
                : ""
          }
        >
          {{ applied: "正常", disabled: "已关闭", degraded: "降级" }[
            value.status
          ] || value.status}
        </Badge>
      </p>
      {value.reason && (
        <p>{reasons[value.reason] || `判断原因：${value.reason}`}</p>
      )}
      <p>被判断去掉 {value.removed_memory_ids.length} 条记忆</p>
      {!!value.stale_memory_ids?.length && (
        <p>另有 {value.stale_memory_ids.length} 条记忆在复核时失效</p>
      )}
      <p className="muted">
        耗时 {seconds(value.duration_ms)} · 总预算 {value.budget_seconds}{" "}
        秒（含排队）
      </p>
      {value.status === "degraded" && <p>本次保留仍有效的原召回。</p>}
    </div>
  );
}
export function SubjectAnnotations({
  value,
}: {
  value: Prepared["memories"][number]["subject_annotations"];
}) {
  if (!value) return null;
  return (
    <div className="subject-annotations">
      {value.possible_same_as.map((link) => (
        <p key={link.link_id}>
          可能是同一人：{link.subjects.map((s) => s.name).join("、")} · 相信程度{" "}
          {link.belief} / 100。
          <a href={`#/people?id=${encodeURIComponent(link.subjects[0].id)}`}>
            核对人物联系
          </a>
        </p>
      ))}
      {value.roleplay.map((link) => (
        <p key={link.link_id}>
          扮演关系（虚构）：{link.actor.name} 扮演 {link.character.name} ·{" "}
          {link.worlds.map((w) => w ?? "场景未知").join("、")} · 相信程度{" "}
          {link.belief} / 100
        </p>
      ))}
    </div>
  );
}
