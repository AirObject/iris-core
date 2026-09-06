import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import {
  cas,
  type Accepted,
  type Action,
  type Fields,
  type ImportFormat,
  type ImportReport,
  type ImportView,
  type Metric,
  type MetricValue,
  type Operation,
  type Preview,
  type Resource,
  type ResourceType,
} from "../api/design";
import {
  ActionButton,
  ActionDialog,
  Empty,
  ErrorNotice,
  FieldsForm,
  QueryState,
  Reason,
  TextData,
  encodeFields,
  useEnvironment,
  useQuery,
} from "../components/core";
import { OperationPanel, Problems } from "../components/Operation";

export function StatsPage() {
  const registry = useQuery<Metric[]>("/stats/metrics");
  const [panel, setPanel] = useState("overview");
  return (
    <>
      <h1>统计与观测</h1>
      <p className="muted">
        指标及分组仅覆盖当前会话可见集合；缺失值保留为未知。
      </p>
      <nav className="tabs">
        {[
          "overview",
          "timeseries",
          "pipeline",
          "projections",
          "recall",
          "providers",
          "storage",
          "security",
        ].map((p) => (
          <button
            key={p}
            aria-pressed={panel === p}
            onClick={() => setPanel(p)}
          >
            {p}
          </button>
        ))}
      </nav>
      <QueryState query={registry}>
        <StatsPanel key={panel} panel={panel} metrics={registry.data ?? []} />
      </QueryState>
    </>
  );
}
function StatsPanel({ panel, metrics }: { panel: string; metrics: Metric[] }) {
  const [metric, setMetric] = useState(
    metrics.find((m) => m.panel === panel)?.metric_id ??
      metrics[0]?.metric_id ??
      "",
  );
  const selected = metrics.find((m) => m.metric_id === metric);
  const [group, setGroup] = useState("");
  const [granularity, setGranularity] = useState(
    selected?.granularity[0] ?? "",
  );
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [filters, setFilters] = useState<Fields>({});
  const [query, setQuery] = useState("");
  const q = useQuery<MetricValue[]>(
    `/stats/${panel}${query ? "?" + query : ""}`,
  );
  return (
    <>
      <form
        className="filterbar"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(
            new URLSearchParams({
              metric_id: metric,
              ...(granularity ? { granularity } : {}),
              ...(group ? { group_by: group } : {}),
              ...(from ? { from } : {}),
              ...(to ? { to } : {}),
              ...Object.fromEntries(
                Object.entries(filters).map(([k, v]) => [k, String(v)]),
              ),
            }).toString(),
          );
        }}
      >
        <label>
          指标
          <select
            value={metric}
            onChange={(e) => {
              setMetric(e.target.value);
              setGroup("");
              setFilters({});
            }}
          >
            {metrics.map((m) => (
              <option value={m.metric_id} key={m.metric_id}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          粒度
          <select
            value={granularity}
            onChange={(e) => setGranularity(e.target.value)}
          >
            {selected?.granularity.map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </label>
        <label>
          分组
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">不分组</option>
            {selected?.group_by.map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </label>
        <label>
          从（原始 UTC 时间）
          <input value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label>
          到<input value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
        <FieldsForm
          fields={selected?.filters ?? []}
          value={filters}
          onChange={setFilters}
        />
        <button>查询</button>
      </form>
      <p>{selected?.description}</p>
      <QueryState query={q}>
        <div className="metric-grid">
          {q.data?.map((v) => (
            <article className="metric" key={v.id}>
              <small>
                {metrics.find((m) => m.metric_id === v.metric_id)?.label ??
                  v.metric_id}
              </small>
              <strong>{v.value === null ? "暂无数据" : String(v.value)}</strong>
              <span>
                {metrics.find((m) => m.metric_id === v.metric_id)?.unit}{" "}
                {v.approximate ? "· 近似值" : ""}
              </span>
              <small>
                {v.bucket} {v.group}
              </small>
            </article>
          ))}
        </div>
        {!q.data?.length && <Empty />}
      </QueryState>
    </>
  );
}
interface ExportView extends Resource {
  operation?: Operation;
  downloadable: boolean;
  format: string;
  rows: string;
  bytes: string;
  sha256: string;
  expires_at: string;
}
export function ExportsPage() {
  const q = useQuery<ExportView[]>("/exports");
  const types = useQuery<ResourceType[]>("/memory/resource-types");
  const [creating, setCreating] = useState(false);
  const [lifecycle, setLifecycle] = useState<{
    item: ExportView;
    action: Action;
  }>();
  const [op, setOp] = useState<Operation>();
  const [error, setError] = useState<unknown>();
  const action: Action = {
    id: "create",
    label: "创建导出",
    permission: "exports.write",
    fields: [
      {
        key: "format",
        label: "格式",
        type: "enum",
        options: ["imc-data/v1", "csv"],
        required: true,
      },
      {
        key: "resource_types",
        label: "资源类型（可多选）",
        type: "multi",
        options: types.data
          ?.filter((t) => !t.read_only)
          .map((t) => t.resource_type),
        required: true,
        description: types.data?.map((t) => t.collection).join(", "),
      },
      { key: "scope", label: "授权范围", type: "json" },
      { key: "from", label: "开始 UTC 时间", type: "string" },
      { key: "to", label: "结束 UTC 时间", type: "string" },
      { key: "include_sources", label: "包含授权来源闭包", type: "boolean" },
    ],
    reason_codes:
      types.data
        ?.flatMap(
          (t) => t.actions.find((a) => a.id === "forget")?.reason_codes ?? [],
        )
        .filter((v, i, a) => a.indexOf(v) === i) ?? [],
    high_risk: true,
  };
  return (
    <>
      <h1>数据导出</h1>
      <p className="notice">
        JSONL 用于经过重映射和审核的再导入；CSV
        是人工报表。导出不包含完整历史、内部执行状态与投影，不是无损备份。
      </p>
      <ActionButton action={action} onClick={() => setCreating(true)} />
      <ErrorNotice error={error} />
      <QueryState query={q}>
        {q.data?.map((item) => (
          <section className="panel" key={item.id}>
            <h2>
              {item.id} · {item.status}
            </h2>
            <p>
              {item.format} · {item.rows} 行 · {item.bytes} 字节
            </p>
            <p>SHA-256 {item.sha256}</p>
            <p>
              有效至 {item.expires_at} · 下载会重新校验权限与 Tombstone 水位
            </p>
            <button
              disabled={
                !item.downloadable || Date.parse(item.expires_at) <= Date.now()
              }
              onClick={() =>
                void api
                  .download(
                    `/exports/${item.id}/download`,
                    `iris-${item.id}.${item.format === "csv" ? "csv" : "jsonl"}`,
                  )
                  .catch(setError)
              }
            >
              凭当前会话下载
            </button>
            {["cancel", "delete"].map((id) => (
              <ActionButton
                key={id}
                action={{
                  id,
                  label: id === "cancel" ? "取消导出任务" : "删除暂存产物",
                  permission: "exports.write",
                  fields: [],
                  reason_codes: action.reason_codes,
                  high_risk: true,
                }}
                resource={item}
                onClick={() =>
                  setLifecycle({
                    item,
                    action: {
                      id,
                      label: id === "cancel" ? "取消导出任务" : "删除暂存产物",
                      permission: "exports.write",
                      fields: [],
                      reason_codes: action.reason_codes,
                      high_risk: true,
                    },
                  })
                }
              />
            ))}
            {item.operation && <OperationPanel initial={item.operation} />}
          </section>
        ))}
        {!q.data?.length && <Empty />}
      </QueryState>
      {lifecycle && (
        <ActionDialog
          action={lifecycle.action}
          resource={lifecycle.item}
          path={`/exports/${lifecycle.item.id}:${lifecycle.action.id}`}
          bodyBuilder={(_, reason) => ({ reason_code: reason })}
          onClose={() => setLifecycle(undefined)}
          onSuccess={(r) => {
            setOp(r.data.operation);
            q.refresh();
          }}
        />
      )}
      {creating && (
        <ActionDialog
          action={action}
          path="/exports"
          bodyBuilder={(fields, reason) => ({ ...fields, reason_code: reason })}
          onClose={() => setCreating(false)}
          onSuccess={(r) => {
            setOp(r.data.operation);
            q.refresh();
          }}
        />
      )}
      {op && <OperationPanel initial={op} />}
    </>
  );
}
export const reportUsable = (
  report: ImportReport | undefined,
  dirty: boolean,
) =>
  !!report &&
  !dirty &&
  report.can_commit &&
  Date.parse(report.expires_at) > Date.now();
export function ImportsPage() {
  const formats = useQuery<ImportFormat[]>("/imports/formats");
  const [formatId, setFormatId] = useState("");
  const [resumeId, setResumeId] = useState("");
  const format = formats.data?.find((f) => f.id === formatId);
  const [step, setStep] = useState(0);
  const [item, setItem] = useState<ImportView>();
  const [file, setFile] = useState<File>();
  const [mapping, setMapping] = useState<Fields>({});
  const [dirty, setDirty] = useState(false);
  const [report, setReport] = useState<ImportReport>();
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>();
  const [message, setMessage] = useState("");
  const [progress, setProgress] = useState("0");
  const [busy, setBusy] = useState(false);
  const [op, setOp] = useState<Operation>();
  const [compensation, setCompensation] = useState<Preview>();
  const key = useRef(crypto.randomUUID());
  const lock = useRef(false);
  const { bootstrap } = useEnvironment();
  const writable =
    !bootstrap.read_only &&
    !bootstrap.maintenance &&
    bootstrap.permissions.includes("imports.write");
  const run = async (action: string) => {
    if (lock.current || !writable) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    setMessage("");
    try {
      if (action === "load") {
        const r = await api.request<ImportView>(
          `/imports/${encodeURIComponent(resumeId)}`,
          { userActivity: true },
        );
        setItem(r.data);
        setFormatId(r.data.format_id);
        setMapping(r.data.mapping);
        setReport(undefined);
        setDirty(false);
        setStep(r.data.status === "awaiting_upload" ? 1 : 3);
      } else if (action === "create" && format && file) {
        if (BigInt(file.size) > BigInt(format.limits.file_bytes))
          throw new Error("文件超过服务端声明的限额");
        if (
          !format.extensions.some((ext) =>
            file.name.toLowerCase().endsWith(ext),
          )
        )
          throw new Error("只接受声明的数据文件格式");
        const r = await api.action<ImportView>("/imports", {
          method: "POST",
          key: key.current,
          body: { format_id: format.id, filename: file.name, mapping: {} },
          userActivity: true,
        });
        setItem(r.data);
        setStep(1);
      } else if (action === "upload" && item && file && format) {
        const r = await api.action<ImportView>(`/imports/${item.id}/file`, {
          method: "PUT",
          raw: file,
          contentType: format.media_types.includes(file.type)
            ? file.type
            : format.media_types[0],
          key: key.current,
          progress: (loaded, total) => setProgress(`${loaded} / ${total} 字节`),
          userActivity: true,
        });
        setItem(r.data);
        setStep(2);
      } else if (action === "mapping" && item && format) {
        const r = await api.action<ImportView>(`/imports/${item.id}/mapping`, {
          method: "PATCH",
          key: key.current,
          body: {
            mapping: encodeFields(format.mapping_fields, mapping),
            ...cas(item),
            reason_code: reason,
          },
          userActivity: true,
        });
        setItem(r.data);
        setDirty(false);
        setReport(undefined);
        setStep(3);
      } else if (action === "validate" && item) {
        const r = await api.action<Accepted>(`/imports/${item.id}:validate`, {
          method: "POST",
          key: key.current,
          body: { ...cas(item) },
          userActivity: true,
        });
        setOp(r.data.operation);
        setStep(4);
      } else if (action === "report" && item) {
        const [view, r] = await Promise.all([
          api.request<ImportView>(`/imports/${item.id}`),
          api.request<ImportReport>(`/imports/${item.id}/report`),
        ]);
        setItem(view.data);
        setReport(r.data);
        setStep(4);
      } else if (action === "commit" && item && reportUsable(report, dirty)) {
        const r = await api.action<Accepted>(`/imports/${item.id}:commit`, {
          method: "POST",
          key: key.current,
          body: {
            report_id: report!.report_id,
            report_hash: report!.report_hash,
            ...cas(item),
            reason_code: reason,
          },
          userActivity: true,
        });
        setOp(r.data.operation);
        setStep(6);
      } else if (action === "compensate-preview" && item) {
        const r = await api.action<Preview>(
          `/imports/${item.id}:compensate-preview`,
          {
            method: "POST",
            key: key.current,
            body: { ...cas(item), reason_code: reason },
            userActivity: true,
          },
        );
        setCompensation(r.data);
      } else if (action === "compensate" && item && compensation) {
        const r = await api.action<Accepted>(`/imports/${item.id}:compensate`, {
          method: "POST",
          key: key.current,
          body: {
            preview_id: compensation.preview_id,
            preview_hash: compensation.preview_hash,
            reason_code: reason,
          },
          userActivity: true,
        });
        setOp(r.data.operation);
        setCompensation(undefined);
      } else if (item) {
        const r = await api.action<Accepted>(`/imports/${item.id}:${action}`, {
          method: "POST",
          key: key.current,
          body:
            action === "resume"
              ? {
                  report_hash: report?.report_hash,
                  checkpoint_version: item.checkpoint_version,
                  reason_code: reason,
                }
              : { ...cas(item), reason_code: reason },
          userActivity: true,
        });
        setOp(r.data.operation);
      }
      key.current = crypto.randomUUID();
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e);
        if (
          [
            "report_stale",
            "report_expired",
            "config_changed",
            "preview_stale",
          ].includes(e.kind)
        ) {
          setReport(undefined);
          setCompensation(undefined);
        }
      } else setMessage(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
      lock.current = false;
    }
  };
  return (
    <>
      <h1>手动数据导入</h1>
      {!item && (
        <div className="filterbar">
          <label>
            已有 Import ID
            <input
              value={resumeId}
              onChange={(e) => setResumeId(e.target.value)}
            />
          </label>
          <button disabled={!resumeId || busy} onClick={() => void run("load")}>
            读取已有导入以续跑
          </button>
        </div>
      )}
      <p className="notice">
        目标租户来自当前会话。只接受声明的数据格式；不会连接数据库、远程 URL
        或服务器路径。历史内容进入审核；Persona 为待审 Proposal，Task
        触发器禁用。
      </p>
      <ol className="steps">
        {["格式", "上传", "映射", "验证", "报告", "逐条审核", "提交与结果"].map(
          (label, i) => (
            <li className={step === i ? "current" : ""} key={label}>
              {i + 1}. {label}
            </li>
          ),
        )}
      </ol>
      <QueryState query={formats}>
        <label>
          数据格式
          <select
            disabled={!!item}
            value={formatId}
            onChange={(e) => {
              setFormatId(e.target.value);
              setReport(undefined);
              key.current = crypto.randomUUID();
            }}
          >
            <option value="">选择服务器已启用格式</option>
            {formats.data?.map((f) => (
              <option key={f.id} value={f.id}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
        {format && (
          <>
            <p>
              {format.description} · 每文件 {format.limits.file_bytes} 字节 /{" "}
              {format.limits.records} 条 / 每条 {format.limits.record_bytes}{" "}
              字节 / 深度 {format.limits.json_depth}
            </p>
            <label>
              本地数据文件
              <input
                type="file"
                disabled={!!item && step !== 1}
                accept={format.extensions.join(",")}
                onChange={(e) => {
                  setFile(e.target.files?.[0]);
                  key.current = crypto.randomUUID();
                }}
              />
            </label>
          </>
        )}
      </QueryState>
      <ErrorNotice error={error} />
      {message && <p role="alert">{message}</p>}
      {!item ? (
        <button
          disabled={!format || !file || busy || !writable}
          onClick={() => void run("create")}
        >
          建立导入
        </button>
      ) : (
        <>
          <p>
            Import {item.id} · {item.status} · Revision {item.revision}
          </p>
          {step === 1 && (
            <>
              <p role="status">上传进度 {progress}</p>
              <button
                disabled={busy || !writable}
                onClick={() => void run("upload")}
              >
                上传完整字节流
              </button>
            </>
          )}
          {step >= 2 && format && (
            <>
              <h2>目标映射</h2>
              <FieldsForm
                fields={format.mapping_fields}
                value={mapping}
                disabled={busy}
                onChange={(v) => {
                  setMapping(v);
                  setDirty(true);
                  setReport(undefined);
                  key.current = crypto.randomUUID();
                }}
              />
              <Reason
                codes={format.reason_codes}
                value={reason}
                onChange={(v) => {
                  setReason(v);
                  key.current = crypto.randomUUID();
                }}
              />
              <button
                disabled={busy || !reason || !writable}
                onClick={() => void run("mapping")}
              >
                保存映射（使旧报告失效）
              </button>
              <button
                disabled={busy || dirty || !writable}
                onClick={() => void run("validate")}
              >
                验证数据
              </button>
              <button disabled={busy} onClick={() => void run("report")}>
                读取最新报告
              </button>
            </>
          )}
          {report && (
            <section className="panel">
              <h2>验证报告</h2>
              <p>
                {report.report_id} · {report.report_hash} · 有效至{" "}
                {report.expires_at}
              </p>
              <TextData value={report.counts} />
              <TextData value={report.warnings} />
              <TextData value={report.decisions} />
              <button onClick={() => setStep(5)}>逐条审核候选</button>
              <button
                disabled={
                  busy || !reason || !reportUsable(report, dirty) || !writable
                }
                onClick={() => void run("commit")}
              >
                确认此报告并提交
              </button>
            </section>
          )}
          {step === 5 && (
            <ImportRecords
              id={item.id}
              onReviewed={() => {
                setReport(undefined);
                setStep(3);
              }}
            />
          )}
          {step >= 4 && <Problems path={`/imports/${item.id}/problems`} />}
          <div className="toolbar">
            {["cancel", "resume", "compensate-preview"].map((a) => (
              <button
                key={a}
                disabled={
                  busy ||
                  !writable ||
                  !reason ||
                  !item.available_actions.includes(a) ||
                  (a === "resume" && (!report || dirty))
                }
                onClick={() => void run(a)}
              >
                {a === "cancel"
                  ? "取消后续批次"
                  : a === "resume"
                    ? "重新授权后续跑"
                    : "预览已提交内容的补偿"}
              </button>
            ))}
          </div>
          {compensation && (
            <section className="panel">
              <TextData value={compensation.targets} />
              <TextData value={compensation.impacts} />
              <p>
                补偿通过 Forget 门禁，仅处理未被后续修改、无新依赖且未受 Hold
                的新建对象。
              </p>
              <button
                disabled={
                  busy ||
                  !compensation.can_commit ||
                  Date.parse(compensation.expires_at) <= Date.now()
                }
                onClick={() => void run("compensate")}
              >
                确认该补偿预览
              </button>
            </section>
          )}
        </>
      )}
      {op && <OperationPanel initial={op} />}
      <p className="muted">
        取消仅停止后续批次，已提交内容保留。备份不可用时提交会 blocked；不存在
        Web 整文件回滚。
      </p>
    </>
  );
}
function ImportRecords({
  id,
  onReviewed,
}: {
  id: string;
  onReviewed: () => void;
}) {
  const [cursor, setCursor] = useState("");
  const q = useQuery<Resource[]>(
    `/imports/${id}/records?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  const [selected, setSelected] = useState<{
    resource: Resource;
    action: Action;
  }>();
  return (
    <QueryState query={q}>
      {q.data?.map((r) => (
        <section className="panel" key={r.id}>
          <TextData value={r.fields} />
          {q.meta?.descriptor?.actions.map((a) => (
            <ActionButton
              key={a.id}
              action={a}
              resource={r}
              onClick={() => setSelected({ resource: r, action: a })}
            />
          ))}
        </section>
      ))}
      {q.meta?.page?.has_more && (
        <button onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}>
          下一页审核记录
        </button>
      )}
      {selected && (
        <ActionDialog
          action={selected.action}
          resource={selected.resource}
          path={`/imports/${id}/records/${selected.resource.id}:review`}
          bodyBuilder={(fields, reason) => ({
            ...cas(selected.resource),
            ...fields,
            reason_code: reason,
          })}
          onClose={() => setSelected(undefined)}
          onSuccess={onReviewed}
        />
      )}
    </QueryState>
  );
}
