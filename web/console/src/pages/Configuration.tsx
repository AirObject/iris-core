import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import {
  convertUnits,
  type Accepted,
  type Adapter,
  type Fields,
  type Operation,
  type ProviderView,
  type Registry,
  type SettingsView,
  type Validation,
} from "../api/design";
import {
  ErrorNotice,
  FieldsForm,
  QueryState,
  Reason,
  TextData,
  encodeFields,
  useEnvironment,
  useQuery,
} from "../components/core";
import { OperationPanel } from "../components/Operation";
import { Related } from "./Memory";
export const canActivate = (config: ProviderView | undefined, dirty: boolean) =>
  !!config &&
  !dirty &&
  config.available_actions.includes("activate") &&
  !config.blocked_actions.some((b) => b.action === "activate") &&
  config.probe?.ok === true &&
  Date.parse(config.probe.expires_at) > Date.now() &&
  config.probe.dimension_observed === config.fields.dimension;
export function ProvidersPage() {
  const adapters = useQuery<Adapter[]>("/providers/embedding/adapters");
  const q = useQuery<{ configs: ProviderView[]; active: ProviderView | null }>(
    "/providers/embedding",
  );
  const [adapter, setAdapter] = useState("");
  const definition = adapters.data?.find((a) => a.id === adapter);
  const [draft, setDraft] = useState<Fields>({});
  const [config, setConfig] = useState<ProviderView>();
  const [dirty, setDirty] = useState(false);
  const [reason, setReason] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [local, setLocal] = useState("");
  const [operation, setOperation] = useState<Operation>();
  const key = useRef(crypto.randomUUID());
  const lock = useRef(false);
  const { bootstrap } = useEnvironment();
  const writable =
    bootstrap.permissions.includes("providers.manage") &&
    !bootstrap.read_only &&
    !bootstrap.maintenance;
  const run = async (action: string) => {
    if (lock.current || !writable || !definition) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    setLocal("");
    try {
      const base = "/providers/embedding";
      if (action === "save") {
        const fields = encodeFields(definition.fields, draft);
        if (
          fields.secret_mode === "secret_ref" &&
          fields.secret_ref &&
          !/^(env:[A-Za-z_][A-Za-z0-9_]*|file:\/[^\r\n]+)$/.test(
            String(fields.secret_ref),
          )
        )
          throw new Error("secret_ref 只接受 env: 或 file: 引用");
        const promise = api.action<ProviderView>(
          config ? `${base}/configs/${config.id}` : `${base}/configs`,
          {
            method: config ? "PATCH" : "POST",
            key: key.current,
            body: {
              adapter_id: adapter,
              ...fields,
              ...(config ? { expected_revision: config.revision } : {}),
              reason_code: reason,
            },
            userActivity: true,
          },
        );
        setDraft((v) =>
          Object.fromEntries(
            Object.entries(v).filter(
              ([k]) =>
                !definition.fields.some(
                  (f) => f.key === k && f.type === "secret",
                ),
            ),
          ),
        );
        const r = await promise;
        setConfig(r.data);
        setDraft(r.data.fields);
        setDirty(false);
        setAck(false);
      } else if (action === "test" && config) {
        const r = await api.action<ProviderView>(
          `${base}/configs/${config.id}:test`,
          {
            method: "POST",
            key: key.current,
            body: { expected_revision: config.revision, reason_code: reason },
            userActivity: true,
          },
        );
        setConfig(r.data);
        setOperation(r.data.operation);
        setAck(false);
      } else if (
        action === "activate" &&
        config &&
        canActivate(config, dirty)
      ) {
        const r = await api.action<Accepted>(
          `${base}/configs/${config.id}:activate`,
          {
            method: "POST",
            key: key.current,
            body: {
              expected_revision: config.revision,
              rebuild_ack: config.rebuild_plan_hash,
              reason_code: reason,
            },
            userActivity: true,
          },
        );
        setOperation(r.data.operation);
        setAck(false);
        q.refresh();
      } else if (action === "rollback" && config) {
        const r = await api.action<Accepted>(`${base}:rollback`, {
          method: "POST",
          key: key.current,
          body: {
            target_config_id: config.id,
            expected_revision: q.data?.active?.revision,
            reason_code: reason,
          },
          userActivity: true,
        });
        setOperation(r.data.operation);
        q.refresh();
      } else if (action === "discard" && config) {
        await api.action(`${base}/configs/${config.id}:discard`, {
          method: "POST",
          key: key.current,
          body: { expected_revision: config.revision, reason_code: reason },
          userActivity: true,
        });
        setConfig(undefined);
        setDraft({});
        q.refresh();
      }
      key.current = crypto.randomUUID();
    } catch (e) {
      if (e instanceof ApiError) setError(e);
      else setLocal(e instanceof Error ? e.message : "配置无效");
    } finally {
      setBusy(false);
      lock.current = false;
    }
  };
  return (
    <>
      <h1>Embedding Provider</h1>
      <p className="notice">
        探测由服务器执行。配置变更、新 Generation 和实际服务 Generation
        分开展示；重建完成前继续使用旧 Generation。
      </p>
      <QueryState query={q}>
        <TextData
          value={
            q.data?.active
              ? {
                  active_config: q.data.active.id,
                  config_revision: q.data.active.revision,
                  new_generation: q.data.active.generation,
                  serving_generation: q.data.active.serving_generation,
                }
              : null
          }
        />
        <div className="toolbar">
          {q.data?.configs.map((c) => (
            <button
              key={c.id}
              onClick={() => {
                setConfig(c);
                setDraft(c.fields);
                setAdapter(
                  String(c.fields.adapter_id ?? adapters.data?.[0]?.id ?? ""),
                );
                setDirty(false);
                setAck(false);
              }}
            >
              {c.id} · {c.status}
            </button>
          ))}
        </div>
      </QueryState>
      <QueryState query={adapters}>
        <label>
          适配器
          <select
            disabled={!!config}
            value={adapter}
            onChange={(e) => {
              setAdapter(e.target.value);
              setDraft({});
              setDirty(true);
            }}
          >
            <option value="">选择适配器</option>
            {adapters.data?.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
      </QueryState>
      {definition && (
        <>
          <FieldsForm
            fields={definition.fields}
            value={draft}
            disabled={busy || !writable}
            onChange={(v) => {
              setDraft(v);
              setDirty(true);
              setAck(false);
              key.current = crypto.randomUUID();
            }}
          />
          <Reason
            codes={definition.reason_codes}
            value={reason}
            onChange={(v) => {
              setReason(v);
              key.current = crypto.randomUUID();
            }}
          />
        </>
      )}
      <ErrorNotice error={error} />
      {local && <p role="alert">{local}</p>}
      <div className="toolbar">
        <button
          disabled={
            !definition ||
            busy ||
            !writable ||
            !reason ||
            (!!config && !config.available_actions.includes("update"))
          }
          onClick={() => void run("save")}
        >
          保存草稿
        </button>
        <button
          disabled={
            busy ||
            !config ||
            dirty ||
            !writable ||
            !config.available_actions.includes("test")
          }
          onClick={() => void run("test")}
        >
          服务端探测
        </button>
        <button
          onClick={() => {
            setConfig(undefined);
            setDraft({});
            setAck(false);
          }}
        >
          新建草稿
        </button>
      </div>
      {config && (
        <section className="panel">
          <p>
            {config.id} · Revision {config.revision} · {config.status} · Secret
            hint {config.secret_hint ?? "无"}
          </p>
          <TextData value={config.probe} />
          <h2>服务端副作用与重建计划</h2>
          <TextData value={config.side_effects} />
          <p>计划 hash：{config.rebuild_plan_hash ?? "不需要重建确认"}</p>
          <TextData value={config.blocked_actions} />
          <label>
            <input
              type="checkbox"
              checked={ack}
              onChange={(e) => setAck(e.target.checked)}
            />
            确认以上服务端 side_effects 与 rebuild_plan_hash
          </label>
          <button
            disabled={
              busy ||
              !reason ||
              !ack ||
              !writable ||
              !canActivate(config, dirty)
            }
            onClick={() => void run("activate")}
          >
            激活配置
          </button>
          <button
            disabled={
              busy ||
              !ack ||
              !reason ||
              !writable ||
              !config.available_actions.includes("rollback")
            }
            onClick={() => void run("validate-rollback")}
          >
            重新激活此历史配置
          </button>
          <button
            disabled={
              busy || !writable || !config.available_actions.includes("discard")
            }
            onClick={() => void run("discard")}
          >
            丢弃未激活草稿
          </button>
        </section>
      )}
      <Related path="/providers/embedding/rebuilds" title="重建历史与进度" />
      {operation && <OperationPanel initial={operation} />}
    </>
  );
}
export function SettingsPage() {
  const registry = useQuery<Registry>("/settings/registry");
  const [scope, setScope] = useState<Fields>({ scope_kind: "tenant" });
  const query = new URLSearchParams(
    Object.fromEntries(Object.entries(scope).map(([k, v]) => [k, String(v)])),
  ).toString();
  const q = useQuery<SettingsView>(`/settings?${query}`);
  const [changes, setChanges] = useState<Fields>({});
  const [reason, setReason] = useState("");
  const [validation, setValidation] = useState<Validation>();
  const [confirmed, setConfirmed] = useState("");
  const [error, setError] = useState<unknown>();
  const [local, setLocal] = useState("");
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState<SettingsView>();
  const [result, setResult] = useState<SettingsView>();
  const [targetRevision, setTargetRevision] = useState("");
  const [intent, setIntent] = useState<"save" | "reset" | "rollback">("save");
  const [group, setGroup] = useState("");
  const [unitValue, setUnitValue] = useState("");
  const [unitResult, setUnitResult] = useState("");
  const key = useRef(crypto.randomUUID());
  const lock = useRef(false);
  const { bootstrap } = useEnvironment();
  const writable =
    bootstrap.permissions.includes("settings.write") &&
    !bootstrap.read_only &&
    !bootstrap.maintenance;
  const run = async (action: string) => {
    if (lock.current || !writable || !q.data) return;
    lock.current = true;
    setBusy(true);
    setError(undefined);
    setLocal("");
    try {
      const body = {
        scope,
        changes: encodeFields(
          registry.data?.settings.filter((s) => s.key in changes) ?? [],
          changes,
        ),
        expected_revision: q.data.settings_revision,
        reason_code: reason,
      };
      if (
        ["validate", "validate-reset", "validate-rollback"].includes(action)
      ) {
        const nextIntent =
          action === "validate-reset"
            ? "reset"
            : action === "validate-rollback"
              ? "rollback"
              : "save";
        const r = await api.action<Validation>("/settings:validate", {
          method: "POST",
          key: key.current,
          body:
            nextIntent === "save"
              ? body
              : {
                  scope,
                  intent: nextIntent,
                  ...(nextIntent === "reset"
                    ? { keys: Object.keys(changes) }
                    : { target_revision: Number(targetRevision) }),
                  expected_revision: q.data.settings_revision,
                  reason_code: reason,
                },
          userActivity: true,
        });
        setIntent(nextIntent);
        setValidation(r.data);
        setConfirmed("");
      } else {
        const r = await api.action<SettingsView>(
          action === "save" ? "/settings" : `/settings:${action}`,
          {
            method: action === "save" ? "PATCH" : "POST",
            key: key.current,
            body:
              action === "rollback"
                ? {
                    target_revision: Number(targetRevision),
                    expected_revision: q.data.settings_revision,
                    reason_code: reason,
                  }
                : action === "reset"
                  ? {
                      scope,
                      keys: Object.keys(changes),
                      expected_revision: q.data.settings_revision,
                      reason_code: reason,
                    }
                  : body,
            userActivity: true,
          },
        );
        setResult(r.data);
        setChanges({});
        setValidation(undefined);
        q.refresh();
      }
      key.current = crypto.randomUUID();
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e);
        if (e.code === "revision_mismatch") {
          const r = await api
            .request<SettingsView>(`/settings?${query}`)
            .catch(() => null);
          if (r) setLatest(r.data);
        }
      } else setLocal(e instanceof Error ? e.message : "参数无效");
    } finally {
      setBusy(false);
      lock.current = false;
    }
  };
  const highKeys = validation?.high_risk_keys ?? [];
  return (
    <>
      <h1>运行参数</h1>
      <QueryState query={registry}>
        <FieldsForm
          fields={[
            {
              key: "scope_kind",
              label: "作用域",
              type: "enum",
              options: [
                ...new Set(
                  registry.data?.settings.flatMap((s) => s.scope) ?? [],
                ),
              ],
            },
            {
              key: "scope_id",
              label: "已有 Agent（Agent scope 时必选）",
              type: "lookup",
              lookup: "agents",
            },
          ]}
          value={scope}
          onChange={(v) => {
            setScope(v);
            setChanges({});
            setValidation(undefined);
            setLatest(undefined);
          }}
        />
        <label>
          参数分组
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">全部</option>
            {[...new Set(registry.data?.settings.map((s) => s.group))].map(
              (g) => (
                <option key={g}>{g}</option>
              ),
            )}
          </select>
        </label>
        <QueryState query={q}>
          <p>
            已保存 Revision {q.data?.settings_revision} · {q.data?.status}
          </p>
          <TextData value={q.data?.instances} />
          {registry.data?.settings
            .filter((s) => !group || s.group === group)
            .map((s) => (
              <section className="setting" key={s.key}>
                <h3>{s.key}</h3>
                <p>
                  来源 {q.data?.sources[s.key] ?? s.source} · 默认{" "}
                  {String(s.default)} · 风险 {s.risk} · 生效 {s.apply_mode} ·
                  最小 {s.minimum ?? "—"} / 最大 {s.maximum ?? "—"}
                </p>
                <FieldsForm
                  fields={[s]}
                  value={{
                    [s.key]:
                      changes[s.key] ?? q.data?.values[s.key] ?? s.default,
                  }}
                  disabled={
                    !writable ||
                    !bootstrap.permissions.some((p) => p === s.permission) ||
                    busy
                  }
                  onChange={(v) => {
                    setChanges((old) => ({ ...old, ...v }));
                    setValidation(undefined);
                    key.current = crypto.randomUUID();
                  }}
                />
              </section>
            ))}
        </QueryState>
        <Reason
          codes={registry.data?.reason_codes ?? []}
          value={reason}
          onChange={(v) => {
            setReason(v);
            setValidation(undefined);
            key.current = crypto.randomUUID();
          }}
        />
      </QueryState>
      <details>
        <summary>无损单位换算辅助（秒 → 微秒）</summary>
        <input
          aria-label="整数秒"
          value={unitValue}
          onChange={(e) => setUnitValue(e.target.value)}
        />
        <button
          onClick={() => {
            try {
              setUnitResult(convertUnits(unitValue, "1000000", "1"));
            } catch {
              setUnitResult("只支持可无损表示的非负整数");
            }
          }}
        >
          换算
        </button>
        <output>{unitResult}</output>
      </details>
      <ErrorNotice error={error} />
      {local && <p role="alert">{local}</p>}
      {latest && (
        <div className="diff">
          <section>
            <h3>保留草稿</h3>
            <TextData value={changes} />
          </section>
          <section>
            <h3>服务器最新值</h3>
            <TextData value={latest.values} />
          </section>
          <button
            onClick={() => {
              q.refresh();
              setLatest(undefined);
              setValidation(undefined);
            }}
          >
            读取最新基准后重新验证草稿
          </button>
        </div>
      )}
      <button
        disabled={
          busy ||
          !writable ||
          !Object.keys(changes).length ||
          !reason ||
          !!latest
        }
        onClick={() => void run("validate")}
      >
        验证并查看副作用
      </button>
      {validation && (
        <section className="panel">
          <TextData value={validation.differences} />
          <TextData value={validation.side_effects} />
          {highKeys.length > 0 && (
            <label>
              输入参数名确认（以逗号分隔）：{highKeys.join(",")}
              <input
                value={confirmed}
                onChange={(e) => setConfirmed(e.target.value)}
              />
            </label>
          )}
          <button
            disabled={
              busy ||
              !writable ||
              !validation.valid ||
              !!latest ||
              (highKeys.length > 0 && confirmed !== highKeys.join(","))
            }
            onClick={() => void run(intent)}
          >
            确认并原子提交{" "}
            {intent === "save" ? "修改" : intent === "reset" ? "重置" : "回滚"}
          </button>
        </section>
      )}
      <div className="toolbar">
        <button
          disabled={
            busy || !writable || !reason || !Object.keys(changes).length
          }
          onClick={() => void run("validate-reset")}
        >
          预览重置所选覆盖
        </button>
        <label>
          历史修订
          <input
            value={targetRevision}
            onChange={(e) => {
              setTargetRevision(e.target.value);
              setValidation(undefined);
            }}
          />
        </label>
        <button
          disabled={
            busy ||
            !writable ||
            !reason ||
            !/^\d+$/.test(targetRevision) ||
            !Number.isSafeInteger(Number(targetRevision))
          }
          onClick={() => void run("validate-rollback")}
        >
          预览回滚为新修订
        </button>
      </div>
      {result && (
        <section className="notice">
          <h2>已保存 · {result.status}</h2>
          <p>待重启参数在实例应用前不会标为已生效。</p>
          <TextData value={result.side_effects} />
          <TextData value={result.instances} />
        </section>
      )}
      <Related path="/settings/history" title="参数历史" />
    </>
  );
}
