import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import {
  convertUnits,
  type Fields,
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
import { Related } from "./Memory";
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
