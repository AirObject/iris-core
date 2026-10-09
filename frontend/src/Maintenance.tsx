import { ConsolidationReportSections } from "./ConsolidationReport";
import { consolidationReason } from "./consolidation-labels";
import { useEffect, useState } from "react";
import { api, errorText, json, useData } from "./api";
import {
  Badge,
  Dialog,
  Empty,
  Notice,
  Pagination,
  fullTime,
  lifecycleLabel,
} from "./ui";
import type {
  MaintenanceItem,
  MaintenanceReport,
  MaintenanceRun,
  MaintenanceSummary,
  Page,
} from "./types";

const triggerNames: Record<string, string> = {
  manual: "手动维护",
  scheduled: "定时维护",
  catchup: "启动补跑",
};
const phaseNames: Record<string, string> = {
  decay: "衰减与状态转换",
  expiry: "到期删除",
  dependency: "依据失效扣减",
  messages: "消息清理",
  retry: "批次重试",
  consolidation: "模型整理",
  persona: "persona 更新",
  goals: "目标依据复核",
};
const countNames: Record<string, string> = {
  decayed: "衰减",
  forgotten: "遗忘",
  restored: "恢复",
  deleted: "删除",
  dependencies_weakened: "依据扣减",
  messages_deleted: "清理消息",
  batches_retried: "重试批次",
  checked: "检查项目",
  skipped: "跳过",
  failed: "失败",
  merged: "合并",
  conflicts: "矛盾建议",
  dependencies_reviewed: "依赖复核建议",
  persona_published: "persona 已发布",
  persona_pending: "persona 待确认",
  goals_reviewed: "目标依据标注变化",
  model_calls: "模型调用",
};
const reasonNames: Record<string, string> = {
  pinned: "已置顶",
  revision_conflict: "修订冲突",
  forgotten: "已遗忘",
  deleted: "已删除",
  no_longer_due: "尚未到期",
  already_applied: "已经扣减",
  dependency_removed: "依据关系已移除",
  no_longer_eligible: "不再符合清理条件",
  referenced: "仍有引用",
  batch_state_changed: "批次状态已变化",
  already_retried: "已自动重试过",
  batch_date_changed: "批次日期已变化",
  target_messages_cleared: "目标消息已清理",
};
function Counts({ summary }: { summary: MaintenanceSummary }) {
  return (
    <dl className="maintenance-counts">
      {Object.entries(summary).map(([key, group]) => (
        <div key={key}>
          <dt>{countNames[key] || key}</dt>
          <dd>{group.count}</dd>
        </div>
      ))}
    </dl>
  );
}
function RunMetadata({ run }: { run: MaintenanceRun }) {
  return (
    <div className="lifecycle-stack">
      <div className="actions">
        <Badge tone={run.state === "completed" ? "green" : "purple"}>
          {run.state === "completed" ? "已完成" : "运行中"}
        </Badge>
        <span>{triggerNames[run.trigger] || run.trigger}</span>
        <span className="muted">#{run.id}</span>
      </div>
      <p>
        开始：{fullTime(run.created_at)} · 完成：
        {run.finished_at ? fullTime(run.finished_at) : "尚未完成"}
      </p>
    </div>
  );
}

