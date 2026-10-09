import { useState } from "react";
import { useData } from "./api";
import Goals from "./Goals";
import Notifications from "./Notifications";
import { Badge, Empty, Notice, Pagination, RoleTime as StateTime } from "./ui";
import type {
  ActiveState,
  CurrentState,
  Page,
  StateReport,
  StateValue,
} from "./types";

const startBasis = { host: "宿主提供", first_report: "自首次报告起" };
const actions = {
  start: "开始活动",
  replace: "更换活动",
  update: "更新状态",
  heartbeat: "心跳",
  end: "结束活动",
};
const fields = {
  activity: "活动",
  mood: "情绪",
  started_at: "开始时间",
  start_time_basis: "开始时间依据",
};

function duration(value: number) {
  const seconds = Math.max(0, Math.floor(value));
  return (
    [
      [Math.floor(seconds / 86400), "天"],
      [Math.floor(seconds / 3600) % 24, "小时"],
      [Math.floor(seconds / 60) % 60, "分钟"],
      [seconds % 60, "秒"],
    ]
      .filter(([n]) => n !== 0)
      .map(([n, unit]) => `${n} ${unit}`)
      .join(" ") || "0 秒"
  );
}
function valueText(value: StateValue | null, quote = false) {
  if (value === null) return "无";
  if (typeof value === "boolean") return value ? "是（true）" : "否（false）";
  if (value === "") return "空文本";
  return quote && typeof value === "string" ? `“${value}”` : String(value);
}
function Mood({ state }: { state: ActiveState }) {
  return (
    <>
      {state.mood === null
        ? state.mood_updated_at
          ? "已清空"
          : "未报告"
        : valueText(state.mood)}
    </>
  );
}
function ReadError({ error, retained }: { error: string; retained: boolean }) {
  return error ? (
    <Notice error>
      当前状态读取失败：{error}。
      {retained && "以下为上次读取结果，可能尚未更新。"}
    </Notice>
  ) : null;
}

