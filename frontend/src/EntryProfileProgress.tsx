import { Badge, fullTime } from "./ui";
import { incrementalReason } from "./entry-profile-labels";
import type { ProfileProgress, ProfileWork } from "./entry-profile-types";

export function ProfileProgressView({
  progress,
}: {
  progress?: ProfileProgress | null;
}) {
  if (!progress) return null;
  return (
    <section aria-label="当前入口进度与积压">
      <h4>当前入口进度与积压</h4>
      <dl className="profile-progress-grid">
        <div>
          <dt>已完成观察分块</dt>
          <dd>
            {progress.completed_chunks} / {progress.observation_chunks} 块
          </dd>
        </div>
        <div>
          <dt>待检查</dt>
          <dd>{progress.pending_checks} 页</dd>
        </div>
        <div>
          <dt>检查失败</dt>
          <dd>{progress.failed_checks} 页</dd>
        </div>
        <div>
          <dt>检查未通过</dt>
          <dd>{progress.rejected_checks} 页</dd>
        </div>
        <div>
          <dt>未准入画像</dt>
          <dd>{progress.waiting_candidates} 条</dd>
        </div>
        <div>
          <dt>待扫描</dt>
          <dd>{progress.unscanned_messages} 条消息</dd>
        </div>
        <div>
          <dt>观察缺口</dt>
          <dd>{progress.observation_gaps} 块</dd>
        </div>
        <div>
          <dt>最早积压时间</dt>
          <dd>
            {progress.oldest_pending_at
              ? fullTime(progress.oldest_pending_at)
              : "无"}
          </dd>
        </div>
        <div>
          <dt>已排队至</dt>
          <dd>消息 #{progress.scan_cursor}</dd>
        </div>
        <div>
          <dt>已连续完成至</dt>
          <dd>消息 #{progress.applied_cursor}</dd>
        </div>
      </dl>
      <p className="fine-print">
        页数和记忆条目数分别统计；未准入不等于失败。消息编号表示处理位置，不是发生时间；连续完成位置不会越过观察缺口。
      </p>
    </section>
  );
}

function workState(work: ProfileWork) {
  if (work.state === "applied")
    return work.kind === "admit" ? "本页通过" : "已完成";
  if (work.state === "rejected")
    return work.kind === "admit" ? "本页未通过" : "未通过";
  return (
    (
      {
        queued: "排队中",
        running: "处理中",
        failed: "本次失败",
        cancelled: "已取消",
        deferred: "稍后重试",
      } as Record<string, string>
    )[work.state] || work.state
  );
}

export function ProfileWorkList({ work }: { work?: ProfileWork[] }) {
  if (!work?.length) return null;
  return (
    <details className="history profile-work-list">
      <summary>本次任务分块与检查 · {work.length} 项</summary>
      <p className="fine-print">
        每页单独检查；全部页通过后才可能准入，还需核对最终正文与依据。宿主是否能看到，以每条画像的投影状态为准。
      </p>
      {work.map((item) => (
        <article
          key={item.id}
          aria-label={`工作项 #${item.id}`}
          className="profile-work-card"
        >
          <div className="detail-badges">
            <strong>
              {item.kind === "observe" ? "观察消息分块" : "画像准入检查"}
            </strong>
            <Badge
              tone={
                item.state === "failed" || item.state === "rejected"
                  ? "danger"
                  : ""
              }
            >
              {workState(item)}
            </Badge>
            <span className="muted">工作项 #{item.id}</span>
          </div>
          {item.memory_id != null && (
            <button
              className="text-button"
              onClick={() =>
                document
                  .getElementById(`profile-memory-${item.memory_id}`)
                  ?.scrollIntoView?.({ behavior: "smooth", block: "start" })
              }
            >
              画像记忆 #{item.memory_id}
            </button>
          )}
          {item.kind === "admit" && (
            <p>
              {item.check_page != null && item.check_pages != null
                ? `检查第 ${item.check_page} / ${item.check_pages} 页`
                : "检查页码未记录"}
            </p>
          )}
          {item.kind === "admit" && item.state === "applied" && (
            <p className="fine-print">
              全部页通过后才可能准入，本页通过不代表整条已展示。
            </p>
          )}
          <dl className="profile-work-metadata">
            <div>
              <dt>所属日期（角色时区）</dt>
              <dd>{item.local_day || "未记录"}</dd>
            </div>
            {item.kind === "observe" && (
              <div>
                <dt>消息编号范围</dt>
                <dd>
                  #{item.cursor_before} 之后至 #{item.cursor_after}
                </dd>
              </div>
            )}
            <div>
              <dt>执行次数</dt>
              <dd>{item.attempts} 次</dd>
            </div>
            <div>
              <dt>下次重试</dt>
              <dd>{item.retry_at ? fullTime(item.retry_at) : "未安排"}</dd>
            </div>
            <div>
              <dt>入队时间</dt>
              <dd>{fullTime(item.created_at)}</dd>
            </div>
            {item.finished_at && (
              <div>
                <dt>结束时间</dt>
                <dd>{fullTime(item.finished_at)}</dd>
              </div>
            )}
          </dl>
          {item.reason && <p>{incrementalReason(item.reason)}</p>}
        </article>
      ))}
    </details>
  );
}