export function MaintenancePanel({
  openMemory,
  initialRun,
}: {
  openMemory: (id: number) => void;
  initialRun?: number;
}) {
  const latest = useData<Page<MaintenanceRun>>(
    "/maintenance?limit=1&offset=0",
    5000,
  );
  const [history, setHistory] = useState(false),
    [offset, setOffset] = useState(0);
  const runs = useData<Page<MaintenanceRun>>(
    history ? `/maintenance?limit=10&offset=${offset}` : null,
    5000,
  );
  const [selected, setSelected] = useState<number | null>(initialRun || null),
    [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const run = latest.data?.items[0];
  useEffect(() => {
    const openLinkedReport = () => {
      const hash = location.hash;
      if (hash.split("?")[0] !== "#/status") return;
      const id = Number(
        new URLSearchParams(hash.split("?")[1] || "").get("run"),
      );
      if (Number.isSafeInteger(id) && id > 0) setSelected(id);
    };
    addEventListener("hashchange", openLinkedReport);
    return () => removeEventListener("hashchange", openLinkedReport);
  }, []);
  async function start() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ accepted: boolean; run_id: number }>(
        "/maintenance",
        json("POST", {}),
      );
      setConfirm(false);
      setSelected(result.run_id);
      latest.refresh();
      runs.refresh();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel maintenance-panel">
      <div className="panel-heading">
        <h2>梦境整理与记忆维护</h2>
        <button
          className="secondary"
          disabled={busy}
          onClick={() => setConfirm(true)}
        >
          手动维护
        </button>
      </div>
      {(error || latest.error || runs.error) && (
        <Notice error>
          {error || latest.error || runs.error}
          <button
            className="text-button"
            onClick={() => {
              latest.refresh();
              runs.refresh();
            }}
          >
            刷新维护记录
          </button>
        </Notice>
      )}
      {confirm && (
        <div className="delete-confirm">
          <p>
            手动维护也计一次衰减，并执行遗忘、到期删除、消息清理和批次重试。还会按本次设置调用模型进行整理与
            persona 更新，并复核目标依据。已有未完成的维护时继续该次运行。
          </p>
          <div className="actions">
            <button
              className="primary"
              disabled={busy}
              onClick={() => void start()}
            >
              {busy ? "正在请求…" : "确认运行维护"}
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => setConfirm(false)}
            >
              取消维护
            </button>
          </div>
        </div>
      )}
      {!latest.data && !latest.error && <p role="status">正在读取维护记录…</p>}
      {latest.data && !run && <Empty title="尚无维护记录" />}
      {run && (
        <>
          <h3 className="maintenance-subheading">最近一次维护</h3>
          <RunMetadata run={run} />
          {Object.keys(run.summary).length ? (
            <Counts summary={run.summary} />
          ) : (
            <p className="quiet">维护进行中，可在报告中查看当前数量。</p>
          )}
          <button className="text-button" onClick={() => setSelected(run.id)}>
            查看最近维护报告
          </button>
        </>
      )}
      <p className="fine-print">
        维护不中断接收和学习。报告逐项保留实际变化、模型建议和失败；检查及跳过数量按阶段和原因汇总。
      </p>
      <button
        className="text-button"
        aria-expanded={history}
        onClick={() => setHistory((v) => !v)}
      >
        历史维护
      </button>
      {history && (
        <div className="lifecycle-stack">
          {!runs.data && !runs.error && <p role="status">正在读取历史维护…</p>}
          {runs.data?.items.length === 0 && (
            <p className="quiet">没有历史维护。</p>
          )}
          {runs.data?.items.map((r) => (
            <div className="maintenance-history-row" key={r.id}>
              <span>
                #{r.id} · {fullTime(r.created_at)} ·{" "}
                {triggerNames[r.trigger] || r.trigger} ·{" "}
                {r.state === "completed" ? "已完成" : "运行中"}
              </span>
              <button className="text-button" onClick={() => setSelected(r.id)}>
                查看报告 #{r.id}
              </button>
            </div>
          ))}
          {runs.data && (
            <Pagination
              total={runs.data.total}
              limit={10}
              offset={offset}
              change={setOffset}
            />
          )}
        </div>
      )}
      {selected !== null && (
        <MaintenanceReportDialog
          key={selected}
          id={selected}
          close={() => {
            setSelected(null);
            if (initialRun) location.hash = "#/status";
          }}
          openMemory={(id) => {
            setSelected(null);
            // Consume the report deep link before opening another detail. A
            // later link back to this report then generates a hashchange.
            if (location.hash.startsWith("#/status?"))
              window.history.replaceState(null, "", "#/status");
            openMemory(id);
          }}
        />
      )}
    </section>
  );
}

