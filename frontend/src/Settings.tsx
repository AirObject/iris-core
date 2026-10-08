import { useEffect, useState } from "react";
import { api, json, errorText, useData } from "./api";
import { Notice, healthLabel } from "./ui";
import { LifecycleSettings } from "./LifecycleSettings";
import { operationNames, actorNames } from "./Operations";
import type { LifecycleConfig, RecallJudgeConfig } from "./types";

type ModelKind = "chat" | "embedding" | "recall_judge";
export const modelNames: Record<string, string> = {
  chat: "对话模型",
  embedding: "Embedding 模型",
  recall_judge: "召回判断模型",
};

export type Role = {
  name: string;
  background: string;
  timezone: string | null;
};
type ModelValue = {
  enabled: boolean;
  base_url: string;
  model: string;
  key_set: boolean;
  dimensions: number | null;
  reasoning_effort: string | null;
  key?: string;
  clearKey?: boolean;
  inherited?: boolean;
};
type Preset = {
  id: string;
  name: string;
  base_url: string;
  model: string;
  reasoning_effort: string | null;
};
export type Settings = {
  role: Role;
  models: Record<ModelKind, ModelValue>;
  model_source: "local" | "external";
  daily_token_limit: number | null;
  learning_concurrency: number;
  lifecycle: LifecycleConfig;
  recall_judge: RecallJudgeConfig;
  presets: Preset[];
  health: Record<string, { state: string }>;
  operations: {
    id: number;
    action: string;
    actor: string;
    created_at: string;
  }[];
};
const timezone = () =>
  Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
const body = (v: ModelValue) =>
  v.inherited
    ? { enabled: false }
    : {
        enabled: v.enabled,
        base_url: v.base_url,
        model: v.model,
        dimensions: v.dimensions,
        reasoning_effort: v.reasoning_effort,
        ...(v.clearKey ? { api_key: "" } : v.key ? { api_key: v.key } : {}),
      };

