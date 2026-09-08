import { useCallback, useRef, useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/generated";
import { terminal, type Operation } from "../api/design";
import {
  Dialog,
  ErrorNotice,
  QueryState,
  Reason,
  TextData,
  useEnvironment,
  useQuery,
} from "../components/core";
import { OperationPanel } from "../components/Operation";

type Schemas = components["schemas"];
export type ProviderConfig = Schemas["EmbeddingConfigView"];
type Adapter = Schemas["EmbeddingAdapter"];
type Definition = Schemas["EmbeddingDefinition"];
type Rebuild = Schemas["EmbeddingRebuild"];
export const canActivate = (
  config: ProviderConfig | undefined,
  dirty: boolean,
) =>
  !!config &&
  !dirty &&
  config.status === "probed" &&
  !config.activation_blocked_reason &&
  !!config.activation_plan &&
  config.activation_plan.expected_revision === config.revision &&
  config.activation_plan.content_revision === config.content_revision &&
  config.probe?.ok === true &&
  config.probe.normalized === true &&
  config.probe.dimension_observed === config.definition.space.dimension;
const labels: Record<string, string> = {
  endpoint: "服务端点",
  "space.model": "模型名称",
  "space.dimension": "向量维度",
  "limits.batch_size": "每批输入数",
  "limits.timeout_us": "请求超时（微秒）",
  "limits.max_qps": "每秒请求上限",
  "limits.breaker_failures": "触发熔断的连续失败数",
  "limits.breaker_cooldown_us": "熔断恢复间隔（微秒）",
  "limits.max_input_chars": "单条输入字符上限",
};
function initialFields(
  adapter: Adapter,
  config?: ProviderConfig,
): Record<string, string> {
  const definition = config?.definition;
  const values: Record<string, string> = {
    endpoint: definition?.endpoint ?? "",
    "space.model": definition?.space.model ?? "",
    "space.dimension": String(definition?.space.dimension ?? ""),
  };
  for (const [name, value] of Object.entries(
    definition?.limits ?? adapter.default_limits,
  ))
    values[`limits.${name}`] = String(value);
  return values;
}
function definitionFrom(
  adapter: Adapter,
  fields: Record<string, string>,
  label: string,
): Definition {
  const numbers: Record<string, number> = {};
  for (const field of adapter.fields) {
    const value = fields[field.key] ?? "";
    if (field.type === "string") {
      if (value.length < field.minimum || value.length > field.maximum)
        throw new Error(`${labels[field.key]}长度不符合服务器规则`);
    } else {
      const number = Number(value);
      if (
        !value.trim() ||
        !Number.isFinite(number) ||
        number < field.minimum ||
        number > field.maximum ||
        (field.type === "integer" && !Number.isSafeInteger(number))
      )
        throw new Error(`${labels[field.key]}不符合服务器范围`);
      numbers[field.key] = number;
    }
  }
  const requiredNumber = (name: string) => {
    const value = numbers[name];
    if (value === undefined) throw new Error("服务器缺少必需字段规则");
    return value;
  };
  return {
    adapter: adapter.id,
    endpoint: fields.endpoint ?? "",
    label,
    space: {
      ...adapter.space_constants,
      model: fields["space.model"] ?? "",
      dimension: requiredNumber("space.dimension"),
    },
    limits: {
      batch_size: requiredNumber("limits.batch_size"),
      timeout_us: requiredNumber("limits.timeout_us"),
      max_qps: requiredNumber("limits.max_qps"),
      breaker_failures: requiredNumber("limits.breaker_failures"),
      breaker_cooldown_us: requiredNumber("limits.breaker_cooldown_us"),
      max_input_chars: requiredNumber("limits.max_input_chars"),
    },
  };
}
export function ProvidersPage() {
  const [cursor, setCursor] = useState("");
  const overview = useQuery<Schemas["EmbeddingOverview"]>(
    `/providers/embedding?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  const adapters = useQuery<Adapter[]>("/providers/embedding/adapters");
  const [selected, setSelected] = useState<string>();
  const detail = useQuery<ProviderConfig>(
    selected
      ? `/providers/embedding/configs/${encodeURIComponent(selected)}`
      : null,
  );
  const [adapterId, setAdapterId] = useState("");
  const [operation, setOperation] = useState<Operation>();
  const [historyVersion, setHistoryVersion] = useState(0);
  const [editorVersion, setEditorVersion] = useState(0);
  const { bootstrap } = useEnvironment();
  const writable =
    bootstrap.permissions.includes("providers.manage") &&
    !bootstrap.read_only &&
    !bootstrap.maintenance &&
    overview.data?.configured === true;
  const refreshOverview = overview.refresh,
    refreshDetail = detail.refresh;
  const completed = useCallback(
    (op: Operation) => {
      setOperation(op);
      if (terminal(op.status)) {
        refreshOverview();
        refreshDetail();
        setHistoryVersion((v) => v + 1);
      }
    },
    [refreshOverview, refreshDetail],
  );
  const saved = (config: ProviderConfig) => {
    setSelected(config.id);
    refreshDetail();
    refreshOverview();
    setHistoryVersion((v) => v + 1);
  };
  const accepted = (op: Operation) => {
    setOperation(op);
    refreshDetail();
    refreshOverview();
    setHistoryVersion((v) => v + 1);
  };
  const adapter = adapters.data?.find(
    (a) => a.id === (detail.data?.definition.adapter ?? adapterId),
  );
  const working = !!operation && !terminal(operation.status);
  return (
    <>
      <h1>Embedding Provider</h1>
      <p>
        重建完成前继续使用当前服务代；首次启用完成前，向量召回按降级协议服务。
      </p>
      <QueryState query={overview}>
        {!overview.data?.configured && (
          <p className="notice">部署尚未配置 Provider 运行时，当前只读。</p>
        )}
        <section className="panel" aria-label="当前服务配置">
          <p>当前配置：{overview.data?.active_config_id ?? "未启用"}</p>
          <p>当前服务代：{overview.data?.generation_id ?? "尚无可用代"}</p>
          <p>投影状态：{overview.data?.projection_state}</p>
        </section>
        <div className="toolbar">
          {overview.data?.configs.map((c) => (
            <button
              key={c.id}
              disabled={working}
              onClick={() => setSelected(c.id)}
            >
              {c.definition.label || c.definition.space.model} · {c.status} ·{" "}
              {c.id}
            </button>
          ))}
          {overview.meta?.page?.has_more && (
            <button
              onClick={() => setCursor(overview.meta?.page?.next_cursor ?? "")}
            >
              下一页配置
            </button>
          )}
          {cursor && (
            <button onClick={() => setCursor("")}>返回首批配置</button>
          )}
          <button
            disabled={working}
            onClick={() => {
              setSelected(undefined);
              setAdapterId("");
            }}
          >
            新建草稿
          </button>
        </div>
      </QueryState>
      {!selected && (
        <QueryState query={adapters}>
          <label>
            适配器
            <select
              value={adapterId}
              disabled={!writable}
              onChange={(e) => setAdapterId(e.target.value)}
            >
              <option value="">选择适配器</option>
              {adapters.data?.map((a) => (
                <option key={a.id} value={a.id} disabled={!a.available}>
                  {a.label}
                  {a.available ? "" : "（部署未启用）"}
                </option>
              ))}
            </select>
          </label>
        </QueryState>
      )}
      {selected && (
        <QueryState query={detail}>
          <button
            disabled={working}
            onClick={() => {
              setEditorVersion((v) => v + 1);
              refreshDetail();
            }}
          >
            放弃本地修改并读取最新配置
          </button>
        </QueryState>
      )}
      {adapter && (!selected || detail.data) && (
        <ProviderEditor
          key={
            detail.data
              ? `${detail.data.id}:${detail.data.revision}:${editorVersion}`
              : `new:${adapter.id}`
          }
          adapter={adapter}
          config={detail.data}
          writable={writable && !working}
          onSaved={saved}
          onAccepted={accepted}
        />
      )}
      {operation && <OperationPanel initial={operation} onChange={completed} />}
      {selected && (
        <RevisionHistory key={`${selected}:${historyVersion}`} id={selected} />
      )}
      <RebuildHistory
        key={historyVersion}
        writable={writable}
        onAccepted={accepted}
      />
      <p>认知 Provider 配置由部署管理，此处不提供修改。</p>
    </>
  );
}
function ProviderEditor({
  adapter,
  config,
  writable,
  onSaved,
  onAccepted,
}: {
  adapter: Adapter;
  config?: ProviderConfig;
  writable: boolean;
  onSaved: (config: ProviderConfig) => void;
  onAccepted: (op: Operation) => void;
}) {
  const [fields, setFields] = useState(() => initialFields(adapter, config));
  const [label, setLabel] = useState(config?.definition.label ?? "");
  const [secretMode, setSecretMode] = useState<"secret_ref" | "sealed">(
    "secret_ref",
  );
  const [secret, setSecret] = useState("");
  const [dirty, setDirty] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [confirm, setConfirm] = useState<"activate" | "rollback">();
  const key = useRef(crypto.randomUUID()),
    lock = useRef(false);
  const editable =
    writable &&
    !busy &&
    (!config || ["draft", "probed"].includes(config.status));
  const changed = () => {
    setDirty(true);
    setConfirm(undefined);
    key.current = crypto.randomUUID();
  };
  const run = async (
    action: "save" | "test" | "discard" | "activate" | "rollback",
  ) => {
    if (lock.current || !writable || !reason) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    try {
      if (action === "save") {
        const definition = definitionFrom(adapter, fields, label);
        const request = api.action<ProviderConfig>(
          config
            ? `/providers/embedding/configs/${config.id}`
            : "/providers/embedding/configs",
          {
            method: config ? "PATCH" : "POST",
            key: key.current,
            userActivity: true,
            body: {
              definition,
              ...(config ? { expected_revision: config.revision } : {}),
              ...(secret
                ? { secret: { mode: secretMode, value: secret } }
                : {}),
              reason_code: reason,
            },
          },
        );
        setSecret("");
        onSaved((await request).data);
      } else if (config && (action === "test" || action === "discard")) {
        const response = await api.action<
          Schemas["ConsoleOperation"] | ProviderConfig
        >(`/providers/embedding/configs/${config.id}:${action}`, {
          method: "POST",
          key: key.current,
          userActivity: true,
          body: { expected_revision: config.revision, reason_code: reason },
        });
        if (action === "test")
          onAccepted(response.data as Schemas["ConsoleOperation"]);
        else onSaved(response.data as ProviderConfig);
      } else if (
        config &&
        confirm === action &&
        config.activation_plan &&
        !dirty
      ) {
        const response = await api.action<Rebuild>(
          action === "rollback"
            ? "/providers/embedding:rollback"
            : `/providers/embedding/configs/${config.id}:activate`,
          {
            method: "POST",
            key: key.current,
            userActivity: true,
            body: {
              expected_revision: config.revision,
              reason_code: reason,
              ...(action === "rollback"
                ? { target_config_id: config.id }
                : { rebuild_ack: config.activation_plan.rebuild_plan_hash }),
            },
          },
        );
        onAccepted(response.data.operation);
        setConfirm(undefined);
      }
      key.current = crypto.randomUUID();
    } catch (e) {
      setError(e);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <section className="panel" aria-label="Provider 配置编辑">
      {config && (
        <>
          <h2>
            {config.definition.label || config.definition.space.model} ·{" "}
            {config.status}
          </h2>
          <p>
            配置修订 {config.revision} · 内容修订 {config.content_revision} ·
            历史代 {config.last_generation_id ?? "无"}
          </p>
          <p>
            凭据模式 {config.secret_mode ?? "无需凭据"} · 提示{" "}
            {config.secret_hint || "无"} · 摘要{" "}
            {config.secret_digest_prefix || "无"} ·{" "}
            {config.resolved ? "可解析" : "不可解析"}
          </p>
        </>
      )}
      <label>
        配置名称
        <input
          value={label}
          maxLength={128}
          disabled={!editable}
          onChange={(e) => {
            setLabel(e.target.value);
            changed();
          }}
        />
      </label>
      {adapter.fields.map((field) => (
        <label key={field.key}>
          {labels[field.key] ?? field.key}
          <input
            value={fields[field.key] ?? ""}
            type={field.type === "string" ? "text" : "number"}
            required={field.required}
            min={field.minimum}
            max={field.maximum}
            step={field.type === "number" ? "any" : 1}
            disabled={!editable}
            onChange={(e) => {
              setFields((v) => ({ ...v, [field.key]: e.target.value }));
              changed();
            }}
          />
          <small>
            范围 {field.minimum}–{field.maximum}
          </small>
        </label>
      ))}
      <p>
        距离 {adapter.space_constants.metric} · 归一化{" "}
        {adapter.space_constants.normalization} · 模板版本{" "}
        {adapter.space_constants.template_version} · 构建版本{" "}
        {adapter.space_constants.builder_version}
      </p>
      {adapter.secret_modes.length > 0 && (
        <>
          <label>
            密钥模式
            <select
              value={secretMode}
              disabled={!editable}
              onChange={(e) => {
                setSecretMode(e.target.value as "secret_ref" | "sealed");
                setSecret("");
                changed();
              }}
            >
              {adapter.secret_modes.map((mode) => (
                <option key={mode} value={mode}>
                  {mode === "secret_ref"
                    ? "部署引用"
                    : "加密封装（需要部署主密钥）"}
                </option>
              ))}
            </select>
          </label>
          <label>
            {secretMode === "secret_ref"
              ? "秘密引用（env:NAME 或 file:/绝对路径）"
              : "一次性提交密钥"}
            <input
              type={secretMode === "sealed" ? "password" : "text"}
              autoComplete="off"
              value={secret}
              disabled={!editable}
              onChange={(e) => {
                setSecret(e.target.value);
                changed();
              }}
            />
          </label>
          {config && <p>留空保留已有凭据；服务器不会返回引用或密钥原文。</p>}
        </>
      )}
      <Reason
        codes={["operator_request"]}
        value={reason}
        onChange={(v) => {
          setReason(v);
          key.current = crypto.randomUUID();
        }}
      />
      <ErrorNotice error={error} />
      <div className="toolbar">
        <button
          disabled={!editable || !reason}
          onClick={() => void run("save")}
        >
          保存草稿
        </button>
        <button
          disabled={
            !writable ||
            busy ||
            !reason ||
            dirty ||
            !config ||
            !["draft", "probed", "retired"].includes(config.status)
          }
          onClick={() => void run("test")}
        >
          服务端探测
        </button>
        <button
          disabled={!editable || !config || !reason || dirty}
          onClick={() => void run("discard")}
        >
          丢弃未激活草稿
        </button>
      </div>
      {config && (
        <>
          <h3>探测结果</h3>
          <TextData value={config.probe ?? null} />
          <p>
            激活门禁：
            {config.activation_blocked_reason ??
              (config.activation_plan ? "服务器计划可用" : "当前状态不可激活")}
          </p>
          <button
            disabled={
              !writable || busy || !reason || !canActivate(config, dirty)
            }
            onClick={() => setConfirm("activate")}
          >
            查看激活计划
          </button>
          <button
            disabled={
              !writable ||
              busy ||
              !reason ||
              dirty ||
              config.status !== "retired" ||
              !config.activation_plan
            }
            onClick={() => setConfirm("rollback")}
          >
            查看历史回滚计划
          </button>
        </>
      )}
      {confirm && config?.activation_plan && (
        <Dialog
          title={confirm === "activate" ? "确认激活配置" : "确认历史回滚"}
          onClose={() => {
            if (!busy) setConfirm(undefined);
          }}
        >
          <Effects value={config.activation_plan.side_effects} />
          <p>
            目标模型：{config.definition.space.model} ·{" "}
            {config.definition.space.dimension} 维
          </p>
          <p>
            复用历史代：{config.activation_plan.reuse_generation_id ?? "不复用"}
          </p>
          <ErrorNotice error={error} />
          <button
            className="primary"
            disabled={busy || !writable || dirty}
            onClick={() => void run(confirm)}
          >
            确认并{confirm === "activate" ? "激活" : "回滚"}
          </button>
        </Dialog>
      )}
    </section>
  );
}
function Effects({ value }: { value: Schemas["EmbeddingSideEffects"] }) {
  return (
    <>
      <p>{value.rebuild ? "需要重建向量索引" : "无需重建向量索引"}</p>
      <p>影响资源估算：{value.estimated_resources}</p>
      <p>{value.worker_required ? "需要 Worker 在线执行" : "无需 Worker"}</p>
      <p>
        请求耗时估算：{value.estimated_duration_seconds.lower}–
        {value.estimated_duration_seconds.upper} 秒（不含队列等待与索引读写）
      </p>
    </>
  );
}
function RevisionHistory({ id }: { id: string }) {
  const [cursor, setCursor] = useState("");
  const query = useQuery<Schemas["EmbeddingRevisionView"][]>(
    `/providers/embedding/configs/${id}/revisions?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  return (
    <section className="panel">
      <h2>不可变修订历史</h2>
      <QueryState query={query}>
        <TextData value={query.data} />
        {query.meta?.page?.has_more && (
          <button
            onClick={() => setCursor(query.meta?.page?.next_cursor ?? "")}
          >
            下一页修订
          </button>
        )}
      </QueryState>
    </section>
  );
}
function RebuildHistory({
  writable,
  onAccepted,
}: {
  writable: boolean;
  onAccepted: (op: Operation) => void;
}) {
  const [cursor, setCursor] = useState("");
  const query = useQuery<Rebuild[]>(
    `/providers/embedding/rebuilds?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const key = useRef(crypto.randomUUID()),
    lock = useRef(false);
  const cancel = async (id: string) => {
    if (lock.current || !writable || !reason) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.action<Rebuild>(
        `/providers/embedding/rebuilds/${id}:cancel`,
        {
          method: "POST",
          key: key.current,
          userActivity: true,
          body: { reason_code: reason },
        },
      );
      key.current = crypto.randomUUID();
      onAccepted(result.data.operation);
      query.refresh();
    } catch (e) {
      setError(e);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <section className="panel">
      <h2>重建历史与进度</h2>
      <button onClick={query.refresh}>刷新重建状态</button>
      <QueryState query={query}>
        {query.data?.map((row) => (
          <article key={row.operation.id}>
            <h3>
              {row.action} · {row.operation.status}
            </h3>
            <p>
              操作 {row.operation.id} · 配置 {row.config_id}
            </p>
            <p>
              处理进度 {row.operation.progress?.processed ?? "0"} /{" "}
              {row.operation.progress?.total ?? "未知"}{" "}
              {row.operation.progress?.unit}
            </p>
            <p>
              结果代 {row.generation_id ?? "尚未发布"} · 当前服务代{" "}
              {row.serving_generation_id ?? "无"}
            </p>
            <p>
              预计剩余{" "}
              {row.estimated_remaining_seconds === null
                ? "未知"
                : `${row.estimated_remaining_seconds} 秒`}
            </p>
            <Effects value={row.side_effects} />
            {row.operation.blocked_reason && (
              <p>{row.operation.blocked_reason}</p>
            )}
            {row.operation.cancellable && (
              <button
                disabled={!writable || busy || !reason}
                onClick={() => void cancel(row.operation.id)}
              >
                取消重建 {row.operation.id}
              </button>
            )}
          </article>
        ))}
        {query.meta?.page?.has_more && (
          <button
            onClick={() => setCursor(query.meta?.page?.next_cursor ?? "")}
          >
            下一页重建
          </button>
        )}
      </QueryState>
      <Reason
        codes={["operator_request"]}
        value={reason}
        onChange={(value) => {
          setReason(value);
          key.current = crypto.randomUUID();
        }}
      />
      <ErrorNotice error={error} />
    </section>
  );
}
