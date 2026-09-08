import { useCallback, useRef, useState } from "react";
import type { Fields, Metric, MetricValue, Operation } from "../api/design";
import { terminal } from "../api/design";
import { ActionDialog, Empty, FieldsForm, QueryState, useEnvironment, useQuery } from "../components/core";
import { OperationPanel } from "../components/Operation";

export function StatsPage() {
  const registry = useQuery<Metric[]>("/stats/metrics");
  const [panel, setPanel] = useState("overview");
  const { bootstrap } = useEnvironment();
  const panels = [...new Set((registry.data ?? []).map((metric) => metric.panel))];
  const [backfill, setBackfill] = useState(false);
  const [operation, setOperation] = useState<Operation>();
  const [epoch, setEpoch] = useState(0);
  const completed = useRef<string | undefined>(undefined);
  const changed = useCallback((value: Operation) => {
    if (terminal(value.status) && completed.current !== value.id) {
      completed.current = value.id;
      setEpoch((previous) => previous + 1);
    }
  }, []);
  return <>
    <h1>统计与观测</h1>
    <p className="muted">统计仅覆盖当前会话可见集合。缺失数据保持未知；部分结果不代表完整总量。</p>
    <QueryState query={registry}>
      <nav className="tabs" aria-label="统计面">
        {panels.map((name) => <button key={name} aria-pressed={panel === name} onClick={() => setPanel(name)}>{name}</button>)}
      </nav>
      {bootstrap.permissions.includes("system.read") && <button onClick={() => setBackfill(true)}>回填统计</button>}
      {backfill && <BackfillDialog onClose={() => setBackfill(false)} onAccepted={(value) => {
        setOperation(value); setBackfill(false);
      }} />}
      {operation && <OperationPanel initial={operation} onChange={changed} />}
      <StatsPanel key={`${panel}:${epoch}`} panel={panel} metrics={registry.data ?? []} />
    </QueryState>
  </>;
}

function StatsPanel({ panel, metrics }: { panel: string; metrics: Metric[] }) {
  const available = metrics.filter((metric) => panel === "timeseries" ? metric.granularity.length > 0 : metric.panel === panel);
  const [metric, setMetric] = useState(available[0]?.metric_id ?? "");
  const selected = metrics.find((item) => item.metric_id === metric);
  const [group, setGroup] = useState("");
  const [granularity, setGranularity] = useState(selected?.granularity[0] ?? "hour");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [filters, setFilters] = useState<Fields>({});
  const [query, setQuery] = useState("");
  const q = useQuery<MetricValue[]>(`/stats/${panel}${query ? "?" + query : ""}`);
  return <>
    <form className="filterbar" onSubmit={(event) => {
      event.preventDefault();
      setQuery(new URLSearchParams({ metric_id: metric,
        ...(panel === "timeseries" ? { granularity } : {}), ...(group ? { group_by: group } : {}),
        ...(from ? { from } : {}), ...(to ? { to } : {}),
        ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== "").map(([key, value]) => [key, String(value)])),
      }).toString());
    }}>
      <label>指标<select value={metric} onChange={(event) => {
        const next = available.find((value) => value.metric_id === event.target.value);
        setMetric(event.target.value); setGroup(""); setFilters({}); setGranularity(next?.granularity[0] ?? "hour");
      }}>{available.map((value) => <option value={value.metric_id} key={value.metric_id}>{value.label}</option>)}</select></label>
      {panel === "timeseries" && <>
        <label>粒度<select value={granularity} onChange={(event) => setGranularity(event.target.value)}>{selected?.granularity.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>从（UTC）<input value={from} onChange={(event) => setFrom(event.target.value)} placeholder="2026-09-08T00:00:00.000000Z" /></label>
        <label>到（UTC）<input value={to} onChange={(event) => setTo(event.target.value)} placeholder="2026-09-09T00:00:00.000000Z" /></label>
      </>}
      <label>分组<select value={group} onChange={(event) => setGroup(event.target.value)}><option value="">不分组</option>{selected?.group_by.map((value) => <option key={value}>{value}</option>)}</select></label>
      <FieldsForm fields={selected?.filters ?? []} value={filters} onChange={setFilters} />
      <button disabled={!metric}>查询</button>
      <button type="button" onClick={q.refresh}>刷新统计</button>
    </form>
    <p>{selected?.description}</p>
    <QueryState query={q}>
      {q.meta?.partial && <p role="status">仅显示已完成的部分统计，不能视为完整总量。</p>}
      {q.meta?.instance_local && <p className="notice">文件与进程数据仅描述此服务实例。</p>}
      <div className="metric-grid">{q.data?.map((value) => <article className="metric" key={value.id}>
        <small>{metrics.find((item) => item.metric_id === value.metric_id)?.label ?? value.metric_id}</small>
        <strong>{value.value === null ? "暂无数据" : String(value.value)}</strong>
        <span>{metrics.find((item) => item.metric_id === value.metric_id)?.unit} {value.approximate ? "· 近似值" : ""}</span>
        <small>{value.bucket} {value.group}</small>
        {value.labels && Object.entries(value.labels).map(([key, detail]) => <small key={key}>{key}: {typeof detail === "object" ? JSON.stringify(detail) : String(detail ?? "暂无数据")}</small>)}
      </article>)}</div>
      {!q.data?.length && <Empty />}
    </QueryState>
  </>;
}

function BackfillDialog({ onClose, onAccepted }: { onClose: () => void; onAccepted: (operation: Operation) => void }) {
  return <ActionDialog path="/stats/rollups:backfill" action={{ id: "backfill", label: "回填统计", method: "POST", permission: "stats.read", reason_codes: ["operator_request"],
    description: "按完整 UTC 小时重算，最多 31 天。现有统计在所有批次完成前继续提供服务。操作需要重新认证。",
    fields: [{ key: "from", label: "开始时间（完整 UTC 小时）", type: "string", required: true }, { key: "to", label: "结束时间（完整 UTC 小时）", type: "string", required: true }] }}
    onClose={onClose} bodyBuilder={(fields, reason) => ({ ...fields, reason_code: reason })}
    onSuccess={(result) => onAccepted(result.data as unknown as Operation)} />;
}