export function RoleFields({
  value,
  change,
}: {
  value: Role;
  change: (v: Role) => void;
}) {
  return (
    <div className="form-grid">
      <label>
        角色名
        <input
          required
          maxLength={100}
          value={value.name}
          onChange={(e) => change({ ...value, name: e.target.value })}
        />
      </label>
      <label>
        时区
        <input
          required
          value={value.timezone || ""}
          placeholder="Asia/Shanghai"
          onChange={(e) => change({ ...value, timezone: e.target.value })}
        />
      </label>
      <label className="full-width">
        背景材料（可选）
        <textarea
          maxLength={16000}
          rows={4}
          value={value.background}
          onChange={(e) => change({ ...value, background: e.target.value })}
        />
        <small>保存为“设定”的自我记忆，不会当作亲身经历。</small>
      </label>
    </div>
  );
}
function ModelFields({
  kind,
  value,
  change,
  settings,
}: {
  kind: ModelKind;
  value: ModelValue;
  change: (v: ModelValue) => void;
  settings: Settings;
}) {
  const label = modelNames[kind],
    readonly = settings.model_source === "external",
    arkEffort = settings.presets.find(
      (p) => p.id === "ark-glm",
    )?.reasoning_effort;
  const [result, setResult] = useState(""),
    [busy, setBusy] = useState(false);
  const inheriting = kind === "recall_judge" && value.inherited;
  const unsavedInheritance =
    inheriting && !settings.models.recall_judge.inherited;
  const canTest = inheriting
    ? settings.models.chat.enabled && !unsavedInheritance
    : value.enabled;
  const test = async () => {
    setBusy(true);
    setResult("");
    try {
      const r = await api<{ message: string; duration_ms: number }>(
        `/settings/models/${kind}/test`,
        json("POST", readonly || inheriting ? {} : body(value)),
      );
      setResult(`${r.message} · ${r.duration_ms} ms`);
    } catch (e) {
      setResult(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="model-fields">
      {kind === "recall_judge" ? (
        <label className="check-label">
          <input
            type="checkbox"
            disabled={readonly}
            checked={!value.inherited}
            onChange={(e) => {
              setResult("");
              change({ ...value, inherited: !e.target.checked, enabled: true });
            }}
          />
          单独配置召回判断模型
        </label>
      ) : (
        <label className="check-label">
          <input
            type="checkbox"
            disabled={readonly}
            checked={value.enabled}
            onChange={(e) => change({ ...value, enabled: e.target.checked })}
          />
          启用{label}
        </label>
      )}
      {inheriting ? (
        <p>
          沿用已保存的对话模型接口、模型和密钥，推理档位 high。当前模型：
          {settings.models.chat.model || "未配置"}。
        </p>
      ) : (
        <fieldset disabled={readonly || !value.enabled}>
          {kind !== "embedding" && (
            <label>
              {label}服务商预设
              <select
                defaultValue=""
                onChange={(e) => {
                  const p = settings.presets.find(
                    (p) => p.id === e.target.value,
                  );
                  if (p)
                    change({
                      ...value,
                      base_url: p.base_url,
                      model: p.model,
                      reasoning_effort: p.reasoning_effort,
                    });
                }}
              >
                <option value="">自定义 OpenAI 兼容服务</option>
                {settings.presets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            {label}接口地址
            <input
              type="url"
              required={value.enabled}
              value={value.base_url}
              placeholder="https://…/v1"
              onChange={(e) => change({ ...value, base_url: e.target.value })}
            />
          </label>
          <label>
            {label}模型名
            <input
              required={value.enabled}
              value={value.model}
              maxLength={200}
              onChange={(e) => change({ ...value, model: e.target.value })}
            />
          </label>
          <label>
            {label} API key
            <input
              aria-label={`${label} API key`}
              type="password"
              autoComplete="new-password"
              value={value.key || ""}
              placeholder={
                value.key_set ? "已设置；留空保留" : "未设置（本地服务可留空）"
              }
              onChange={(e) =>
                change({ ...value, key: e.target.value, clearKey: false })
              }
            />
            <small>
              密钥{value.key_set ? "已设置" : "未设置"}，保存后不回显。
            </small>
          </label>
          {value.key_set && (
            <label className="check-label">
              <input
                type="checkbox"
                checked={value.clearKey || false}
                onChange={(e) =>
                  change({ ...value, clearKey: e.target.checked, key: "" })
                }
              />
              移除已保存的密钥
            </label>
          )}
          {kind !== "embedding" ? (
            <label>
              {label}推理档位
              <input
                aria-label={`${label}推理档位`}
                value={value.reasoning_effort || ""}
                maxLength={50}
                placeholder={
                  kind === "recall_judge" ? "留空使用 high" : "可选；留空不发送"
                }
                onChange={(e) =>
                  change({ ...value, reasoning_effort: e.target.value || null })
                }
              />
              {arkEffort && <small>火山方舟 GLM 预设使用 {arkEffort}。</small>}
              {kind === "recall_judge" && (
                <small>召回判断档位留空时，后端使用 high。</small>
              )}
            </label>
          ) : (
            <label>
              {label}向量维度
              <input
                type="number"
                min={1}
                max={65536}
                value={value.dimensions ?? ""}
                onChange={(e) =>
                  change({
                    ...value,
                    dimensions: e.target.value ? Number(e.target.value) : null,
                  })
                }
              />
              <small>可选；更换模型或维度后须重新标定召回。</small>
            </label>
          )}
        </fieldset>
      )}
      <div className="button-row">
        <button
          type="button"
          disabled={busy || !canTest}
          onClick={() => void test()}
        >
          {busy ? "正在测试…" : `测试${label}连接`}
        </button>
        {result && <span role="status">{result}</span>}
      </div>
      {unsavedInheritance && (
        <p className="muted">保存沿用设置后可测试连接。</p>
      )}
    </div>
  );
}

export function SetupWizard({ done }: { done: () => void }) {
  const data = useData<Settings>("/settings");
  if (data.error)
    return (
      <Notice error>
        {data.error}
        <button onClick={data.refresh}>重新加载</button>
      </Notice>
    );
  if (!data.data) return <p role="status">正在读取设置…</p>;
  return <SetupForm settings={data.data} done={done} />;
}
function SetupForm({
  settings,
  done,
}: {
  settings: Settings;
  done: () => void;
}) {
  const [role, setRole] = useState({
    ...settings.role,
    timezone: settings.role.timezone || timezone(),
  });
  const [models, setModels] = useState(settings.models),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (settings.model_source === "local")
        for (const kind of ["chat", "embedding"] as const) {
          if (models[kind].enabled || settings.models[kind].enabled)
            await api(
              `/settings/models/${kind}`,
              json("PUT", body(models[kind])),
            );
        }
      await api("/setup/complete", json("POST", role));
      history.replaceState(null, "", "/#/trial");
      done();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="setup-form">
      <section>
        <h2>2 · 认识角色</h2>
        <RoleFields
          value={role}
          change={(v) => setRole({ ...v, timezone: v.timezone || timezone() })}
        />
      </section>
      <section>
        <h2>3 · 配置模型</h2>
        <p>
          对话模型用于学习和试用回复，也可以稍后再配。Embedding
          可选，未配置时使用全文检索。
        </p>
        {settings.model_source === "external" && (
          <Notice>配置来自外部文件，只读；请在外部文件中修改。</Notice>
        )}
        {(["chat", "embedding"] as const).map((kind) => (
          <div className="setup-model" key={kind}>
            <h3>{kind === "chat" ? "对话模型" : "Embedding 模型（可选）"}</h3>
            <ModelFields
              kind={kind}
              value={models[kind]}
              change={(v) => setModels({ ...models, [kind]: v })}
              settings={settings}
            />
          </div>
        ))}
      </section>
      {!models.chat.enabled && (
        <Notice>未配置模型，暂不学习；完成设置后仍可接收消息。</Notice>
      )}
      {error && <Notice error>{error}</Notice>}
      <button className="primary" disabled={busy}>
        {busy ? "正在保存…" : "完成设置并开始试用"}
      </button>
    </form>
  );
}

export default function SettingsPage() {
  const data = useData<Settings>("/settings", 2000);
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">管理</span>
          <h1>设置</h1>
          <p>保存后对新的工作生效。</p>
        </div>
      </div>
      {data.error && <Notice error>{data.error}</Notice>}
      {data.data ? <SettingsForm settings={data.data} /> : <p>正在读取设置…</p>}
    </>
  );
}
function SettingsForm({ settings: initial }: { settings: Settings }) {
  const [settings, setSettings] = useState(initial);
  useEffect(() => setSettings(initial), [initial]);
  const [role, setRole] = useState({
      ...settings.role,
      timezone: settings.role.timezone || timezone(),
    }),
    [models, setModels] = useState(settings.models);
  const [limit, setLimit] = useState(
      settings.daily_token_limit?.toString() || "",
    ),
    [concurrency, setConcurrency] = useState(settings.learning_concurrency),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(""),
    [busy, setBusy] = useState(false);
  const save = async (path: string, method: string, value: unknown) => {
    setBusy(true);
    setError("");
    setSaved("");
    try {
      const latest = await api<Settings>(path, json(method, value));
      setSettings(latest);
      for (const kind of ["chat", "embedding", "recall_judge"] as const) {
        if (path === `/settings/models/${kind}`)
          setModels((v) => ({ ...v, [kind]: latest.models[kind] }));
      }
      setSaved("已保存");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="settings-grid">
      {error && <Notice error>{error}</Notice>}
      {saved && <Notice>{saved}</Notice>}
      <section className="panel settings-panel">
        <h2>角色</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save("/settings/role", "PATCH", role);
          }}
        >
          <RoleFields
            value={role}
            change={(v) =>
              setRole({ ...v, timezone: v.timezone || timezone() })
            }
          />
          <button disabled={busy} className="primary">
            保存角色
          </button>
        </form>
      </section>
      {settings.model_source === "external" && (
        <Notice>
          配置来自外部文件，只读；数据库模型配置和 secrets.json 不用于本次服务。
        </Notice>
      )}
      {(["chat", "embedding", "recall_judge"] as const).map((kind) => (
        <section key={kind} className="panel settings-panel">
          <h2>{modelNames[kind]}</h2>
          <p>
            当前状态：
            {healthLabel(settings.health[kind]?.state || "configuration_error")}
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void save(`/settings/models/${kind}`, "PUT", body(models[kind]));
            }}
          >
            <ModelFields
              kind={kind}
              value={models[kind]}
              change={(v) => setModels({ ...models, [kind]: v })}
              settings={settings}
            />
            {settings.model_source === "local" && (
              <button className="primary" disabled={busy}>
                保存{modelNames[kind]}
              </button>
            )}
          </form>
          {["temporarily_unavailable", "account_problem"].includes(
            settings.health[kind]?.state,
          ) && (
            <button
              onClick={() =>
                void save(`/settings/models/${kind}/retry`, "POST", {})
              }
            >
              立即重试
            </button>
          )}
          {kind === "recall_judge" && (
            <RecallJudgeSettings value={settings.recall_judge} />
          )}
        </section>
      ))}
      <section className="panel settings-panel">
        <h2>用量与学习</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save("/settings/limits", "PATCH", {
              daily_token_limit: limit ? Number(limit) : null,
              learning_concurrency: concurrency,
            });
          }}
        >
          <label>
            每日 token 上限
            <input
              type="number"
              min={1}
              value={limit}
              placeholder="不限"
              onChange={(e) => setLimit(e.target.value)}
            />
          </label>
          <label>
            学习并发
            <input
              type="number"
              required
              min={1}
              max={32}
              value={concurrency}
              onChange={(e) => setConcurrency(Number(e.target.value))}
            />
          </label>
          <p className="muted">
            按角色时区每日恢复；在途调用和未报告的 token
            可能使实际用量超过上限。
          </p>
          <button className="primary" disabled={busy}>
            保存用量与并发
          </button>
        </form>
      </section>
      <section className="panel settings-panel">
        <h2>最近操作</h2>
        <a className="text-button" href="#/operations">
          查看全部操作记录
        </a>
        <ul className="operations">
          {settings.operations.map((o) => (
            <li key={o.id}>
              <span>{operationNames[o.action] || o.action}</span>
              <small>
                {new Date(o.created_at).toLocaleString("zh-CN")} ·{" "}
                {actorNames[o.actor] || o.actor}
              </small>
            </li>
          ))}
        </ul>
      </section>
      <LifecycleSettings
        value={settings.lifecycle}
        timezone={settings.role.timezone}
      />
    </div>
  );
}