export function MaintenanceReportDialog({
  id,
  close,
  openMemory,
}: {
  id: number;
  close: () => void;
  openMemory: (id: number) => void;
}) {
  const [poll, setPoll] = useState(3000);
  const report = useData<MaintenanceReport>(`/maintenance/${id}`, poll);
  const data = report.data;
  useEffect(() => {
    if (data?.state === "completed") setPoll(0);
  }, [data?.state]);
  const groups: [string, MaintenanceItem[]][] = data
    ? [
        [
          "变化的记忆",
          data.items.filter(
            (i) =>
              i.memory_id != null &&
              i.outcome !== "failed" &&
              i.phase !== "consolidation",
          ),
        ],
        [
          "清理的消息",
          data.items.filter((i) => i.outcome === "messages_deleted"),
        ],
        [
          "重试的批次",
          data.items.filter((i) => i.outcome === "batches_retried"),
        ],
        ["失败项", data.items.filter((i) => i.outcome === "failed")],
      ]
    : [];
  return (
    <Dialog title={`维护报告 #${id}`} onClose={close}>
      {report.error && (
        <Notice error>
          {report.error}
          <button className="text-button" onClick={report.refresh}>
            重新读取报告
          </button>
        </Notice>
      )}
      {!data && !report.error && <p role="status">正在读取维护报告…</p>}
      {data && (
        <div className="lifecycle-stack">
          <RunMetadata run={data} />
          <p className="muted">
            角色时区：{data.timezone} · 当前阶段：
            {data.state === "completed"
              ? "已结束"
              : Object.values(phaseNames)[data.phase] || "生成报告"}
          </p>
          <Counts summary={data.summary} />
          <details>
            <summary>检查阶段与跳过原因</summary>
            {Object.entries(data.summary.checked?.by_phase || {}).map(
              ([key, count]) => (
                <p key={key}>
                  {phaseNames[key] || key}：{count}
                </p>
              ),
            )}
            {Object.entries(data.summary.skipped?.reasons || {}).map(
              ([key, count]) => (
                <p key={key}>
                  {reasonNames[key] || consolidationReason(key)}：{count}
                </p>
              ),
            )}
          </details>
          <ConsolidationReportSections data={data} openMemory={openMemory} />
          {groups.map(([name, items]) => (
            <ReportGroup
              key={`${id}:${name}`}
              name={name}
              items={items}
              openMemory={openMemory}
            />
          ))}
        </div>
      )}
    </Dialog>
  );
}

function ReportGroup({
  name,
  items,
  openMemory,
}: {
  name: string;
  items: MaintenanceItem[];
  openMemory: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  return (
    <section className="detail-section" aria-label={name}>
      <h3>
        {name} <Badge>{items.length}</Badge>
      </h3>
      {!items.length && <p className="quiet">暂无记录。</p>}
      {items.slice(offset, offset + 30).map((i) => (
        <article className="maintenance-item" key={`${i.phase}:${i.item_key}`}>
          {i.memory_id != null ? (
            <button
              className="text-button"
              onClick={() => openMemory(i.memory_id!)}
            >
              记忆 #{i.memory_id}
            </button>
          ) : (
            <strong>
              {i.phase === "messages"
                ? "消息"
                : i.phase === "persona"
                  ? "persona 任务"
                  : i.phase === "goals"
                    ? "目标"
                    : "批次"}{" "}
              #{i.object_id}
            </strong>
          )}
          <p>
            {phaseNames[i.phase] || i.phase} ·{" "}
            {countNames[i.outcome] || i.outcome}
          </p>
          {typeof i.details.before === "number" && (
            <p>
              保留强度：{i.details.before} → {i.details.after}
            </p>
          )}
          {i.details.transition && (
            <p>状态变为：{lifecycleLabel(i.details.transition)}</p>
          )}
          {i.details.source_memory_id !== undefined && (
            <p>失效依据：记忆 #{i.details.source_memory_id}</p>
          )}
          {i.reason && (
            <p className="danger-text">
              {reasonNames[i.reason] || consolidationReason(i.reason)}
            </p>
          )}
          <small>{fullTime(i.created_at)}</small>
        </article>
      ))}
      <Pagination total={items.length} offset={offset} change={setOffset} />
    </section>
  );
}
