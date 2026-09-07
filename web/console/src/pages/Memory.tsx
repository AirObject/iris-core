import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import {
  cas,
  uniqueById,
  type Accepted,
  type Action,
  type Fields,
  type Preview,
  type Resource,
  type ResourceType,
  type Result,
} from "../api/design";
import {
  ActionButton,
  ActionDialog,
  Dialog,
  Empty,
  ErrorNotice,
  FieldsForm,
  MetaLine,
  QueryState,
  Reason,
  TextData,
  useEnvironment,
  useQuery,
} from "../components/core";
import { EntityAttributes, EntityAttributeDialog } from "./EntityAttributes";
import { ArtifactUpload } from "./ArtifactUpload";
import { OperationPanel } from "../components/Operation";
export function MemoryPage() {
  const { collection } = useParams();
  const registry = useQuery<ResourceType[]>("/memory/resource-types");
  const descriptor = registry.data?.find((v) => v.collection === collection);
  return (
    <>
      <h1>记忆管理</h1>
      <QueryState query={registry}>
        <nav className="tabs">
          {registry.data?.map((type) => (
            <Link key={type.collection} to={`/memory/${type.collection}`}>
              {type.label}
            </Link>
          ))}
        </nav>
        {descriptor ? (
          <MemoryCollection key={collection} type={descriptor} />
        ) : (
          <Empty>选择资源类型。只显示服务端已注册且可见的资源。</Empty>
        )}
      </QueryState>
    </>
  );
}
function MemoryCollection({ type }: { type: ResourceType }) {
  const [filters, setFilters] = useState<Fields>({});
  const [applied, setApplied] = useState<Fields>({});
  const [sort, setSort] = useState(type.sorts[0]?.key ?? "");
  const [cursor, setCursor] = useState("");
  const [rows, setRows] = useState<Resource[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [params, setParams] = useSearchParams();
  const id = params.get("id");
  const [action, setAction] = useState<Action>();
  const [forget, setForget] = useState(false);
  const [upload, setUpload] = useState(false);
  const [result, setResult] = useState<Result<Accepted>>();
  const { epoch, bootstrap } = useEnvironment();
  const canForget =
    bootstrap.permissions.includes("memory.forget") &&
    !bootstrap.read_only &&
    !bootstrap.maintenance &&
    type.actions.some((a) => a.id === "forget");
  useEffect(() => { setUpload(false); }, [epoch]);
  const queryString = new URLSearchParams({
    limit: "50",
    ...(sort ? { sort } : {}),
    ...Object.fromEntries(
      Object.entries(applied)
        .filter(([, v]) => v !== "")
        .map(([k, v]) => [k, String(v)]),
    ),
    ...(cursor ? { cursor } : {}),
  }).toString();
  const path = `/memory/${type.collection}`;
  const MemoryActionDialog = action?.id === "attributes" ? EntityAttributeDialog : ActionDialog;
  const q = useQuery<Resource[]>(`${path}?${queryString}`);
  const detail = useQuery<Resource>(
    id ? `${path}/${encodeURIComponent(id)}` : null,
  );
  useEffect(() => {
    setRows([]);
    setCursor("");
    setSelected([]);
  }, [applied, sort, epoch]);
  useEffect(() => {
    if (q.data)
      setRows((old) => (cursor ? uniqueById([...old, ...q.data!]) : q.data!));
  }, [q.data, cursor]);
  useEffect(() => {
    if (q.error instanceof ApiError && q.error.kind === "cursor_invalid") {
      setCursor("");
      setRows([]);
    }
  }, [q.error]);
  const refresh = () => {
    setCursor("");
    setRows([]);
    setSelected([]);
    q.refresh();
    detail.refresh();
  };
  const selectedResources =
    id && detail.data
      ? [detail.data]
      : rows.filter((r) => selected.includes(r.id));
  return (
    <>
      <p className="muted">{type.description}</p>
      <form
        className="filterbar"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied({ ...filters });
          q.refresh();
        }}
      >
        <FieldsForm
          fields={type.filters}
          value={filters}
          onChange={setFilters}
        />
        <label>
          排序
          <select aria-label="排序" value={sort} onChange={(e) => setSort(e.target.value)}>
            {type.sorts.map((s) => (
              <option key={s.key} value={s.key}>{s.label} · {s.direction}</option>
            ))}
          </select>
        </label>
        <button>应用筛选</button>
        <button type="button" onClick={refresh}>
          刷新
        </button>
      </form>
      {upload && type.upload && <ArtifactUpload action={type.upload} onClose={() => setUpload(false)} onSuccess={() => refresh()} />}
      <div className="toolbar">
        {type.upload && <ActionButton action={type.upload} onClick={() => setUpload(true)} />}
        {type.create_schema && type.create && (
          <ActionButton
            action={type.create}
            onClick={() => setAction(type.create)}
          />
        )}
        <button
          disabled={!selected.length || selected.length > (type.supports.forget_max_targets ?? 50) || !canForget}
          onClick={() => setForget(true)}
        >
          预览所选 Forget ({selected.length})
        </button>
        {type.supports.forget_selector && <button
          disabled={!rows.length || !canForget}
          onClick={() => {
            setSelected([]);
            setForget(true);
          }}
        >
          预览筛选集合 Forget
        </button>}
      </div>
      <ErrorNotice error={q.error} />
      <MetaLine meta={q.meta} />
      {q.loading && <p role="status">加载列表…</p>}
      {!rows.length && !q.loading ? (
        <Empty />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">选择</th>
                {type.list_columns.map((c) => (
                  <th scope="col" key={c.key}>{c.label}</th>
                ))}
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <input
                      aria-label={`选择 ${row.id}`}
                      type="checkbox"
                      checked={selected.includes(row.id)}
                      onChange={(e) =>
                        setSelected((old) =>
                          e.target.checked
                            ? [...old, row.id]
                            : old.filter((v) => v !== row.id),
                        )
                      }
                    />
                  </td>
                  {type.list_columns.map((c) => (
                    <td key={c.key}>
                      {String(
                        row.fields[c.key] ??
                          (c.key === "id"
                            ? row.id
                            : c.key === "status"
                              ? row.status
                              : "—"),
                      )}
                    </td>
                  ))}
                  <td>
                    <button onClick={() => setParams({ id: row.id })}>
                      详情
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {q.meta?.page?.has_more && (
        <button
          disabled={q.loading}
          onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}
        >
          加载下一页
        </button>
      )}
      {id && (
        <section className="panel">
          <button onClick={() => setParams({})}>关闭详情</button>
          <QueryState query={detail}>
            {detail.data && (
              <>
                <h2>{String(detail.data.fields.title ?? type.label)}</h2>
                <p>
                  {detail.data.id} · Revision{" "}
                  {detail.data.revision ?? detail.data.version_token} ·{" "}
                  {detail.data.updated_at}
                </p>
                <TextData value={detail.data.fields} />
                <h3>Scope / 隐私 / Evidence 与来源</h3>
                <TextData
                  value={{
                    scope: detail.data.scope,
                    privacy_labels: detail.data.privacy_labels,
                    source_refs: detail.data.source_refs,
                  }}
                />
                <div className="toolbar">
                  {type.actions
                    .filter((a) => a.id !== "forget" && (a.id !== "update" || type.update_schema !== null))
                    .map((a) => (
                      <ActionButton
                        key={a.id}
                        action={a}
                        resource={detail.data}
                        onClick={() => setAction(a)}
                      />
                    ))}
                  {type.actions.find((a) => a.id === "forget") && (
                    <ActionButton
                      action={type.actions.find((a) => a.id === "forget")!}
                      resource={detail.data}
                      onClick={() => setForget(true)}
                    />
                  )}
                </div>
                {detail.data.source && (
                  <Link
                    to={`/memory/${detail.data.source.collection}?id=${encodeURIComponent(detail.data.source.id)}`}
                  >
                    查看 Canonical 来源
                  </Link>
                )}
                {type.collection === "entities" && <EntityAttributes key={detail.meta?.request_id} path={`${path}/${encodeURIComponent(id)}/attributes`} />}
                {type.supports.history && <Related path={`${path}/${encodeURIComponent(id)}/history`} title="修订历史" />}
                {type.supports.references && <Related
                  path={`${path}/${encodeURIComponent(id)}/references`}
                  title="入向 / 出向引用"
                />}
                {type.collection === "tasks" &&
                  ["steps", "dependencies", "triggers"].map((child) => (
                    <TaskChildren
                      key={child}
                      path={`${path}/${encodeURIComponent(id)}/${child}`}
                      parent={detail.data!}
                      kind={child}
                      onSuccess={refresh}
                    />
                  ))}
              </>
            )}
          </QueryState>
        </section>
      )}
      {action && (
        <MemoryActionDialog
          action={action}
          path={
            action === type.create
              ? path
              : `${path}/${encodeURIComponent(id ?? "")}${action.id === "attributes" ? "/attributes" : action.suffix ?? (action.id === "update" ? "" : `:${action.id}`)}`
          }
          resource={action === type.create ? undefined : detail.data}
          onClose={() => setAction(undefined)}
          onSuccess={(r) => {
            setResult(r);
            refresh();
          }}
        />
      )}
      {forget && (
        <ForgetDialog
          targets={selectedResources}
          selector={
            type.supports.forget_selector && !id && !selected.length
              ? { collection: type.collection, filters: Object.fromEntries(Object.entries(applied).filter(([, value]) => value !== "")), sort }
              : undefined
          }
          onClose={() => setForget(false)}
          onSuccess={(r) => {
            setResult(r);
            refresh();
          }}
        />
      )}
      {result && (
        <>
          <MetaLine meta={result.meta} />
          {result.data.operation && (
            <OperationPanel initial={result.data.operation} />
          )}
          <p>
            Canonical：
            {result.data.canonical_status ??
              (result.status === 202 ? "已受理" : "已提交")}{" "}
            · 异步清理：{result.data.cleanup_status ?? "不适用"}
          </p>
        </>
      )}
    </>
  );
}
export function Related({ path, title }: { path: string; title: string }) {
  const [open, setOpen] = useState(false);
  return (
    <section>
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        {title}
      </button>
      {open && <PagedData path={path} />}
    </section>
  );
}
export function PagedData({ path }: { path: string }) {
  const [cursor, setCursor] = useState("");
  const q = useQuery<unknown[]>(
    `${path}${path.includes("?") ? "&" : "?"}limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  return (
    <QueryState query={q}>
      {q.data?.length ? <TextData value={q.data} /> : <Empty />}
      {q.meta?.page?.has_more && (
        <button onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}>
          下一页
        </button>
      )}
    </QueryState>
  );
}
function TaskChildren({
  path,
  parent,
  kind,
  onSuccess,
}: {
  path: string;
  parent: Resource;
  kind: string;
  onSuccess: () => void;
}) {
  const q = useQuery<Resource[]>(path);
  const [selection, setSelection] = useState<{
    action: Action;
    resource?: Resource;
  }>();
  return (
    <section>
      <h3>
        {kind === "steps"
          ? "任务步骤"
          : kind === "dependencies"
            ? "任务依赖"
            : "任务触发器"}
      </h3>
      <QueryState query={q}>
        {q.meta?.descriptor?.create && (
          <ActionButton
            action={q.meta.descriptor.create}
            onClick={() =>
              setSelection({ action: q.meta!.descriptor!.create! })
            }
          />
        )}
        {q.data?.map((item) => (
          <div className="panel" key={item.id}>
            <TextData value={item.fields} />
            {q.meta?.descriptor?.actions.map((a) => (
              <ActionButton
                key={a.id}
                action={a}
                resource={item}
                onClick={() => setSelection({ action: a, resource: item })}
              />
            ))}
          </div>
        ))}
      </QueryState>
      {selection && (
        <TaskChildActionDialog
          detailPath={kind === "triggers" && selection.resource ? `${path}/${encodeURIComponent(selection.resource.id)}` : undefined}
          action={selection.action}
          path={`${path}${selection.resource ? `/${encodeURIComponent(selection.resource.id)}${selection.action.id === "update" ? "" : `:${selection.action.id}`}` : ""}`}
          resource={selection.resource}
          bodyBuilder={(fields, reason) => ({
            ...cas(parent),
            ...(selection.resource ? {
              ...(selection.action.id === "update" ? { fields: {
                ...fields,
                ...(kind === "triggers" ? { task_step_id: fields.task_step_id ?? null } : {}),
              } } : fields),
              child_expected_revision: selection.resource.revision,
            } : { fields }),
            reason_code: reason,
          })}
          onClose={() => setSelection(undefined)}
          onSuccess={() => {
            q.refresh();
            onSuccess();
          }}
        />
      )}
    </section>
  );
}
function TaskChildActionDialog(props: Parameters<typeof ActionDialog>[0] & { detailPath?: string }) {
  const detail = useQuery<Resource>(props.detailPath ?? null);
  if (!props.detailPath) return <ActionDialog {...props} />;
  if (detail.data) return <ActionDialog {...props} resource={detail.data} />;
  return <Dialog title={props.action.label} onClose={props.onClose}>
    <QueryState query={detail}><span>正在读取触发器配置</span></QueryState>
  </Dialog>;
}
export function ForgetDialog({
  targets,
  selector,
  onClose,
  onSuccess,
  base = "/memory",
}: {
  targets: Resource[];
  selector?: Fields;
  onClose: () => void;
  onSuccess: (r: Result<Accepted>) => void;
  base?: string;
}) {
  const [mode, setMode] = useState("soft");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<Preview>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const key = useRef(crypto.randomUUID());
  const lock = useRef(false);
  const { bootstrap } = useEnvironment();
  const descriptor = useQuery<ResourceType[]>("/memory/resource-types");
  const modes = descriptor.data
    ? ["soft", "erase"].filter((candidate) => targets.every((target) => {
        const spec = descriptor.data?.find((row) => row.resource_type === target.resource_type);
        return (spec?.supports.forget_modes ?? ["soft", "erase"]).some((mode) => mode === candidate);
      }))
    : ["soft"];
  const reasons = [
    ...new Set(
      descriptor.data?.flatMap((t) =>
        t.actions
          .filter((a) => a.id === "forget")
          .flatMap((a) => a.reason_codes),
      ) ?? [],
    ),
  ];
  const run = async (commit: boolean) => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    try {
      if (commit && preview) {
        const r = await api.action<Accepted | import("../api/generated").components["schemas"]["ConsoleOperation"]>(`${base}:forget`, {
          method: "POST",
          key: key.current,
          body: {
            preview_id: preview.preview_id,
            preview_hash: preview.preview_hash,
            reason_code: reason,
          },
          userActivity: true,
        });
        onSuccess({ ...r, data: "id" in r.data ? { operation: r.data } : r.data });
        onClose();
      } else {
        const r = await api.action<Preview>(`${base}:forget-preview`, {
          key: key.current,
          method: "POST",
          body: {
            ...(selector
              ? { selector }
              : {
                  targets: targets.map((t) => ({
                    resource_type: t.resource_type,
                    id: t.id,
                    ...cas(t),
                  })),
                }),
            mode,
            reason_code: reason,
          },
          userActivity: true,
        });
        setPreview(r.data);
        setConfirmed(false);
        key.current = crypto.randomUUID();
      }
    } catch (e) {
      setError(e);
      if (e instanceof ApiError && e.kind === "preview_stale") {
        setPreview(undefined);
        setConfirmed(false);
      }
    } finally {
      setBusy(false);
      lock.current = false;
    }
  };
  return (
    <Dialog
      title="Forget · 先预览后确认"
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <p className="notice">
        soft 和 erase 均不可撤销。保护资源和 Legal Hold
        不能绕过。每次最多 500 项；超过 50 项分批执行。筛选预览会固定当前目标，后续新增匹配内容不会加入。
      </p>
      <label>
        方式
        <select
          disabled={busy || !descriptor.data}
          value={mode}
          onChange={(e) => {
            setMode(e.target.value);
            key.current = crypto.randomUUID();
            setConfirmed(false);
            setPreview(undefined);
          }}
        >
          {modes.includes("soft") && <option value="soft">soft · Tombstone 与投影摘除</option>}
          {modes.includes("erase") && <option value="erase">erase · 追加内容擦除</option>}
        </select>
      </label>
      <Reason
        codes={preview?.reason_codes ?? reasons}
        value={reason}
        onChange={(v) => {
          setReason(v);
          key.current = crypto.randomUUID();
          setPreview(undefined);
        }}
      />
      <ErrorNotice error={error} />
      {preview ? (
        <>
          <h3>目标、保护 / Hold 与关联影响</h3>
          <TextData value={preview.targets} />
          <TextData value={preview.impacts} />
          <p>有效至 {preview.expires_at}</p>
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            我确认此预览的固定集合与不可撤销影响
          </label>
          <button
            className="danger"
            disabled={
              busy ||
              !confirmed ||
              !preview.can_commit ||
              Date.parse(preview.expires_at) <= Date.now() ||
              bootstrap.read_only ||
              bootstrap.maintenance ||
              !bootstrap.permissions.includes("memory.forget")
            }
            onClick={() => void run(true)}
          >
            按预览提交 Forget
          </button>
          <button disabled={busy} onClick={() => { setPreview(undefined); setConfirmed(false); key.current = crypto.randomUUID(); }}>重新预览</button>
        </>
      ) : (
        <button
          disabled={
            busy ||
            !descriptor.data ||
            !modes.includes(mode) ||
            !reason ||
            bootstrap.read_only ||
            bootstrap.maintenance ||
            !bootstrap.permissions.includes("memory.forget")
          }
          onClick={() => void run(false)}
        >
          {busy ? "处理中…" : "生成预览"}
        </button>
      )}
    </Dialog>
  );
}