function RecallJudgeSettings({ value }: { value: RecallJudgeConfig }) {
  const [enabled, setEnabled] = useState(value.enabled);
  const [concurrency, setConcurrency] = useState(String(value.concurrency));
  const [queue, setQueue] = useState(String(value.queue_limit));
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setSaved(false);
    if (
      !concurrency.trim() ||
      !Number.isInteger(Number(concurrency)) ||
      Number(concurrency) < 1 ||
      Number(concurrency) > 8
    ) {
      setError("召回判断并发须为 1—8 的整数");
      return;
    }
    if (
      !queue.trim() ||
      !Number.isInteger(Number(queue)) ||
      Number(queue) < 0 ||
      Number(queue) > 64
    ) {
      setError("召回判断排队上限须为 0—64 的整数");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api(
        "/settings/recall-judge",
        json("PATCH", {
          enabled,
          concurrency: Number(concurrency),
          queue_limit: Number(queue),
        }),
      );
      setSaved(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="judge-settings" onSubmit={save} noValidate>
      <h3>判断开关与排队</h3>
      <p>
        每次回复准备最多判断 8 条相关记忆，总预算 10
        秒（含排队）。判断失败或暂停时保留仍有效的原召回；人物要点不参与判断。
      </p>
      {error && <Notice error>{error}</Notice>}
      {saved && <Notice>召回判断设置已保存</Notice>}
      <fieldset className="lifecycle-fieldset" disabled={busy}>
        <label className="check-label">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          启用召回判断
        </label>
        <p className="muted">
          关闭后保留基础召回。清除独立模型配置只会恢复沿用对话模型，不会关闭判断。
        </p>
        <div className="form-grid">
          <label>
            召回判断并发
            <input
              type="number"
              min={1}
              max={8}
              step={1}
              required
              value={concurrency}
              onChange={(e) => setConcurrency(e.target.value)}
            />
          </label>
          <label>
            召回判断排队上限
            <input
              type="number"
              min={0}
              max={64}
              step={1}
              required
              value={queue}
              onChange={(e) => setQueue(e.target.value)}
            />
          </label>
        </div>
      </fieldset>
      <button className="primary" disabled={busy}>
        保存召回判断设置
      </button>
    </form>
  );
}