function StateDetails({ state }: { state: ActiveState }) {
  return (
    <>
      {state.possibly_stale && (
        <div className="state-stale">
          <Badge tone="warning">可能过时</Badge>
          <p>
            超过 {state.stale_after_minutes}{" "}
            分钟未收到报告，保留宿主最后报告的活动。
          </p>
        </div>
      )}
      <dl className="state-fields">
        <div className="full-width">
          <dt>当前活动</dt>
          <dd className="state-activity">{state.activity}</dd>
          <dd className="state-field-time">
            活动更新时间：
            <StateTime value={state.activity_updated_at} />
          </dd>
        </div>
        <div>
          <dt>情绪</dt>
          <dd>
            <Mood state={state} />
          </dd>
          <dd className="state-field-time">
            情绪更新时间：
            <StateTime value={state.mood_updated_at} />
          </dd>
        </div>
        <div>
          <dt>活动持续时长</dt>
          <dd>{duration(state.duration_seconds)}</dd>
        </div>
        <div>
          <dt>开始时间</dt>
          <dd>
            <StateTime value={state.started_at} />
          </dd>
        </div>
        <div>
          <dt>开始时间依据</dt>
          <dd>{startBasis[state.start_time_basis]}</dd>
        </div>
        <div className="full-width">
          <dt>最后更新时间</dt>
          <dd>
            <StateTime value={state.updated_at} />
          </dd>
        </div>
        <div>
          <dt>最后报告宿主</dt>
          <dd>{state.host ?? "未知"}</dd>
        </div>
        <div>
          <dt>最后报告入口</dt>
          <dd>{state.entry_id ?? "未知"}</dd>
        </div>
      </dl>
      <h3>细节</h3>
      {Object.keys(state.details).length ? (
        <dl className="state-fields state-detail-fields">
          {Object.entries(state.details).map(([key, detail]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{valueText(detail.value)}</dd>
              <dd className="state-field-time">
                更新时间：
                <StateTime value={detail.updated_at} />
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="quiet">暂无细节报告</p>
      )}
    </>
  );
}

export function StateSummary() {
  const current = useData<CurrentState>("/state", 2000);
  const state = current.data;
  return (
    <div className="state-summary">
      <ReadError error={current.error} retained={state !== null} />
      {current.error && (
        <button className="text-button" onClick={current.refresh}>
          重试当前状态
        </button>
      )}
      {!state && !current.error && <p role="status">正在读取当前状态…</p>}
      {state &&
        (state.activity ? (
          <>
            <p className="state-summary-activity">{state.activity}</p>
            {state.possibly_stale && <Badge tone="warning">可能过时</Badge>}
            <dl className="state-summary-fields">
              <div>
                <dt>情绪</dt>
                <dd>
                  <Mood state={state} />
                </dd>
              </div>
              <div>
                <dt>活动持续时长</dt>
                <dd>{duration(state.duration_seconds)}</dd>
              </div>
            </dl>
            {state.start_time_basis === "first_report" && (
              <p className="muted">持续时长自首次报告起计算。</p>
            )}
          </>
        ) : (
          <p className="quiet">暂无宿主报告的当前活动</p>
        ))}
      <a className="text-button" href="#/state">
        查看状态与报告历史
      </a>
    </div>
  );
}

function ReportCard({ report }: { report: StateReport }) {
  const changes = [
    ...Object.entries(fields).flatMap(([key, label]) => {
      const change = report.changes[key as keyof typeof fields];
      return change ? [{ key, label, ...change }] : [];
    }),
    ...Object.entries(report.changes.details || {}).map(([key, change]) => ({
      key: `details.${key}`,
      label: `细节 · ${key}`,
      ...change,
    })),
  ];
  const show = (key: string, value: StateValue | null) => {
    if (value === null) return "无";
    if (key === "started_at") return <StateTime value={String(value)} />;
    if (key === "start_time_basis")
      return startBasis[value as keyof typeof startBasis] || String(value);
    return valueText(value, true);
  };
  return (
    <article className="state-report" aria-label={`报告 #${report.id}`}>
      <div className="panel-heading">
        <h3>
          {actions[report.action]} · {report.action}
        </h3>
        <span className="muted">
          报告 #{report.id} · {report.method}
        </span>
      </div>
      <p className="state-report-time">
        <StateTime value={report.reported_at} />
      </p>
      <dl className="state-report-source">
        <div>
          <dt>宿主</dt>
          <dd>{report.host ?? "未知"}</dd>
        </div>
        <div>
          <dt>入口</dt>
          <dd>{report.entry_id ?? "未知"}</dd>
        </div>
      </dl>
      {changes.length ? (
        <dl className="state-changes">
          {changes.map((change) => (
            <div key={change.key}>
              <dt>{change.label}</dt>
              <dd>
                <span className="muted">原值</span>
                <span>{show(change.key, change.before)}</span>
              </dd>
              <dd>
                <span className="muted">现值</span>
                <span>{show(change.key, change.after)}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="muted">无字段值变化，本次报告仍已记录。</p>
      )}
    </article>
  );
}

function CurrentStatePage() {
  const current = useData<CurrentState>("/state", 2000);
  const [offset, setOffset] = useState(0);
  const reports = useData<Page<StateReport>>(
    `/state/reports?limit=30&offset=${offset}`,
    offset === 0 ? 2000 : 0,
  );
  return (
    <>
      <div className="state-page">
        <section
          className="panel state-panel"
          aria-labelledby="current-state-title"
        >
          <div className="panel-heading">
            <h2 id="current-state-title">当前状态</h2>
            <button
              className="text-button"
              disabled={current.loading}
              onClick={current.refresh}
            >
              刷新当前状态
            </button>
          </div>
          <p className="state-help">
            活动持续时长与情绪、细节的更新时间分别记录。时间按角色时区显示，保留
            UTC 偏移。
          </p>
          <ReadError error={current.error} retained={current.data !== null} />
          {!current.data && !current.error && (
            <p role="status">正在读取当前状态…</p>
          )}
          {current.data &&
            (current.data.activity ? (
              <StateDetails state={current.data} />
            ) : (
              <Empty title="暂无宿主报告的当前活动">
                宿主开始活动后会显示在这里；结束活动后，报告历史仍会保留。
              </Empty>
            ))}
        </section>
        <section
          className="panel state-panel"
          aria-labelledby="state-reports-title"
        >
          <div className="panel-heading">
            <h2 id="state-reports-title">
              报告历史{reports.data ? ` · ${reports.data.total} 条` : ""}
            </h2>
            <button
              className="text-button"
              disabled={reports.loading}
              onClick={() => (offset === 0 ? reports.refresh() : setOffset(0))}
            >
              刷新报告历史
            </button>
          </div>
          <p className="state-help">
            按报告顺序从新到旧排列，列出字段值的实际变化。心跳也会保留记录。
            {offset === 0
              ? "最新一页自动刷新。"
              : "较早报告暂停自动刷新；刷新历史可回到最新一页。"}
          </p>
          {reports.error && (
            <Notice error>
              报告历史读取失败：{reports.error}。
              {reports.data && "以下为上次读取的报告。"}
            </Notice>
          )}
          {!reports.data && !reports.error && (
            <p role="status">正在读取报告历史…</p>
          )}
          {reports.data?.items.length === 0 && <Empty title="暂无报告历史" />}
          {reports.data?.items.map((report) => (
            <ReportCard key={report.id} report={report} />
          ))}
          {reports.data && (
            <Pagination
              total={reports.data.total}
              offset={offset}
              limit={30}
              change={setOffset}
            />
          )}
        </section>
      </div>
    </>
  );
}

export default function StatePage({
  initialQuery = "",
  openMemory = () => {},
}: {
  initialQuery?: string;
  openMemory?: (id: number) => void;
}) {
  const query = new URLSearchParams(initialQuery);
  const parsedId = Number(query.get("id"));
  const [goalId, setGoalId] = useState<number | null>(
    Number.isSafeInteger(parsedId) && parsedId > 0 ? parsedId : null,
  );
  const initialTab = query.get("tab");
  const [tab, setTab] = useState(
    initialTab === "goals" || initialTab === "notifications"
      ? initialTab
      : "state",
  );
  function openGoal(id: number) {
    setGoalId(id);
    setTab("goals");
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">此刻与接下来的事</p>
          <h1>状态与目标</h1>
          <p>
            当前状态完全由宿主报告，管理员不能编辑。状态与目标由所有入口共享。
          </p>
        </div>
      </div>
      <nav className="learning-tabs" aria-label="状态与目标分区">
        <button aria-pressed={tab === "state"} onClick={() => setTab("state")}>
          当前状态
        </button>
        <button
          aria-pressed={tab === "goals"}
          onClick={() => {
            setGoalId(null);
            setTab("goals");
          }}
        >
          目标与询问
        </button>
        <button
          aria-pressed={tab === "notifications"}
          onClick={() => setTab("notifications")}
        >
          提醒
        </button>
      </nav>
      {tab === "state" ? (
        <CurrentStatePage />
      ) : tab === "goals" ? (
        <Goals key={goalId} initialId={goalId} openMemory={openMemory} />
      ) : (
        <Notifications openGoal={openGoal} />
      )}
    </>
  );
}
