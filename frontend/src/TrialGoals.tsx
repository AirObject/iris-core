import { useData } from "./api";
import { Badge, Notice } from "./ui";
import { GoalBadges, GoalDeadline } from "./goal-ui";
import type { Goal, GoalNotification, Page } from "./types";

export default function TrialGoals({
  goals,
  error = "",
}: {
  goals?: Goal[];
  error?: string;
}) {
  const pending = useData<Page<GoalNotification>>(
    "/notifications?status=pending&limit=1&offset=0",
    2000,
  );
  const active = goals
    ?.filter((goal) => goal.state === "open" && !goal.merged_into)
    .slice(0, 10);
  return (
    <section className="panel trial-goals" aria-labelledby="trial-goals-title">
      <div className="panel-heading">
        <h2 id="trial-goals-title">未结束的目标与询问</h2>
        <Badge>{active?.length ?? "—"}</Badge>
      </div>
      <p className="goal-help">
        角色答应一件事 → 学习形成目标 →
        到点发布提醒。可在学习结束后点击目标查看来源；全部入口共享，优先展示临近、过期的目标，最多
        10 个。
      </p>
      {error && (
        <Notice error>
          目标暂不可读取：{error}。{active && "以下为上次读取结果。"}
        </Notice>
      )}
      {!active && !error && <p role="status">正在读取目标…</p>}
      {active?.length === 0 && <p className="quiet">暂无未结束的目标或询问</p>}
      {active?.map((goal) => (
        <a
          key={goal.id}
          className="trial-goal-link"
          href={`#/state?tab=goals&id=${goal.id}`}
        >
          <GoalBadges goal={goal} />
          <p className="goal-content">{goal.content}</p>
          <small>
            <GoalDeadline goal={goal} />
          </small>
          <span className="text-button">查看目标与来源 ↗</span>
        </a>
      ))}
      {pending.error && (
        <p className="danger-text">
          提醒暂不可读取：{pending.error}
          <button className="text-button" onClick={pending.refresh}>
            重试提醒
          </button>
        </p>
      )}
      <div className="actions">
        <a className="text-button" href="#/state?tab=goals">
          查看全部目标与询问
        </a>
        <a className="text-button" href="#/state?tab=notifications">
          查看提醒
          {pending.data?.total !== undefined
            ? ` · ${pending.data.total} 条待取走`
            : ""}
        </a>
      </div>
      <p className="goal-help">
        提醒被宿主取走不代表已送达或目标完成。这里的自动刷新不会取走提醒。
      </p>
    </section>
  );
}
