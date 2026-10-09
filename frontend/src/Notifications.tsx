import { useState } from "react";
import { useData } from "./api";
import { Badge, Empty, Notice, Pagination, RoleTime } from "./ui";
import { notificationStates, reminderKinds } from "./goal-ui";
import type { GoalNotification, Page } from "./types";

export function NotificationCard({
  note,
  openGoal,
}: {
  note: GoalNotification;
  openGoal: (id: number) => void;
}) {
  return (
    <article className="notification-card">
      <div className="panel-heading">
        <h3>{reminderKinds[note.reminder_kind]}</h3>
        <Badge tone={note.status === "pending" ? "purple" : ""}>
          {notificationStates[note.status]}
        </Badge>
      </div>
      <p className="goal-content">{note.content}</p>
      <div className="goal-time-lines">
        <p>
          发布时间：
          <RoleTime value={note.published_at} />
        </p>
        <p>
          计划时间：
          <RoleTime value={note.scheduled_at} />
        </p>
        {note.deadline_at && (
          <p>
            发布时的截止时间：
            <RoleTime value={note.deadline_at} />
          </p>
        )}
        {note.taken_at && (
          <p>
            取走时间：
            <RoleTime value={note.taken_at} />
          </p>
        )}
        {note.cancelled_at && (
          <p>
            取消时间：
            <RoleTime value={note.cancelled_at} />
          </p>
        )}
      </div>
      <button className="text-button" onClick={() => openGoal(note.goal_id)}>
        查看目标 #{note.goal_id}
      </button>
    </article>
  );
}
export default function Notifications({
  goalId,
  openGoal,
}: {
  goalId?: number;
  openGoal: (id: number) => void;
}) {
  const [status, setStatus] = useState(goalId ? "" : "pending");
  const [offset, setOffset] = useState(0);
  const query = new URLSearchParams({ limit: "30", offset: String(offset) });
  if (status) query.set("status", status);
  if (goalId) query.set("goal_id", String(goalId));
  const list = useData<Page<GoalNotification>>(
    `/notifications?${query}`,
    offset === 0 ? 2000 : 0,
  );
  return (
    <section className="panel goals-panel" aria-label="提醒列表">
      <div className="panel-heading">
        <h2>{goalId ? "该目标的提醒" : "提醒"}</h2>
        <button className="text-button" onClick={list.refresh}>
          刷新提醒
        </button>
      </div>
      <p className="goal-help">
        “已取走”只表示宿主拿到了提醒，不代表已送达用户或目标完成。此处查看不会取走提醒。
      </p>
      <label className="notification-filter">
        提醒状态
        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">全部</option>
          {Object.entries(notificationStates).map(([value, label]) => (
            <option value={value} key={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      {list.error && (
        <Notice error>
          {list.error}。{list.data && "以下为上次读取结果。"}
        </Notice>
      )}
      {!list.data && !list.error && <p role="status">正在读取提醒…</p>}
      {list.data?.items.length === 0 && (
        <Empty title="当前范围没有提醒">
          有截止时间的普通目标会在临近、到期时产生提醒；询问不安排提醒。
        </Empty>
      )}
      {list.data?.items.map((note) => (
        <NotificationCard key={note.id} note={note} openGoal={openGoal} />
      ))}
      {list.data && (
        <Pagination
          total={list.data.total}
          offset={offset}
          change={setOffset}
        />
      )}
    </section>
  );
}
