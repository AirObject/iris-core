import { useState } from "react";
import type { components } from "../api/generated";
import { api } from "../api/client";
import {
  type Accepted,
  type Action,
  type Resource,
  type Result,
} from "../api/design";
import {
  ActionButton,
  ActionDialog,
  Dialog,
  Reason,
  Empty,
  ErrorNotice,
  FieldsForm,
  QueryState,
  SecretDialog,
  TextData,
  useEnvironment,
  useQuery,
} from "../components/core";
import { OperationPanel } from "../components/Operation";
import { Related } from "./Memory";
import protocol from "../api/protocol.json" with { type: "json" };
type KeyView = components["schemas"]["KeyView"];
const reasons = protocol.reason_codes;
export function KeysPage() {
  const plane = "operator";
  const base = plane === "operator" ? "/keys" : "/service-credentials";
  const [cursor, setCursor] = useState("");
  const [filter, setFilter] = useState("");
  const q = useQuery<KeyView[]>(
    `${base}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}${filter ? `&label=${encodeURIComponent(filter)}` : ""}`,
  );
  const { bootstrap } = useEnvironment();
  const [selected, setSelected] = useState<{ action: Action; key?: KeyView }>();
  const [secret, setSecret] = useState<string>();
  const [secretMissing, setSecretMissing] = useState(false);
  const permission =
    plane === "operator" ? "keys.manage" : "service_keys.manage";
  const issue: Action = {
    id: "issue",
    label: "签发密钥",
    permission,
    high_risk: true,
    reason_codes: [],
    fields: [
      { key: "label", label: "名称", type: "string", required: true },
      { key: "description", label: "说明", type: "text" },
      {
        key: "expires_at",
        label: "到期（UTC，保留微秒）",
        type: "string",
        required: true,
      },
      {
        key: "template",
        label: "签发模板（只在创建时展开）",
        type: "enum",
        options: protocol.templates,
        required: true,
      },
      {
        key: "grants",
        label: "不可变 Grant",
        type: "json",
        required: true,
        description:
          "使用显式 selector；空 ids 表示无授权。只能在当前会话的可委托范围内签发。",
      },
    ],
  };
  const keyAction = (id: string): Action => ({
    id,
    label:
      id === "update"
        ? "修改元数据 / 缩短有效期"
        : id === "rotate"
          ? "轮换并交接"
          : "吊销密钥",
    permission,
    method: id === "update" ? "PATCH" : "POST",
    high_risk: true,
    reason_codes: reasons,
    fields:
      id === "update"
        ? [
            { key: "label", label: "名称", type: "string" },
            { key: "description", label: "说明", type: "text" },
            {
              key: "expires_at",
              label: "缩短有效期（不可延长）",
              type: "string",
            },
          ]
        : [],
  });
  return (
    <>
      <h1>密钥与会话</h1>
      <p className="notice">
        Grant 不可原地修改。轮换需显式交接，最后一把可用 Owner
        由服务端保护。宿主凭据不用于 Console 登录。
      </p>
      <label>
        名称筛选
        <input
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setCursor("");
          }}
        />
      </label>
      <ActionButton
        action={issue}
        onClick={() => setSelected({ action: issue })}
      />
      <details>
        <summary>当前可授权范围（服务端返回）</summary>
        <TextData value={api.session?.grants} />
      </details>
      <QueryState query={q}>
        {q.data?.map((k) => (
          <section className="panel" key={k.id}>
            <h2>
              {k.label} · {k.status}
            </h2>
            <p>
              {k.token_prefix} · Revision {k.revision} · 到期 {k.expires_at}
            </p>
            {k.status === "pending_confirmation" && (
              <p className="notice">
                待新密钥登录确认；截止 {k.confirmation_expires_at}
                ，尚未交接时旧密钥继续有效。
              </p>
            )}
            <TextData value={k.grants} />
            {["update", "rotate", "revoke"].map((a) => (
              <ActionButton
                key={a}
                action={keyAction(a)}
                onClick={() => setSelected({ action: keyAction(a), key: k })}
              />
            ))}
          </section>
        ))}
        {!q.data?.length && <Empty />}
        {q.meta?.page?.has_more && (
          <button onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}>
            下一页
          </button>
        )}
      </QueryState>
      {selected && (
        <ActionDialog
          key={selected.key?.id ?? "new"}
          action={selected.action}
          path={`${base}${selected.key ? `/${selected.key.id}${selected.action.id === "update" ? "" : `:${selected.action.id}`}` : ""}`}
          resource={
            selected.key
              ? {
                  id: selected.key.id,
                  resource_type: "key",
                  revision: selected.key.revision,
                  status: selected.key.status,
                  fields: {
                    label: selected.key.label,
                    description: selected.key.description,
                    expires_at: selected.key.expires_at,
                  },
                  available_actions: ["update", "rotate", "revoke"],
                  blocked_actions: [],
                }
              : undefined
          }
          bodyBuilder={(fields, reason) => ({
            ...fields,
            ...{},
            ...(selected.key
              ? {
                  expected_revision: selected.key.revision,
                  reason_code: reason,
                }
              : {}),
          })}
          onClose={() => setSelected(undefined)}
          onSuccess={(r) => {
            setSecret(r.data.secret);
            setSecretMissing(r.data.secret_available === false);
            q.refresh();
          }}
        />
      )}
      {secret && (
        <SecretDialog secret={secret} onClose={() => setSecret(undefined)} />
      )}
      {secretMissing && (
        <p role="alert">
          签发请求已处理，明文已不可再次获取。请按元数据定位密钥，通过吊销后重新签发处理。
        </p>
      )}
      {bootstrap.permissions.length > 0 && <Sessions />}
    </>
  );
}
export function Sessions() {
  const [cursor, setCursor] = useState("");
  const q = useQuery<components["schemas"]["SessionSummary"][]>(
    `/auth/sessions?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
  const [selected, setSelected] =
    useState<components["schemas"]["SessionSummary"]>();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  return (
    <section>
      <h1>会话管理</h1>
      <QueryState query={q}>
        {q.data?.map((session) => (
          <section className="panel" key={session.id}>
            <h2>{session.current ? "当前会话" : session.id}</h2>
            <TextData value={session} />
            <button
              onClick={() => {
                setSelected(session);
                setReason("");
              }}
            >
              吊销此会话
            </button>
          </section>
        ))}
        {q.meta?.page?.has_more && (
          <button onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}>
            下一页会话
          </button>
        )}
      </QueryState>
      <ErrorNotice error={error} />
      {selected && (
        <Dialog
          title="吊销会话"
          onClose={() => {
            if (!busy) setSelected(undefined);
          }}
        >
          <p>此操作使所选会话立即失效。当前会话吊销后返回登录。</p>
          <Reason codes={reasons} value={reason} onChange={setReason} />
          <button
            disabled={busy || !reason}
            onClick={() => {
              setBusy(true);
              void api
                .request(`/auth/sessions/${selected.id}:revoke`, {
                  method: "POST",
                  body: { reason_code: reason },
                })
                .then(() => {
                  if (selected.id === api.session?.session.id) api.clear();
                  else q.refresh();
                  setSelected(undefined);
                })
                .catch(setError)
                .finally(() => setBusy(false));
            }}
          >
            确认吊销
          </button>
        </Dialog>
      )}
    </section>
  );
}
/** Unpublished view/action descriptors; all commands are explicit backend-designed operations. */
interface Workspace {
  title: string;
  description: string;
  fields?: Resource;
  items: Resource[];
  actions: Action[];
  create?: Action;
  reason_codes: string[];
}
export function ManagementPage({
  kind,
}: {
  kind: "personas" | "retention" | "system" | "audit" | "operations";
}) {
  const [section, setSection] = useState(
    kind === "retention" ? "legal-holds" : kind === "system" ? "jobs" : "",
  );
  const [agent, setAgent] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState("");
  const [cursor, setCursor] = useState("");
  const path =
    kind === "personas"
      ? agent.agent_id
        ? `/personas/${agent.agent_id}`
        : null
      : kind === "retention"
        ? `/retention/${section}`
        : kind === "system"
          ? `/system/${section}`
          : `/${kind}`;
  const queryResult = useQuery<Workspace | Resource[]>(
    path
      ? `${path}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}${filter ? `&status=${encodeURIComponent(filter)}` : ""}`
      : null,
  );
  const q = {
    ...queryResult,
    data: Array.isArray(queryResult.data)
      ? {
          items: queryResult.data,
          actions: queryResult.meta?.descriptor?.actions ?? [],
          create: queryResult.meta?.descriptor?.create,
          title: queryResult.meta?.descriptor?.title ?? "",
          description: queryResult.meta?.descriptor?.description ?? "",
          reason_codes: [],
        }
      : queryResult.data,
  };
  const [selection, setSelection] = useState<{
    action: Action;
    resource?: Resource;
  }>();
  const [result, setResult] = useState<Result<Accepted>>();
  return (
    <>
      <h1>
        {kind === "personas"
          ? "Persona 人格"
          : kind === "retention"
            ? "保留策略与 Legal Hold"
            : kind === "system"
              ? "系统运维"
              : kind === "audit"
                ? "只读审计"
                : "Operations"}
      </h1>
      {kind === "personas" && (
        <FieldsForm
          fields={[
            {
              key: "agent_id",
              label: "选择已有 Agent",
              type: "lookup",
              lookup: "agents",
            },
          ]}
          value={agent}
          onChange={(v) => setAgent({ agent_id: String(v.agent_id ?? "") })}
        />
      )}
      {kind === "retention" && (
        <nav className="tabs">
          {["legal-holds", "policies"].map((s) => (
            <button
              key={s}
              onClick={() => {
                setSection(s);
                setCursor("");
              }}
            >
              {s}
            </button>
          ))}
        </nav>
      )}
      {kind === "system" && (
        <>
          <nav className="tabs">
            {["jobs", "schedules", "workers", "indexes", "backups"].map((s) => (
              <button
                key={s}
                onClick={() => {
                  setSection(s);
                  setCursor("");
                }}
              >
                {s}
              </button>
            ))}
          </nav>
          <p className="notice">
            索引重建仅重建派生数据。备份可触发和查询；恢复属于离线运行手册流程。
          </p>
        </>
      )}
      {kind === "personas" && (
        <p className="notice">
          发布与回滚需独立权限、近期认证、基准 Revision、Policy 和
          Evidence；回滚产生新 Revision。模型 Proposal 不会自动发布。
        </p>
      )}
      <label>
        状态筛选
        <input
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setCursor("");
          }}
        />
      </label>
      <QueryState query={q}>
        <h2>{q.data?.title}</h2>
        <p>{q.data?.description}</p>
        {q.data?.fields && <TextData value={q.data.fields} />}
        {q.data?.create && kind !== "audit" && (
          <ActionButton
            action={q.data.create}
            onClick={() => setSelection({ action: q.data!.create! })}
          />
        )}
        {q.data?.items?.map((item) => (
          <section className="panel" key={item.id}>
            <h3>
              {String(item.fields.title ?? item.id)} · {item.status}
            </h3>
            <TextData value={item.fields} />
            {kind !== "audit" &&
              q.data?.actions.map((a) => (
                <ActionButton
                  key={a.id}
                  action={a}
                  resource={item}
                  onClick={() => setSelection({ action: a, resource: item })}
                />
              ))}
            {kind === "operations" && item.fields.operation && (
              <OperationPanel
                initial={
                  item.fields
                    .operation as unknown as import("../api/design").Operation
                }
              />
            )}
          </section>
        ))}
        {!q.data?.items?.length && <Empty />}
        {q.meta?.page?.has_more && (
          <button onClick={() => setCursor(q.meta?.page?.next_cursor ?? "")}>
            下一页
          </button>
        )}
      </QueryState>
      {kind === "personas" && path && (
        <>
          <Related path={`${path}/history`} title="人格发布历史" />
          <Related path={`${path}/proposals`} title="待审 Proposal 与草稿" />
        </>
      )}
      {selection && path && (
        <ActionDialog
          action={selection.action}
          resource={selection.resource}
          path={`${path}${selection.action.suffix?.replace("{id}", selection.resource?.id ?? "") ?? (selection.resource ? `/${selection.resource.id}:${selection.action.id}` : "")}`}
          onClose={() => setSelection(undefined)}
          onSuccess={(r) => {
            setResult(r);
            q.refresh();
          }}
        />
      )}
      {result?.data.operation && (
        <OperationPanel initial={result.data.operation} />
      )}
    </>
  );
}

export function ServiceCredentialsPage() {
  const q = useQuery<components["schemas"]["ServiceCredentialView"][]>(
    "/service-credentials",
  );
  const [selected, setSelected] = useState<{
    action: Action;
    resource?: components["schemas"]["ServiceCredentialView"];
  }>();
  const [secret, setSecret] = useState<string>();
  const [missing, setMissing] = useState(false);
  const issue: Action = {
    id: "issue",
    label: "签发 application 宿主凭据",
    permission: "service_keys.manage",
    high_risk: true,
    reason_codes: reasons,
    fields: [
      { key: "label", label: "名称", type: "string", required: true },
      {
        key: "app_instance_id",
        label: "宿主实例标识",
        type: "string",
        required: true,
      },
      {
        key: "expires_at",
        label: "到期 UTC 微秒时间",
        type: "string",
        required: true,
      },
      {
        key: "data_purposes",
        label: "用途（JSON 数组：reply / planning / reflection / tool）",
        type: "json",
        required: true,
      },
      ...[
        "agent_ids",
        "space_group_ids",
        "space_ids",
        "entity_ids",
        "capabilities",
      ].map((key) => ({
        key,
        label: key,
        type: "json" as const,
        description: "显式授权集合，不自动扩张",
      })),
    ],
  };
  const command = (id: string): Action => ({
    id,
    label:
      id === "update"
        ? "修改元数据 / 缩短有效期"
        : id === "rotate"
          ? "轮换宿主凭据"
          : "吊销宿主凭据",
    permission: "service_keys.manage",
    method: id === "update" ? "PATCH" : "POST",
    reason_codes: reasons,
    high_risk: true,
    fields:
      id === "update"
        ? [
            { key: "label", label: "名称", type: "string" },
            { key: "description", label: "说明", type: "text" },
            { key: "expires_at", label: "缩短有效期", type: "string" },
          ]
        : id === "rotate"
          ? [
              {
                key: "overlap_seconds",
                label: "交叠秒数（0–3600）",
                type: "integer",
              },
            ]
          : [],
  });
  return (
    <>
      <h1>application 宿主凭据</h1>
      <p className="notice">
        仅签发 application
        plane；授权集合不可原地修改。轮换可设置有界交叠期，运营密钥不能作为宿主
        Bearer 使用。
      </p>
      <ActionButton
        action={issue}
        onClick={() => setSelected({ action: issue })}
      />
      <QueryState query={q}>
        {q.data?.map((r) => (
          <section className="panel" key={r.id}>
            <h2>{r.label}</h2>
            <TextData value={r} />
            {["update", "rotate", "revoke"].map((id) => (
              <ActionButton
                key={id}
                action={command(id)}
                onClick={() =>
                  setSelected({ action: command(id), resource: r })
                }
              />
            ))}
          </section>
        ))}
        {!q.data?.length && <Empty />}
      </QueryState>
      {selected && (
        <ActionDialog
          action={selected.action}
          path={`/service-credentials${selected.resource ? `/${selected.resource.id}${selected.action.id === "update" ? "" : `:${selected.action.id}`}` : ""}`}
          resource={
            selected.resource
              ? {
                  id: selected.resource.id,
                  revision: selected.resource.revision,
                  resource_type: "service_credential",
                  status: "active",
                  fields: {
                    label: selected.resource.label,
                    description: selected.resource.description,
                    expires_at: selected.resource.expires_at,
                  },
                  available_actions: ["update", "rotate", "revoke"],
                  blocked_actions: [],
                }
              : undefined
          }
          bodyBuilder={(fields, reason) => ({
            ...fields,
            ...(selected.resource
              ? { expected_revision: selected.resource.revision }
              : { plane: "application" }),
            reason_code: reason,
          })}
          onClose={() => setSelected(undefined)}
          onSuccess={(r) => {
            setSecret(r.data.secret);
            setMissing(r.data.secret_available === false);
            q.refresh();
          }}
        />
      )}
      {secret && (
        <SecretDialog secret={secret} onClose={() => setSecret(undefined)} />
      )}
      {missing && (
        <p role="alert">明文不可再次显示，请定位凭据后受控吊销并重新签发。</p>
      )}
    </>
  );
}
