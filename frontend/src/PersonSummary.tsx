import { useCallback, useEffect, useRef, useState } from "react";
import { apiResponse, ApiError, errorText, json, useData } from "./api";
import { Badge, Dialog, Notice, Pagination, fullTime } from "./ui";
import type { Page } from "./types";
import type {
  SummaryAttempt,
  SummaryDetail,
  SummaryDiff,
  SummaryItem,
  SummarySnapshot,
  SummaryVersion,
} from "./person-summary-types";
import { SummaryDetails, SummaryStale } from "./PersonSummaryDetails";
import { summaryReason, summaryStatuses } from "./person-summary-labels";

// Match the backend's sentence boundaries while retaining unchanged structured lines.
function splitCount(text: string) {
  const chars = Array.from(text);
  let start = 0,
    count = 0,
    i = 0;
  while (i < chars.length) {
    const c = chars[i];
    const boundary =
      "。！？!?\n".includes(c) ||
      (c === "." && (i + 1 === chars.length || /\s/u.test(chars[i + 1])));
    i++;
    if (!boundary) continue;
    while (i < chars.length && '。！？!?”’」』"'.includes(chars[i])) i++;
    if (chars.slice(start, i).join("").trim()) {
      count++;
      start = i;
    }
  }
  return count + Number(!!chars.slice(start).join("").trim());
}
export function summaryBudget(content: string, previous: SummaryItem[] = []) {
  const known = [
    ...new Set(previous.filter((s) => s.kind).map((s) => s.text)),
  ].sort((a, b) => b.length - a.length);
  let count = 0,
    start = 0;
  if (known.length) {
    const pattern = new RegExp(
      "^(?:" +
        known
          .map((text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
          .join("|") +
        ")",
      "gm",
    );
    for (const match of content.matchAll(pattern)) {
      count += splitCount(content.slice(start, match.index)) + 1;
      start = match.index! + match[0].length;
    }
  }
  count += splitCount(content.slice(start));
  const characters = Array.from(content),
    han = characters.filter((c) => /[\u4e00-\u9fff]/u.test(c)).length;
  return {
    items: count,
    characters: characters.length,
    tokens: Math.max(1, han + Math.floor((characters.length - han + 2) / 3)),
  };
}
function SummaryTask({
  base,
  initial,
  onSettled,
  view,
}: {
  base: string;
  initial: SummaryAttempt;
  onSettled: () => void;
  view: (v: number) => void;
}) {
  const [polling, setPolling] = useState(initial.state === "running");
  const task = useData<SummaryAttempt>(
    `${base}/attempts/${initial.id}`,
    polling ? 1500 : 0,
  );
  const completed = useRef(false);
  useEffect(() => {
    if (task.data && task.data.state !== "running" && !completed.current) {
      completed.current = true;
      setPolling(false);
      onSettled();
    }
  }, [task.data, onSettled]);
  const value = task.data || initial;
  return (
    <section className="entry-profile-section" aria-label="摘要生成任务">
      <h3>生成任务 #{value.id}</h3>
      {task.error && (
        <Notice error>
          {task.error}
          <button onClick={task.refresh}>重试任务状态</button>
        </Notice>
      )}
      <p role="status">
        {value.state === "running"
          ? value.stage === "checking"
            ? "正在检查候选…"
            : "正在生成候选…"
          : summaryStatuses[value.state] || value.state}
      </p>
      {value.reason && <p>{summaryReason(value.reason)}</p>}
      <p className="fine-print">
        开始：{fullTime(value.started_at)}
        {value.finished_at && ` · 结束：${fullTime(value.finished_at)}`}
      </p>
      {value.state === "rejected" && (
        <p>
          当前发布版保持不变。确定性拒绝后自动整理等待新依据；管理员仍可主动重新生成。
        </p>
      )}
      {value.cleared_at && <p>任务关联内容因彻底清除而清空。</p>}
      {value.version != null && (
        <button onClick={() => view(value.version!)}>
          查看任务版本 v{value.version}
        </button>
      )}
    </section>
  );
}

type Draft = {
  content: string;
  expected_version: number;
  original: SummaryItem[];
};
type SettingsDraft = {
  enabled: boolean;
  publish_mode: "check_auto" | "all_manual";
  expected_version: number;
};
type Action = {
  kind: "confirm" | "reject" | "rollback";
  version: number;
  expected_version: number;
};
export default function PersonSummaryPanel({
  subjectId,
  initialVersion,
  openMemory,
}: {
  subjectId: string;
  initialVersion?: number;
  openMemory: (id: number) => void;
}) {
  const base = `/people/${encodeURIComponent(subjectId)}/summary`;
  const snapshot = useData<SummarySnapshot>(base, 5000);
  const [offset, setOffset] = useState(0),
    [selected, setSelected] = useState<number | null>(initialVersion || null);
  const history = useData<Page<SummaryVersion>>(
    `${base}/versions?limit=10&offset=${offset}`,
  );
  const current = useData<SummaryDetail>(
    snapshot.data?.current
      ? `${base}/versions/${snapshot.data.current.version}`
      : null,
  );
  const detail = useData<SummaryDetail>(
    selected ? `${base}/versions/${selected}` : null,
  );
  const [comparison, setComparison] = useState<string | null>(null),
    diff = useData<SummaryDiff>(comparison);
  const [before, setBefore] = useState("1"),
    [after, setAfter] = useState("2");
  const [draft, setDraft] = useState<Draft | null>(null),
    [settings, setSettings] = useState<SettingsDraft | null>(null);
  const [action, setAction] = useState<Action | null>(null),
    [rejection, setRejection] = useState("");
  const [accepted, setAccepted] = useState<SummaryAttempt | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [conflict, setConflict] = useState(false);
  const submitting = useRef(false),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const refresh = useCallback(() => {
    snapshot.refresh();
    history.refresh();
    current.refresh();
    detail.refresh();
    diff.refresh();
  }, [
    snapshot.refresh,
    history.refresh,
    current.refresh,
    detail.refresh,
    diff.refresh,
  ]);
  const value = snapshot.data,
    version = value?.settings.current_version ?? 0;
  const staleSignature = JSON.stringify(value?.stale_basis || []);
  useEffect(() => {
    history.refresh();
    current.refresh();
    detail.refresh();
  }, [
    version,
    value?.current?.cleared_at,
    staleSignature,
    value?.pending?.version,
    value?.latest_attempt?.state,
    history.refresh,
    current.refresh,
    detail.refresh,
  ]);
  const view = (v: number) => {
    setSelected(v);
    setError("");
  };
  const latest = value?.latest_attempt;
  const attempt =
    latest && (!accepted || latest.id >= accepted.id) ? latest : accepted;
  const working = attempt?.state === "running";
  const locked = busy || snapshot.loading || !!snapshot.error || conflict;
  const ready =
    !value?.current ||
    (current.data?.version === version && !current.loading && !current.error);
  const budget = summaryBudget(draft?.content || "", draft?.original);
  const limits = value?.edit_limits || {
    max_items: 5,
    max_chars: 200,
    max_tokens: 250,
  };
  const invalid =
    !draft?.content.trim() ||
    budget.characters > limits.max_chars ||
    budget.tokens > limits.max_tokens ||
    budget.items > limits.max_items;
  async function change(
    path: string,
    method: string,
    body: unknown,
    message: string,
  ) {
    if (submitting.current) return false;
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await apiResponse<{
        accepted?: boolean;
        attempt?: SummaryAttempt;
      }>(base + path, json(method, body));
      if (alive.current) {
        if (
          path === "/regenerate" &&
          result.status === 202 &&
          result.data.attempt
        )
          setAccepted(result.data.attempt);
        setNotice(message);
        setAction(null);
        refresh();
      }
      return true;
    } catch (e) {
      if (alive.current) {
        const stale = e instanceof ApiError && e.status === 409;
        setConflict(stale);
        setError(
          stale
            ? "当前摘要、设置、依据或生成任务已变化。已刷新版本；请核对后再操作，编辑草稿已保留。"
            : errorText(e),
        );
        if (stale) setAction(null);
        refresh();
      }
      return false;
    } finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const compare = (left: number, right: number) => {
    setBefore(String(left));
    setAfter(String(right));
    setComparison(`${base}/diff?before_version=${left}&after_version=${right}`);
  };
  return (
    <section className="panel person-summary" aria-label="人物摘要">
      <div className="panel-heading">
        <h2>人物摘要</h2>
        <button disabled={busy} onClick={refresh}>
          刷新人物摘要
        </button>
      </div>
      <p className="muted">
        从与此人物相关、全部入口共享的有效记忆提炼。查看与轮询不会召回或增加使用次数。
      </p>
      {(snapshot.error || error) && (
        <Notice error>{error || snapshot.error}</Notice>
      )}
      {notice && <Notice>{notice}</Notice>}
      {!value ? (
        <p role="status">正在读取人物摘要…</p>
      ) : (
        <>
          {!value.supported && (
            <Notice>
              此主体类型或已合并人物不支持摘要写操作，可查阅已有历史。
            </Notice>
          )}
          {!value.global_enabled && (
            <Notice>全局人物摘要开关已关闭，不会生成或加入回复准备。</Notice>
          )}
          {conflict && (
            <button
              disabled={busy || snapshot.loading || !!snapshot.error || !ready}
              onClick={() => {
                setConflict(false);
                setError("");
                setDraft((d) =>
                  d
                    ? {
                        ...d,
                        expected_version: version,
                        original: current.data?.sentences || [],
                      }
                    : null,
                );
                setSettings((s) =>
                  s ? { ...s, expected_version: version } : null,
                );
              }}
            >
              已核对最新版本，继续操作
            </button>
          )}
          <p>
            <Badge>
              {value.settings.enabled ? "此人摘要已开启" : "此人摘要已关闭"}
            </Badge>{" "}
            ·{" "}
            {value.settings.publish_mode === "all_manual"
              ? "全部人工确认"
              : "检查通过自动发布"}{" "}
            ·{" "}
            <Badge tone={value.pending_count ? "amber" : ""}>
              待确认 {value.pending_count}
            </Badge>
          </p>
          {value.supported && (
            <div className="button-row">
              <button
                disabled={locked || !ready}
                onClick={() =>
                  setDraft({
                    content: value.current?.content || "",
                    expected_version: version,
                    original: current.data?.sentences || [],
                  })
                }
              >
                编辑摘要
              </button>
              <button
                disabled={locked}
                onClick={() =>
                  setSettings({
                    enabled: value.settings.enabled,
                    publish_mode: value.settings.publish_mode,
                    expected_version: version,
                  })
                }
              >
                人物摘要设置
              </button>
              <button
                disabled={
                  locked ||
                  working ||
                  !value.global_enabled ||
                  !value.settings.enabled
                }
                onClick={() =>
                  void change(
                    "/regenerate",
                    "POST",
                    { expected_version: version },
                    "已接受重新生成，请查看任务进度。",
                  )
                }
              >
                {working ? "摘要生成中…" : "重新生成摘要"}
              </button>
            </div>
          )}
          <p className="fine-print">
            重新生成至少需要 8
            条相关全局有效记忆，并受开关、版本检查与模型预算限制。
          </p>
          {settings && (
            <form
              className="entry-profile-section"
              aria-label="人物摘要设置"
              onSubmit={async (e) => {
                e.preventDefault();
                if (locked || settings.expected_version !== version) return;
                if (
                  await change(
                    "/settings",
                    "PATCH",
                    settings,
                    "人物摘要设置已保存",
                  )
                )
                  setSettings(null);
              }}
            >
              <label className="check-label">
                <input
                  type="checkbox"
                  disabled={busy}
                  checked={settings.enabled}
                  onChange={(e) =>
                    setSettings({ ...settings, enabled: e.target.checked })
                  }
                />
                启用此人的摘要
              </label>
              <label>
                摘要发布方式
                <select
                  disabled={busy}
                  value={settings.publish_mode}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      publish_mode: e.target
                        .value as SettingsDraft["publish_mode"],
                    })
                  }
                >
                  <option value="check_auto">检查通过自动发布</option>
                  <option value="all_manual">全部人工确认</option>
                </select>
              </label>
              <p>
                关闭后保留历史；发布方式只影响之后的候选，不会发布已有待确认版本。
              </p>
              {settings.expected_version !== version && (
                <Notice>设置基于旧版本，请取消后重新打开设置。</Notice>
              )}
              <div className="button-row">
                <button
                  disabled={locked || settings.expected_version !== version}
                >
                  保存人物摘要设置
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setSettings(null)}
                >
                  取消设置
                </button>
              </div>
            </form>
          )}
          {draft && (
            <form
              className="entry-profile-section"
              aria-label="编辑人物摘要"
              onSubmit={async (e) => {
                e.preventDefault();
                if (locked || invalid || draft.expected_version !== version)
                  return;
                if (
                  await change(
                    "",
                    "PUT",
                    {
                      content: draft.content,
                      expected_version: draft.expected_version,
                    },
                    "摘要编辑已发布",
                  )
                )
                  setDraft(null);
              }}
            >
              <h3>编辑并直接发布</h3>
              <p>
                新增或改动条目标为管理员手写，之后生成会保留；未改条目保留原依据。按句号或换行分条。
              </p>
              <label>
                人物摘要正文
                <textarea
                  rows={6}
                  disabled={busy}
                  value={draft.content}
                  onChange={(e) =>
                    setDraft({ ...draft, content: e.target.value })
                  }
                />
              </label>
              <p>
                {budget.items} / {limits.max_items} 条 · {budget.characters} /{" "}
                {limits.max_chars} 字 · {budget.tokens} / {limits.max_tokens}{" "}
                估算 token（无最低字数） · 基于 v{draft.expected_version}
              </p>
              {draft.expected_version !== version && (
                <Notice>
                  当前版本已变化，草稿已保留。请核对最新摘要后更新草稿版本。
                  <button
                    type="button"
                    disabled={!ready || locked}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        expected_version: version,
                        original: current.data?.sentences || [],
                      })
                    }
                  >
                    更新草稿版本
                  </button>
                </Notice>
              )}
              <div className="button-row">
                <button
                  disabled={
                    locked || invalid || draft.expected_version !== version
                  }
                >
                  发布摘要编辑
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setDraft(null)}
                >
                  取消编辑摘要
                </button>
              </div>
            </form>
          )}
          <section className="entry-profile-section" aria-label="当前人物摘要">
            <h3>当前摘要</h3>
            {!value.current ? (
              <p>尚无已发布的人物摘要。</p>
            ) : current.error ? (
              <Notice error>
                {current.error}
                <button onClick={current.refresh}>重试当前摘要</button>
              </Notice>
            ) : !current.data || current.data.version !== version ? (
              <p>正在读取当前摘要…</p>
            ) : (
              <SummaryDetails value={current.data} openMemory={openMemory} />
            )}
            {!current.data && (
              <SummaryStale value={value.stale_basis} openMemory={openMemory} />
            )}
          </section>
          {value.pending && (
            <section
              className="entry-profile-section"
              aria-label="待确认人物摘要"
            >
              <h3>待确认候选 · v{value.pending.version}</h3>
              <p className="entry-profile-content">{value.pending.content}</p>
              <div className="button-row">
                <button onClick={() => view(value.pending!.version)}>
                  审阅候选 v{value.pending.version}
                </button>
                {value.current && (
                  <button
                    onClick={() => compare(version, value.pending!.version)}
                  >
                    对比当前与候选
                  </button>
                )}
              </div>
            </section>
          )}
          {attempt && (
            <SummaryTask
              key={attempt.id}
              base={base}
              initial={attempt}
              onSettled={refresh}
              view={view}
            />
          )}
          <section className="entry-profile-section" aria-label="人物摘要历史">
            <h3>版本历史</h3>
            {history.error && (
              <Notice error>
                {history.error}
                <button onClick={history.refresh}>重试摘要历史</button>
              </Notice>
            )}
            {history.data?.items.map((v) => (
              <div className="entry-profile-evidence" key={v.version}>
                <button onClick={() => view(v.version)}>
                  查看摘要 v{v.version}
                </button>{" "}
                <Badge>
                  {v.is_current
                    ? "当前版本"
                    : summaryStatuses[v.status] || v.status}
                </Badge>
                {v.cleared_at && <Badge tone="warning">因彻底清除而清空</Badge>}
                <p>{fullTime(v.generated_at)}</p>
              </div>
            ))}
            {history.data && !history.data.total && <p>暂无历史版本。</p>}
            {history.data && (
              <Pagination
                total={history.data.total}
                offset={offset}
                change={setOffset}
                limit={10}
              />
            )}
          </section>
          {selected != null && (
            <section
              className="entry-profile-section"
              aria-label="人物摘要版本详情"
            >
              <h3>摘要 v{selected} 的依据与检查</h3>
              {detail.error ? (
                <Notice error>
                  {detail.error}
                  <button onClick={detail.refresh}>重试摘要版本</button>
                </Notice>
              ) : !detail.data ? (
                <p>正在读取版本…</p>
              ) : (
                <>
                  <SummaryDetails value={detail.data} openMemory={openMemory} />
                  {value.supported && detail.data.status === "candidate" && (
                    <div className="button-row">
                      <button
                        disabled={
                          locked ||
                          !!detail.data.cleared_at ||
                          !detail.data.checks.passed
                        }
                        onClick={() =>
                          setAction({
                            kind: "confirm",
                            version: selected,
                            expected_version: version,
                          })
                        }
                      >
                        确认发布摘要 v{selected}
                      </button>
                      <button
                        disabled={locked}
                        onClick={() => {
                          setRejection("");
                          setAction({
                            kind: "reject",
                            version: selected,
                            expected_version: version,
                          });
                        }}
                      >
                        拒绝摘要 v{selected}
                      </button>
                    </div>
                  )}
                  {value.supported &&
                    detail.data.status === "published" &&
                    !detail.data.is_current && (
                      <button
                        disabled={
                          locked ||
                          !!detail.data.cleared_at ||
                          !!detail.data.invalidated_reason ||
                          !detail.data.content.trim()
                        }
                        onClick={() =>
                          setAction({
                            kind: "rollback",
                            version: selected,
                            expected_version: version,
                          })
                        }
                      >
                        回滚到摘要 v{selected}
                      </button>
                    )}
                </>
              )}
            </section>
          )}
          <section className="entry-profile-section" aria-label="摘要版本对比">
            <h3>任意两版对比</h3>
            <form
              className="button-row"
              onSubmit={(e) => {
                e.preventDefault();
                if (
                  Number.isSafeInteger(+before) &&
                  +before > 0 &&
                  Number.isSafeInteger(+after) &&
                  +after > 0
                )
                  compare(+before, +after);
              }}
            >
              <label>
                较早版本
                <input
                  type="number"
                  min={1}
                  required
                  value={before}
                  onChange={(e) => setBefore(e.target.value)}
                />
              </label>
              <label>
                较晚版本
                <input
                  type="number"
                  min={1}
                  required
                  value={after}
                  onChange={(e) => setAfter(e.target.value)}
                />
              </label>
              <button>对比摘要版本</button>
            </form>
            {diff.error && (
              <Notice error>
                {diff.error}
                <button onClick={diff.refresh}>重试摘要对比</button>
              </Notice>
            )}
            {diff.data && (
              <>
                <p>
                  v{diff.data.before} → v{diff.data.after}
                  {!diff.data.changed && " · 条目没有变化"}
                </p>
                {diff.data.sentences.map((d, i) => (
                  <div className="persona-diff-row" key={i}>
                    <strong>
                      {(
                        {
                          equal: "保留",
                          replace: "修改",
                          insert: "新增",
                          delete: "删除",
                        } as Record<string, string>
                      )[d.op] || d.op}
                    </strong>
                    <div className="persona-diff-columns">
                      <div>
                        <small>之前</small>
                        {d.before.map((s, j) => (
                          <p key={j} className="entry-profile-content">
                            {s.text}
                          </p>
                        ))}
                      </div>
                      <div>
                        <small>之后</small>
                        {d.after.map((s, j) => (
                          <p key={j} className="entry-profile-content">
                            {s.text}
                          </p>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}
              </>
            )}
          </section>
          <a
            href={`#/operations?object_type=person_summary&object_id=${encodeURIComponent(subjectId)}`}
          >
            查看人物摘要操作记录
          </a>
          {action && (
            <Dialog
              title={
                action.kind === "confirm"
                  ? "确认发布人物摘要"
                  : action.kind === "reject"
                    ? "拒绝人物摘要"
                    : "回滚人物摘要"
              }
              onClose={() => setAction(null)}
              closeDisabled={busy}
            >
              <p>
                {action.kind === "rollback"
                  ? `将复制 v${action.version}，新建并发布一个版本；历史版本保持原记录。`
                  : action.kind === "confirm"
                    ? `将发布候选 v${action.version}，替换当前摘要。`
                    : `将拒绝候选 v${action.version}，保留当前发布版。`}
              </p>
              {action.kind === "reject" && (
                <label>
                  拒绝摘要原因
                  <input
                    maxLength={1000}
                    value={rejection}
                    onChange={(e) => setRejection(e.target.value)}
                    disabled={busy}
                  />
                </label>
              )}
              <p>请核对条目、依据和检查记录后再次确认。</p>
              {error && <Notice error>{error}</Notice>}
              <div className="button-row">
                <button disabled={busy} onClick={() => setAction(null)}>
                  取消操作
                </button>
                <button
                  className="primary"
                  disabled={locked || action.expected_version !== version}
                  onClick={() =>
                    void change(
                      `/versions/${action.version}/${action.kind}`,
                      "POST",
                      {
                        expected_version: action.expected_version,
                        ...(action.kind === "reject"
                          ? { reason: rejection.trim() || "管理员拒绝" }
                          : {}),
                      },
                      action.kind === "rollback"
                        ? "已新建并发布回滚版本"
                        : action.kind === "confirm"
                          ? "候选已确认发布"
                          : "候选已拒绝",
                    )
                  }
                >
                  确认
                  {action.kind === "rollback"
                    ? "回滚"
                    : action.kind === "confirm"
                      ? "发布"
                      : "拒绝"}
                </button>
              </div>
            </Dialog>
          )}
        </>
      )}
    </section>
  );
}
