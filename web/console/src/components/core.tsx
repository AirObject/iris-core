import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError, CONTRACT_VERSION, metadataText } from "../api/client";
import {
  cas,
  uniqueById,
  type Action,
  type Accepted,
  type Bootstrap,
  type Field,
  type Fields,
  type Meta,
  type Resource,
  type Result,
  type Value,
} from "../api/design";
export const Environment = createContext<{
  bootstrap: Bootstrap;
  epoch: number;
}>({
  bootstrap: {
    contract_version: CONTRACT_VERSION,
    permissions: [],
    modules: [],
    read_only: true,
    maintenance: false,
    upload_limits: {
      file_bytes: "0",
      records: "0",
      record_bytes: "0",
      json_depth: 0,
    },
    display_timezone: "UTC",
    pending_restart: false,
    import_in_progress: false,
  },
  epoch: 0,
});
export const useEnvironment = () => useContext(Environment);
export function useQuery<T>(path: string | null) {
  const { epoch } = useEnvironment();
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<{
    data?: T;
    meta?: Meta;
    error?: unknown;
    loading: boolean;
  }>({ loading: true });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: !!path });
    if (path)
      void api
        .request<T>(path, { signal: controller.signal })
        .then((r) => setState({ data: r.data, meta: r.meta, loading: false }))
        .catch((error) => {
          if (!controller.signal.aborted) setState({ error, loading: false });
        });
    return () => controller.abort();
  }, [path, epoch, reload]);
  const refresh = useCallback(() => setReload((v) => v + 1), []);
  return { ...state, refresh };
}
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : null;
  const text =
    e?.status === 404
      ? "对象不存在、不可见，或接口尚未接入。"
      : e?.kind === "permission_denied"
        ? "当前会话无此动作权限。"
        : e?.kind === "preview_stale"
          ? "预览已失效，请重新预览并确认。"
          : e?.kind === "report_stale"
            ? "验证报告已失效，请重新验证。"
            : e?.kind === "secret_unavailable"
              ? "明文不可再次获取。请通过受控吊销后重新签发。"
              : e?.status === 429
                ? `请求受限；请在 ${Math.ceil(e.retryAfter / 1000)} 秒后重试。`
                : e?.code === "revision_mismatch"
                  ? "资源修订已变化。草稿已保留，请比较最新数据。"
                  : e?.status === 0
                    ? "网络中断，结果未知。重试会复用本次动作幂等键。"
                    : "请求未完成，请检查状态后重试。";
  return (
    <div role="alert" className="notice error">
      {text}
      {e && (
        <small>
          {e.message} · 请求 {e.requestId || "未返回"} ·{" "}
          {JSON.stringify(e.details)}
        </small>
      )}
    </div>
  );
}
export function QueryState({
  query,
  children,
}: {
  query: { loading: boolean; error?: unknown; data?: unknown; meta?: Meta };
  children: ReactNode;
}) {
  if (query.loading)
    return (
      <div role="status" className="empty">
        正在加载…
      </div>
    );
  if (query.error) return <ErrorNotice error={query.error} />;
  return (
    <>
      {query.meta && <MetaLine meta={query.meta} />}
      {children}
    </>
  );
}
export function MetaLine({ meta }: { meta?: Meta }) {
  return meta ? <p className="meta">{metadataText(meta)}</p> : null;
}
export function TextData({ value }: { value: unknown }) {
  return (
    <pre className="text-data">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}
export function Empty({ children = "暂无可见数据" }: { children?: ReactNode }) {
  return <div className="empty">{children}</div>;
}
export function Dialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => {
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      aria-label={title}
    >
      <div className="dialog-title">
        <h2>{title}</h2>
        <button aria-label="关闭对话框" onClick={onClose}>
          ×
        </button>
      </div>
      {children}
    </dialog>
  );
}
function Lookup({
  field,
  value,
  onChange,
}: {
  field: Field;
  value: Value | undefined;
  onChange: (value: Value) => void;
}) {
  const [cursor, setCursor] = useState("");
  const { epoch } = useEnvironment();
  const [items, setItems] = useState<{ id: string; label?: string; fields?: Fields }[]>([]);
  const query = useQuery<{ id: string; label?: string; fields?: Fields }[]>(
    `${field.lookup === "task-steps" ? `/memory/tasks/${encodeURIComponent(field.lookup_parent_id ?? "")}/steps` : `/lookups/${field.lookup}`}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  useEffect(() => {
    setCursor("");
    setItems([]);
  }, [epoch, field.lookup, field.lookup_parent_id]);
  useEffect(() => {
    if (query.data) setItems((old) => uniqueById([...old, ...query.data!]));
  }, [query.data]);
  return (
    <>
      <select
        required={field.required}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">选择已授权对象</option>
        {items.map((item) => (
          <option key={item.id} value={item.id}>
            {field.lookup === "identities" && item.fields
              ? [item.fields.provider, item.fields.realm, item.fields.external_id].map(String).join(" / ")
              : String(item.label ?? item.fields?.display_name ?? item.fields?.name ?? item.fields?.title ?? item.fields?.kind ?? item.id)}
          </option>
        ))}
      </select>
      <ErrorNotice error={query.error} />
      {query.meta?.page?.has_more && (
        <button
          type="button"
          onClick={() => setCursor(query.meta?.page?.next_cursor ?? "")}
        >
          更多选项
        </button>
      )}
    </>
  );
}
export function FieldsForm({
  fields,
  value,
  onChange,
  disabled = false,
}: {
  fields: Field[];
  value: Fields;
  onChange: (v: Fields) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset disabled={disabled} className="fields">
      {fields.map((field) => (
        <label key={field.key}>
          <span>
            {field.label}
            {field.required ? " *" : ""}
          </span>
          {field.description && <small>{field.description}</small>}
          {field.type === "lookup" ? (
            <Lookup
              field={field}
              value={value[field.key]}
              onChange={(v) => onChange({ ...value, [field.key]: v })}
            />
          ) : field.type === "boolean" ? (
            <input
              type="checkbox"
              checked={value[field.key] === true}
              onChange={(e) =>
                onChange({ ...value, [field.key]: e.target.checked })
              }
            />
          ) : field.type === "multi" ? (
            <select
              multiple
              required={field.required}
              value={
                Array.isArray(value[field.key])
                  ? (value[field.key] as string[])
                  : []
              }
              onChange={(e) =>
                onChange({
                  ...value,
                  [field.key]: Array.from(
                    e.target.selectedOptions,
                    (o) => o.value,
                  ),
                })
              }
            >
              {field.options?.map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          ) : field.type === "enum" ? (
            <select
              required={field.required}
              value={String(value[field.key] ?? "")}
              onChange={(e) =>
                onChange({ ...value, [field.key]: e.target.value })
              }
            >
              <option value="">请选择</option>
              {field.options?.map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          ) : field.type === "text" || field.type === "json" ? (
            <textarea
              required={field.required}
              rows={field.type === "json" ? 5 : 3}
              value={
                typeof value[field.key] === "object"
                  ? JSON.stringify(value[field.key], null, 2)
                  : String(value[field.key] ?? "")
              }
              onChange={(e) =>
                onChange({ ...value, [field.key]: e.target.value })
              }
            />
          ) : (
            <input
              required={field.required}
              type={field.type === "secret" ? "password" : "text"}
              autoComplete="off"
              inputMode={
                ["integer", "number", "bytes", "duration_us"].includes(
                  field.type,
                )
                  ? "decimal"
                  : undefined
              }
              value={String(value[field.key] ?? "")}
              onChange={(e) =>
                onChange({ ...value, [field.key]: e.target.value })
              }
              pattern={field.pattern}
            />
          )}
          {field.unit && <small>单位：{field.unit}</small>}
        </label>
      ))}
    </fieldset>
  );
}
export function encodeFields(fields: Field[], value: Fields): Fields {
  const out: Fields = {};
  for (const f of fields) {
    const v = value[f.key];
    if (v === undefined || (v === null && f.type !== "json") || (v === "" && !f.allow_empty)) {
      if (f.required) throw new Error(`请填写 ${f.label}`);
      continue;
    }
    if (f.type === "integer" || f.type === "number") {
      const n = Number(v);
      if (
        !Number.isFinite(n) ||
        (f.type === "integer" && !Number.isSafeInteger(n))
      )
        throw new Error(`${f.label} 超出安全数字范围`);
      out[f.key] = n;
    } else if (f.type === "bytes" || f.type === "duration_us") {
      if (!/^\d+$/.test(String(v)))
        throw new Error(`${f.label} 需要十进制整数`);
      out[f.key] = String(v);
    } else if (f.type === "json") {
      out[f.key] = typeof v === "string" ? (JSON.parse(v) as Value) : v;
    } else out[f.key] = v;
  }
  return out;
}
export function Reason({
  codes,
  value,
  onChange,
}: {
  codes: string[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label>
      操作原因
      <select required value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">请选择服务端原因码</option>
        {codes.map((code) => (
          <option key={code}>{code}</option>
        ))}
      </select>
    </label>
  );
}
export function allowed(
  bootstrap: Bootstrap,
  action: Action,
  resource?: Pick<Resource, "available_actions" | "blocked_actions">,
) {
  return (
    !bootstrap.read_only &&
    !bootstrap.maintenance &&
    bootstrap.permissions.some((p) => p === action.permission) &&
    (!resource || resource.available_actions.includes(action.id)) &&
    !resource?.blocked_actions.some((v) => v.action === action.id)
  );
}
export function ActionButton({
  action,
  resource,
  onClick,
}: {
  action: Action;
  resource?: Pick<Resource, "available_actions" | "blocked_actions">;
  onClick: () => void;
}) {
  const { bootstrap } = useEnvironment();
  const blocked = resource?.blocked_actions.find((v) => v.action === action.id);
  return (
    <span className="action">
      <button
        disabled={!allowed(bootstrap, action, resource)}
        onClick={onClick}
      >
        {action.label}
      </button>
      {blocked && <small>{blocked.reason}</small>}
    </span>
  );
}
export function SecretDialog({
  secret,
  onClose,
}: {
  secret: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  return (
    <Dialog title="密钥仅显示这一次" onClose={onClose}>
      <p>
        确认妥善保存后关闭。轮换继任密钥处于
        pending_confirmation，需用新密钥登录完成交接；未确认前旧密钥保留。
      </p>
      <pre className="secret">{secret}</pre>
      <button
        onClick={() =>
          void navigator.clipboard.writeText(secret).then(() => setCopied(true))
        }
      >
        {copied ? "已复制" : "复制密钥"}
      </button>
      <label>
        <input
          type="checkbox"
          checked={saved}
          onChange={(e) => setSaved(e.target.checked)}
        />
        我已保存
      </label>
      <button disabled={!saved} onClick={onClose}>
        确认并清除
      </button>
    </Dialog>
  );
}
export function ActionDialog({
  action,
  path,
  resource,
  onClose,
  onSuccess,
  bodyBuilder,
  loadLatest,
}: {
  action: Action;
  path: string;
  resource?: Resource;
  onClose: () => void;
  onSuccess: (r: Result<Accepted>) => void;
  bodyBuilder?: (fields: Fields, reason: string) => unknown;
  loadLatest?: () => Promise<{ revision?: number; fields: Record<string, unknown>; label?: string }>;
}) {
  const [draft, setDraft] = useState<Fields>(() => ({
    ...Object.fromEntries(action.fields.filter((f) => f.default !== undefined).map((f) => [f.key, f.default!])),
    ...resource?.fields,
  }));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [localError, setLocalError] = useState("");
  const [latest, setLatest] = useState<{ revision?: number; fields: Record<string, unknown>; label?: string }>();
  const [confirmation, setConfirmation] = useState(false);
  const key = useRef(crypto.randomUUID());
  const lock = useRef(false);
  const { bootstrap } = useEnvironment();
  const visibleFields = action.id === "correct" && resource?.resource_type === "claim" && draft.mode !== "supersede"
    ? action.fields.filter((field) => field.key !== "value" && field.key !== "canonical_text")
    : action.fields;
  const submit = async () => {
    if (lock.current || !allowed(bootstrap, action, resource)) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    setLocalError("");
    try {
      const fields = encodeFields(visibleFields, draft);
      const body = bodyBuilder
        ? bodyBuilder(fields, reason)
        : {
            ...(action.initial_revision !== undefined ? { expected_revision: action.initial_revision } : cas(resource ?? {})),
            ...commandFields(action, fields),
            ...(action.reason_codes.length ? { reason_code: reason } : {}),
          };
      const promise = api.action<Accepted>(path, {
        method: action.method ?? "POST",
        body,
        key: key.current,
        userActivity: true,
      });
      setDraft((prev) =>
        Object.fromEntries(
          Object.entries(prev).filter(
            ([k]) =>
              !action.fields.some((f) => f.key === k && f.type === "secret"),
          ),
        ),
      );
      const result = await promise;
      onSuccess(result);
      onClose();
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e);
        const changed = e.code === "revision_mismatch" || (loadLatest &&
          ["persona_base_revision_stale", "invalid_state_transition"].includes(e.code));
        if (changed && (resource || loadLatest)) {
          const value = await (loadLatest
            ? loadLatest()
            : api.request<Resource>(path.replace(/:[^/]+$/, "")).then((result) => result.data)
          ).catch(() => null);
          if (value) setLatest(value);
        }
      } else setLocalError(e instanceof Error ? e.message : "字段无效");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={action.label}
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <p>{action.description}</p>
        <FieldsForm
          fields={visibleFields}
          value={draft}
          disabled={busy}
          onChange={(v) => {
            setDraft(v);
            key.current = crypto.randomUUID();
          }}
        />
        {action.reason_codes.length > 0 && (
          <Reason
            codes={action.reason_codes}
            value={reason}
            onChange={(v) => {
              setReason(v);
              key.current = crypto.randomUUID();
            }}
          />
        )}
        {action.high_risk && (
          <label>
            <input
              type="checkbox"
              checked={confirmation}
              onChange={(e) => setConfirmation(e.target.checked)}
            />
            确认当前目标、修订、Policy 与 Evidence；敏感动作可能要求重新认证
          </label>
        )}
        {localError && <p role="alert">{localError}</p>}
        <ErrorNotice error={error} />
        {latest && (
          <div className="diff">
            <section>
              <h3>当前草稿（保留）</h3>
              <TextData value={draft} />
            </section>
            <section>
              <h3>{latest.label ?? `服务器最新 Revision ${latest.revision}`}</h3>
              <TextData value={latest.fields} />
            </section>
            <p>请关闭后重新读取目标，再按差异重新编辑；不会自动覆盖。</p>
          </div>
        )}
        <button
          className="primary"
          disabled={
            busy ||
            !!latest ||
            (action.high_risk && !confirmation) ||
            !allowed(bootstrap, action, resource)
          }
        >
          {busy ? "处理中…" : "确认提交"}
        </button>
      </form>
    </Dialog>
  );
}

export function commandFields(action: Action, fields: Fields): Fields {
  const remaining = { ...fields };
  const outer: Fields = {};
  for (const key of [
    "scope",
    "privacy_labels",
    "source_refs",
    "evidence",
    "target_status",
    "target_revision",
    "decision",
    "target_id",
  ])
    if (key in remaining) {
      outer[key] = remaining[key]!;
      delete remaining[key];
    }
  const dimensions = ["agent_id", "space_group_id", "space_id", "session_id"]
    .filter((key) => key in remaining);
  if (dimensions.length) {
    outer.scope = Object.fromEntries(dimensions.map((key) => [key, remaining[key]!])) as Fields;
    for (const key of dimensions) delete remaining[key];
  }
  if (
    [
      "transition",
      "activate",
      "expire",
      "dismiss",
      "confirm",
      "revoke",
      "approve",
      "reject",
      "rollback",
      "review",
      "redirect",
    ].includes(action.id)
  )
    return { ...outer, ...remaining };
  return { ...outer, fields: remaining };
}
