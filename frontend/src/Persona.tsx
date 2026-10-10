import { useEffect, useRef, useState } from "react";
import { api, apiResponse, ApiError, errorText, json, useData } from "./api";
import { Badge, Dialog, Empty, Notice } from "./ui";
import { PersonaGuide } from "./PersonaGuide";
import {
  PersonaDiff,
  PersonaVersionView,
  type PersonaActionKind,
  type PersonaCatalog,
} from "./PersonaDetails";
import {
  PersonaHistory,
  PersonaMemories,
  PersonaAttempts,
} from "./PersonaHistory";
import { PersonaTask } from "./PersonaTask";
import { activeAttempt, personaReason } from "./persona-labels";
import type {
  PersonaSnapshot,
  PersonaVersion,
  PersonaSummary as VersionSummary,
  PersonaAttempt,
} from "./persona-types";

type Action = {
  kind: PersonaActionKind;
  expectedVersion: number;
  version: VersionSummary;
};
const actionTitles = {
  edit: "编辑并发布 persona",
  confirm: "确认发布候选",
  reject: "拒绝候选",
  rollback: "回滚 persona",
};
const actionButtons = {
  edit: "发布编辑",
  confirm: "确认发布",
  reject: "确认拒绝",
  rollback: "确认回滚",
};
const tabs = {
  current: "当前与候选",
  history: "版本历史",
  memories: "自我记忆",
  attempts: "生成记录",
};
export default function PersonaPage({
  openMemory,
  initialQuery = "",
}: {
  openMemory: (id: number) => void;
  initialQuery?: string;
}) {
  const initial = new URLSearchParams(initialQuery);
  const initialVersion = Number(initial.get("version"));
  const [focusPending, setFocusPending] = useState(
    initial.get("focus") === "pending",
  );
  const pendingTarget = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState(
    Object.hasOwn(tabs, initial.get("tab") || "")
      ? initial.get("tab")!
      : "current",
  );
  const snapshot = useData<PersonaSnapshot>("/persona", 2000),
    catalog = useData<PersonaCatalog>("/catalog");
  const [action, setAction] = useState<Action | null>(null),
    [draft, setDraft] = useState(""),
    [reason, setReason] = useState("");
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [conflict, setConflict] = useState(false),
    [epoch, setEpoch] = useState(0);
  const [receipt, setReceipt] = useState<{
    attempt: PersonaAttempt;
    path?: string;
  } | null>(null);
  const data = snapshot.data,
    current = data?.current,
    pending = data?.pending;
  useEffect(() => {
    if (focusPending && tab === "current" && pending && pendingTarget.current) {
      pendingTarget.current.focus();
      pendingTarget.current.scrollIntoView?.({ block: "start" });
      setFocusPending(false);
    }
  }, [focusPending, tab, pending]);
  const latest = data?.latest_attempt;
  // A terminal snapshot is newer than the original 202 receipt. Once either
  // read has reached a terminal state, an older in-flight snapshot cannot undo it.
  const tracked =
    latest &&
    (!receipt ||
      latest.id > receipt.attempt.id ||
      (latest.id === receipt.attempt.id &&
        activeAttempt(receipt.attempt.state) &&
        !activeAttempt(latest.state)))
      ? {
          attempt: latest,
          path: receipt?.attempt.id === latest.id ? receipt.path : undefined,
        }
      : receipt;
  const locked = busy || conflict || !!snapshot.error || snapshot.loading;
  function begin(kind: PersonaActionKind, version: VersionSummary) {
    if (!current || locked) return;
    setAction({ kind, version, expectedVersion: current.id });
    setDraft(version.content);
    setReason("");
    setError("");
    setMessage("");
  }
  function reload() {
    setAction(null);
    setConflict(false);
    setError("");
    setMessage("");
    snapshot.refresh();
    setEpoch((v) => v + 1);
  }
  function failure(e: unknown) {
    setError(errorText(e));
    if (e instanceof ApiError && e.code === "persona_conflict")
      setConflict(true);
  }
  async function submit() {
    if (!action || locked) return;
    if (action.kind === "edit" && (!draft.trim() || [...draft].length > 800)) {
      setError("persona 正文须为 1—800 字，不能只含空白");
      return;
    }
    if (action.kind === "reject" && [...reason.trim()].length > 1000) {
      setError("拒绝原因不能超过 1000 字");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const body = {
        expected_version: action.expectedVersion,
        ...(action.kind === "edit"
          ? { content: draft }
          : action.kind === "reject" && reason.trim()
            ? { reason: reason.trim() }
            : {}),
      };
      const v = await api<PersonaVersion>(
        action.kind === "edit"
          ? "/persona"
          : `/persona/versions/${action.version.id}/${action.kind}`,
        json(action.kind === "edit" ? "PUT" : "POST", body),
      );
      setMessage(
        action.kind === "reject"
          ? `已拒绝候选 v${v.id}，当前版本仍保留。`
          : `已发布 persona v${v.id}${action.kind === "rollback" ? `，回滚自 v${action.version.id}` : ""}。`,
      );
      setAction(null);
      setTab("current");
      snapshot.refresh();
      setEpoch((v) => v + 1);
    } catch (e) {
      failure(e);
    } finally {
      setBusy(false);
    }
  }
  async function regenerate() {
    if (!current || locked || activeAttempt(tracked?.attempt.state)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await apiResponse<{
        accepted: boolean;
        attempt: PersonaAttempt;
      }>("/persona/regenerate", json("POST", { expected_version: current.id }));
      if (result.status !== 202 || !result.data.accepted)
        throw new ApiError(
          "invalid_response",
          "生成请求返回了意外结果，请刷新查看任务记录；不要重复提交。",
          result.status,
        );
      const attempt = result.data.attempt;
      // Only follow the authenticated local management task resource, never an
      // arbitrary Location URL carrying the administrator session/CSRF token.
      const location = result.headers.get("Location");
      const path =
        location === `/admin/api/persona/attempts/${attempt.id}`
          ? location.slice("/admin/api".length)
          : `/persona/attempts/${attempt.id}`;
      setReceipt({ attempt, path });
      snapshot.refresh();
    } catch (e) {
      failure(e);
      snapshot.refresh();
    } finally {
      setBusy(false);
    }
  }
  const refreshButton = (
    <button disabled={busy} onClick={reload}>
      重新加载当前版本
    </button>
  );
  return (
    <div className="persona-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">自我与未来</span>
          <h1>persona 与自我</h1>
          <p>从持续经历中认识自己，逐句查看变化的依据。</p>
        </div>
        <div className="persona-actions">
          <button disabled={busy} onClick={snapshot.refresh}>
            刷新 persona
          </button>
          {current && (
            <>
              <button disabled={locked} onClick={() => begin("edit", current)}>
                编辑并发布
              </button>
              <button
                className="primary"
                disabled={locked || activeAttempt(tracked?.attempt.state)}
                onClick={() => void regenerate()}
              >
                重新生成
              </button>
            </>
          )}
        </div>
      </div>
      {snapshot.error && (
        <Notice error>
          {snapshot.error}。数据可能尚未更新，写操作暂不可用。
        </Notice>
      )}
      {catalog.error && (
        <Notice error>
          入口类型暂不可用。{catalog.error}
          <button onClick={catalog.refresh}>重试入口信息</button>
        </Notice>
      )}
      {!action && error && (
        <Notice error>
          {error}
          {conflict && (
            <>
              <p>请重新加载后核对当前版本，再决定是否操作。</p>
              {refreshButton}
            </>
          )}
        </Notice>
      )}
      {message && <Notice>{message}</Notice>}
      {data?.needs_update && (
        <section className="notice persona-stale" aria-label="失效依据">
          <h2>
            <Badge tone="warning">待更新</Badge> {data.stale_basis_count}{" "}
            条依据失效
          </h2>
          <p>
            当前 persona
            仍在使用。重新生成会重新提炼与检查；失效依据不等于已有待确认候选。
          </p>
          <ul>
            {data.stale_basis.map((b, i) => (
              <li key={`${b.memory_id}-${i}`}>
                记忆 #{b.memory_id} · 记录修订 {b.expected_revision} ·{" "}
                {b.current_revision == null
                  ? "当前不可用"
                  : `当前修订 ${b.current_revision}`}
                ：{personaReason(b.reason)}
                {b.memory && (
                  <button
                    className="text-button"
                    onClick={() => openMemory(b.memory_id)}
                  >
                    查看失效依据 #{b.memory_id}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {pending && (
        <Notice>
          <Badge tone="warning">待确认</Badge> 候选 v{pending.id}{" "}
          等待决定，当前版本仍在使用。
          <button
            className="text-button"
            onClick={() => {
              setTab("current");
              setFocusPending(true);
            }}
          >
            查看候选与差异
          </button>
        </Notice>
      )}
      {data && !pending && initial.get("focus") === "pending" && (
        <Notice>当前没有待确认的 persona 候选。</Notice>
      )}
      {tracked && (
        <PersonaTask
          key={tracked.attempt.id}
          initial={tracked.attempt}
          path={tracked.path}
          onFinish={(attempt) => {
            setReceipt({ attempt, path: tracked.path });
            snapshot.refresh();
            setEpoch((v) => v + 1);
          }}
        />
      )}
      <PersonaGuide />
      <div
        className="tabs persona-tabs"
        role="tablist"
        aria-label="persona 内容"
      >
        {Object.entries(tabs).map(([value, label]) => (
          <button
            key={value}
            id={`persona-tab-${value}`}
            role="tab"
            aria-selected={tab === value}
            tabIndex={tab === value ? 0 : -1}
            onKeyDown={(e) => {
              const values = Object.keys(tabs),
                index = values.indexOf(value);
              const next =
                e.key === "ArrowRight"
                  ? (index + 1) % values.length
                  : e.key === "ArrowLeft"
                    ? (index + values.length - 1) % values.length
                    : e.key === "Home"
                      ? 0
                      : e.key === "End"
                        ? values.length - 1
                        : null;
              if (next === null) return;
              e.preventDefault();
              setTab(values[next]);
              document.getElementById(`persona-tab-${values[next]}`)?.focus();
            }}
            aria-controls="persona-tab-panel"
            className={tab === value ? "active" : ""}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id="persona-tab-panel"
        aria-labelledby={`persona-tab-${tab}`}
      >
        {tab === "current" && (
          <>
            {!data && !snapshot.error && <p>正在读取 persona…</p>}
            {data && !current && (
              <Empty title="暂无 persona 版本">
                请先在设置中完成角色初始设定。
              </Empty>
            )}
            {current && (
              <PersonaVersionView
                key={`current-${current.id}-${epoch}`}
                id={current.id}
                title="当前 persona"
                catalog={catalog.data}
                openMemory={openMemory}
              />
            )}
            {pending && (
              <div
                ref={pendingTarget}
                role="region"
                aria-label="待确认候选与差异"
                tabIndex={-1}
              >
                <PersonaVersionView
                  key={`pending-${pending.id}-${epoch}`}
                  id={pending.id}
                  title="待确认候选"
                  catalog={catalog.data}
                  openMemory={openMemory}
                  onAction={begin}
                  currentID={current?.id}
                  locked={locked}
                />
                {current && (
                  <div className="panel">
                    <PersonaDiff
                      key={`${current.id}-${pending.id}-${epoch}`}
                      before={current.id}
                      after={pending.id}
                    />
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {tab === "history" && (
          <PersonaHistory
            initialComparison={(() => {
              const before = Number(initial.get("before")),
                after = Number(initial.get("after"));
              return Number.isSafeInteger(before) &&
                before > 0 &&
                Number.isSafeInteger(after) &&
                after > 0
                ? { before, after }
                : undefined;
            })()}
            initialVersion={
              Number.isSafeInteger(initialVersion) && initialVersion > 0
                ? initialVersion
                : undefined
            }
            currentID={current?.id}
            catalog={catalog.data}
            openMemory={openMemory}
            onAction={begin}
            locked={locked}
          />
        )}
        {tab === "memories" && <PersonaMemories openMemory={openMemory} />}
        {tab === "attempts" && <PersonaAttempts />}
      </div>
      {action && (
        <Dialog
          title={actionTitles[action.kind]}
          closeDisabled={busy}
          onClose={() => setAction(null)}
        >
          <form
            noValidate
            className="persona-action-form"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            {action.kind === "edit" ? (
              <>
                <p>
                  发布会替换当前 v{action.expectedVersion}{" "}
                  并新建版本；新增或改动的句子会标为手写。待确认候选会被替代。
                </p>
                <p>
                  之后的自动候选若删去手写内容，按“大变化”处理：默认方式需人工确认；“全部自动”模式下，检查通过仍会直接发布。
                </p>
                <label>
                  persona 正文
                  <textarea
                    rows={9}
                    disabled={busy}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                </label>
                <small>{[...draft].length} / 800 字</small>
              </>
            ) : action.kind === "confirm" ? (
              <p>
                发布候选 v{action.version.id} 会替换当前 v
                {action.expectedVersion}
                。旧版本保留在历史中，可在之后回滚；服务会再次核对版本和依据。
              </p>
            ) : action.kind === "reject" ? (
              <>
                <p>
                  拒绝候选 v{action.version.id}，保留当前 v
                  {action.expectedVersion}
                  。拒绝记录与原因会保存；采用新候选需要再次生成。
                </p>
                <label>
                  拒绝原因（可选）
                  <textarea
                    rows={3}
                    maxLength={1000}
                    disabled={busy}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
              </>
            ) : (
              <p>
                使用 v{action.version.id} 的正文与依据新建一个版本，替换当前 v
                {action.expectedVersion}
                ；历史版本保持不变。待确认候选会被替代，旧依据失效时新版本仍会标为待更新。
              </p>
            )}
            {error && (
              <Notice error>
                {error}
                {conflict && (
                  <>
                    <p>
                      重新加载会放弃本次未保存草稿。可先复制草稿，再核对最新版本。
                    </p>
                    <button type="button" onClick={reload}>
                      重新加载当前版本
                    </button>
                  </>
                )}
              </Notice>
            )}
            <div className="persona-actions">
              <button
                type="button"
                disabled={busy}
                onClick={() => setAction(null)}
              >
                取消
              </button>
              <button className="primary" disabled={locked}>
                {busy ? "正在提交…" : actionButtons[action.kind]}
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  );
}
