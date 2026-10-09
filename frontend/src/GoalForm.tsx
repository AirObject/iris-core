import { useState, type FormEvent } from "react";
import { Notice } from "./ui";
import { GoalDeadline, personName } from "./goal-ui";
import type { Goal, GoalCatalog } from "./types";

const localStamp = (value: string) => {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};
export default function GoalForm({
  goal,
  catalog,
  busy,
  locked = false,
  submit,
  cancel,
}: {
  goal?: Goal;
  catalog: GoalCatalog | null;
  busy: boolean;
  locked?: boolean;
  submit: (body: Record<string, unknown>) => void;
  cancel: () => void;
}) {
  const [content, setContent] = useState(goal?.content || "");
  const [kind, setKind] = useState<Goal["kind"]>(goal?.kind || "normal");
  const [mode, setMode] = useState(goal ? "keep" : "none");
  const [date, setDate] = useState("");
  const [stamp, setStamp] = useState(
    goal?.deadline && !goal.deadline_unresolved
      ? localStamp(goal.deadline)
      : "",
  );
  const [lead, setLead] = useState(goal?.reminder_minutes?.toString() ?? "");
  const [people, setPeople] = useState<string[]>([]);
  const [entry, setEntry] = useState("");
  const [error, setError] = useState("");
  function save(event: FormEvent) {
    event.preventDefault();
    if (busy || locked) return;
    setError("");
    if (!content.trim() || content.trim().length > 4000) {
      setError("目标正文须为 1—4000 个字符且不为空白");
      return;
    }
    const body: Record<string, unknown> = { content: content.trim() };
    if (kind === "normal") {
      if (
        lead.trim() &&
        (!Number.isInteger(Number(lead)) ||
          Number(lead) < 0 ||
          Number(lead) > 525600)
      ) {
        setError("提醒提前量须为 0—525600 的整数，留空使用默认值");
        return;
      }
      if (!goal || lead !== (goal.reminder_minutes?.toString() ?? ""))
        body.reminder_minutes = lead.trim() ? Number(lead) : null;
      if (mode === "date") {
        if (
          !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
          !Number.isFinite(Date.parse(date)) ||
          new Date(date).toISOString().slice(0, 10) !== date
        ) {
          setError("请选择有效的截止日期");
          return;
        }
        body.deadline = date;
      } else if (mode === "datetime") {
        const parsed = new Date(stamp);
        if (
          !stamp ||
          !Number.isFinite(parsed.getTime()) ||
          localStamp(parsed.toISOString()) !==
            (stamp.length === 16 ? stamp + ":00" : stamp)
        ) {
          setError("请选择有效的截止时间");
          return;
        }
        body.deadline = parsed.toISOString();
      } else if (mode === "none") body.deadline = null;
    } else if (!goal) {
      body.deadline = null;
      body.reminder_minutes = null;
    }
    if (!goal) {
      if (people.length > 100) {
        setError("涉及的人最多 100 位");
        return;
      }
      Object.assign(body, { kind, people, entry_id: entry || null });
    }
    submit(body);
  }
  return (
    <form className="goal-form" onSubmit={save} noValidate>
      {error && <Notice error>{error}</Notice>}
      <fieldset className="lifecycle-fieldset" disabled={busy || locked}>
        {!goal && (
          <label>
            创建类型
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as Goal["kind"])}
            >
              <option value="normal">普通目标</option>
              <option value="question">询问</option>
            </select>
          </label>
        )}
        <label>
          目标正文
          <textarea
            required
            maxLength={4000}
            rows={4}
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
        </label>
        {kind === "question" ? (
          <p className="lifecycle-help">
            询问没有截止时间和提醒，由宿主决定何时询问。
          </p>
        ) : (
          <>
            {goal && (
              <p>
                当前截止时间：
                <GoalDeadline goal={goal} />
              </p>
            )}
            <div className="form-grid">
              <label>
                截止方式
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  {goal && <option value="keep">保持当前截止时间</option>}
                  <option value="none">不设截止时间</option>
                  <option value="date">指定日期</option>
                  <option value="datetime">指定日期和时间</option>
                </select>
              </label>
              <label>
                提醒提前量（分钟）
                <input
                  type="number"
                  min={0}
                  max={525600}
                  step={1}
                  value={lead}
                  placeholder="留空使用默认值"
                  onChange={(e) => setLead(e.target.value)}
                />
              </label>
              {mode === "date" && (
                <label>
                  截止日期
                  <input
                    aria-label="截止日期"
                    type="date"
                    required
                    value={date}
                    onChange={(e) => setDate(e.target.value)}
                  />
                  <small>按角色时区当天 23:59:59 截止。</small>
                </label>
              )}
              {mode === "datetime" && (
                <label>
                  截止日期和时间
                  <input
                    aria-label="截止日期和时间"
                    type="datetime-local"
                    step={1}
                    required
                    value={stamp}
                    onChange={(e) => setStamp(e.target.value)}
                  />
                  <small>
                    按本机时区{" "}
                    {Intl.DateTimeFormat().resolvedOptions().timeZone}{" "}
                    输入；保存后按角色时区显示。
                  </small>
                </label>
              )}
            </div>
            <p className="lifecycle-help">
              留空提前量使用默认值；0
              表示到期时提醒。修改期限或提前量会更新尚未取走的提醒，已取走的记录保留。
            </p>
          </>
        )}
        {!goal && (
          <div className="form-grid">
            <label>
              涉及的人
              <select
                aria-label="涉及的人"
                multiple
                size={4}
                value={people}
                onChange={(e) =>
                  setPeople(
                    Array.from(e.target.selectedOptions, (o) => o.value),
                  )
                }
              >
                {catalog?.people
                  .filter(
                    (p) =>
                      !["self", "scene"].includes(p.id) &&
                      !["self", "scene"].includes(p.kind || ""),
                  )
                  .map((p) => (
                    <option value={p.id} key={p.id}>
                      {personName(p.id, catalog)}
                    </option>
                  ))}
              </select>
              <small>可多选，也可不指定。</small>
            </label>
            <label>
              记录产生入口
              <select
                aria-label="记录产生入口"
                value={entry}
                onChange={(e) => setEntry(e.target.value)}
              >
                <option value="">不指定</option>
                {catalog?.entries.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name} · {e.id}
                  </option>
                ))}
              </select>
              <small>仅记录产生位置，所有入口共享此目标。</small>
            </label>
          </div>
        )}
        {!goal && (
          <p className="lifecycle-help">
            管理员创建的目标独立保留；发现相似项只标“可能重复”，由管理员明确合并或驳回。
          </p>
        )}
        <div className="actions">
          <button className="primary">
            {busy ? "正在保存…" : goal ? "保存目标" : "创建"}
          </button>
        </div>
      </fieldset>
      <button
        type="button"
        className="secondary"
        disabled={busy}
        onClick={cancel}
      >
        取消
      </button>
    </form>
  );
}
