import {
  ProfileItemView,
  StructuredProfileEditor,
  editItem,
  itemRequest,
  profileDraftBudget,
  type ProfileDraft,
} from "./EntryProfileItems";
import { useEffect, useRef, useState } from "react";
import { api, errorText, json, useData } from "./api";
import { Badge, Dialog, Notice, Pagination, fullTime } from "./ui";
import type {
  ProfileBasis,
  ProfileDetail,
  ProfileDiff,
  ProfileSnapshot,
  ProfileSentence,
  ProfileVersion,
} from "./entry-profile-types";

const degree = (value: string) =>
  ({ small: "小", medium: "中", large: "大" })[value] || value;
const source = (value: string) =>
  ({
    periodic: "定期更新",
    regenerate: "重新生成",
    edit: "管理员编辑",
    rollback: "回滚",
  })[value] || value;
const status = (value: string) =>
  ({
    published: "已发布",
    candidate: "待确认",
    rejected: "被拒绝",
    failed: "失败",
    conflict: "版本冲突",
    skipped: "已跳过",
  })[value] || value;
const reasons: Record<string, string> = {
  message_cleaned: "原消息已清理",
  message_changed: "原消息已变化",
  memory_unavailable: "记忆已失效或删除",
  memory_changed: "记忆已修改",
  visibility_changed: "记忆可见范围已变化",
  memory_sources_changed: "记忆来源已变化",
  disabled: "本入口画像已关闭",
  entry_kind: "私聊入口不生成画像",
  daily_limit: "今天已经尝试过生成，请在下一个角色本地日期重试",
  insufficient_messages: "首次生成需要最近 14 天至少 50 条消息",
  no_evidence: "没有可用的本入口依据",
  unconfigured: "尚未配置对话模型",
  daily_token_limit: "达到今日 token 上限",
  current_version_changed: "当前已发布版本已变化，结果未发布",
  settings_changed: "画像设置已变化，结果未发布",
  evidence_changed: "依据在生成期间发生变化，结果未发布",
  interrupted: "服务中断，任务未完成",
  unexpected_error: "生成失败，请查看运行状态",
  timeout: "模型调用超时",
  invalid_output: "模型输出格式无效，未发布",
  invalid_sentence: "候选句格式无效",
  too_many_deleted: "检查删除了超过 30% 的候选句",
  too_short: "删句后的正文不足 150 字",
  too_long: "正文超过 500 字",
  superseded: "已被新候选或编辑取代",
  paused: "对话用途已暂停",
  invalid_items: "候选条目列表格式无效",
  invalid_item: "条目类型、名称或含义格式无效",
  invalid_quotes: "每个模型条目需要 1—3 条引语",
  invalid_quote: "引语格式无效或超过 100 字",
  invalid_quote_reference: "引语消息编号不在本入口材料中",
  quote_not_found: "引语不是消息正文或转引原话的连续片段",
  invalid_evidence_reference: "含义引用的记忆依据无效",
  duplicate_item: "条目重复",
  account_identifier: "包含账号标识或长数字号段",
  insufficient_topic_recurrence: "常聊话题缺少跨日期的独立消息正文依据",
  too_many_items: "条目数超过 6 条（包含手写条目）",
  token_budget: "超过画像的估算 token 预算",
  empty: "检查后没有可保留内容",
  administrator_rejected: "管理员拒绝",
};
const reason = (value: string, format = 1) =>
  value === "too_many_deleted" && format === 2
    ? "检查累计删除了超过 50% 的模型条目"
    : reasons[value] || value;
const basisName = (basis: ProfileBasis) =>
  basis.memory_id
    ? `记忆 #${basis.memory_id} · 修订 ${basis.revision}`
    : `消息 #${basis.message_id}`;

export function EntryProfileDialog({
  entry,
  onClose,
  openMemory,
}: {
  entry: { id: string; name: string };
  onClose: () => void;
  openMemory: (id: number) => void;
}) {
  const base = `/entries/${encodeURIComponent(entry.id)}/profile`;
  const snapshot = useData<ProfileSnapshot>(base, 2000);
  const [offset, setOffset] = useState(0);
  const history = useData<{ items: ProfileVersion[]; total: number }>(
    `${base}/versions?limit=10&offset=${offset}`,
  );
  const [selected, setSelected] = useState<number | null>(null);
  const detailPanel = useRef<HTMLElement>(null);
  const detail = useData<ProfileDetail>(
    selected === null ? null : `${base}/versions/${selected}`,
  );
  const [draft, setDraft] = useState<ProfileDraft | null>(null);
  const currentDetail = useData<ProfileDetail>(
    snapshot.data?.current?.format_version === 2
      ? `${base}/versions/${snapshot.data.current.version}`
      : null,
  );
  const [settings, setSettings] = useState<{
    enabled: boolean;
    publish_mode: string;
    expected_version: number;
  } | null>(null);
  const [before, setBefore] = useState("1"),
    [after, setAfter] = useState("2");
  const [comparison, setComparison] = useState<string | null>(null);
  const diff = useData<ProfileDiff>(comparison);
  const [rejection, setRejection] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const submitting = useRef(false),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const value = snapshot.data;
  // Refresh history/details when a background task or another administrator finishes.
  const currentVersion = value?.settings.current_version;
  const pendingVersion = value?.pending?.version;
  const taskState = value?.latest_attempt?.state;
  useEffect(() => {
    history.refresh();
    detail.refresh();
  }, [
    currentVersion,
    pendingVersion,
    taskState,
    history.refresh,
    detail.refresh,
  ]);
  const refresh = () => {
    snapshot.refresh();
    history.refresh();
    detail.refresh();
  };
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
      await api(base + path, json(method, body));
      if (alive.current) {
        setNotice(message);
        refresh();
      }
      return true;
    } catch (e) {
      if (alive.current) {
        setError(errorText(e));
        refresh();
      }
      return false;
    } finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const view = (version: number) => {
    setSelected(version);
    requestAnimationFrame(() =>
      detailPanel.current?.scrollIntoView?.({
        behavior: "smooth",
        block: "start",
      }),
    );
    setRejection("");
    setError("");
  };
  const expected = { expected_version: value?.settings.current_version || 0 };
  const currentReady = currentDetail.data?.version === value?.current?.version;
  const working = value?.latest_attempt?.state === "running";
  const budget = profileDraftBudget(
    draft || { expected_version: 0, content: "" },
  );
  const limits = value?.edit_limits || {
    max_items: 6,
    max_chars: 500,
    max_tokens: 500,
  };
  const size = budget.characters;
  const draftInvalid =
    !draft ||
    size > limits.max_chars ||
    (draft.items
      ? !draft.items.length ||
        draft.items.length > limits.max_items ||
        budget.tokens > limits.max_tokens ||
        draft.items.some((item) =>
          item.kind
            ? !item.label.trim() ||
              item.quotes.some(
                (q) =>
                  !Number.isSafeInteger(q.message_id) ||
                  q.message_id < 1 ||
                  !q.text.trim(),
              )
            : !item.text.trim(),
        )
      : size < 150);
  return (
    <Dialog
      title={`入口画像 · ${entry.name}`}
      onClose={onClose}
      closeDisabled={busy}
    >
      <div className="entry-profile">
        <p className="muted">
          本入口的交流背景。只用于本入口回复准备，停用后保留历史。
        </p>
        {(snapshot.error || error) && (
          <Notice error>
            {error || snapshot.error}
            <button className="text-button" onClick={refresh}>
              刷新画像
            </button>
          </Notice>
        )}
        {notice && <Notice>{notice}</Notice>}
        {!value ? (
          <p role="status">正在读取入口画像…</p>
        ) : !value.supported ? (
          <Notice>私聊入口不生成入口画像。</Notice>
        ) : (
          <>
            <section className="entry-profile-section" aria-label="画像设置">
              <div className="button-row">
                <Badge>{value.settings.enabled ? "已开启" : "已关闭"}</Badge>
                <span>
                  {value.settings.publish_mode === "check_auto"
                    ? "检查通过自动发布"
                    : "全部人工确认"}
                </span>
                <Badge tone={value.pending_count ? "amber" : ""}>
                  待确认 {value.pending_count}
                </Badge>
              </div>
              {!value.global_enabled && (
                <Notice>
                  全局画像开关已关闭，当前不会生成或加入回复准备。
                </Notice>
              )}
              {settings ? (
                <form
                  onSubmit={async (e) => {
                    e.preventDefault();
                    if (
                      await change(
                        "/settings",
                        "PATCH",
                        settings,
                        "画像设置已保存",
                      )
                    )
                      setSettings(null);
                  }}
                >
                  <label className="check-label">
                    <input
                      type="checkbox"
                      checked={settings.enabled}
                      disabled={busy}
                      onChange={(e) =>
                        setSettings({ ...settings, enabled: e.target.checked })
                      }
                    />
                    启用本入口画像
                  </label>
                  <label>
                    发布方式
                    <select
                      aria-label="发布方式"
                      value={settings.publish_mode}
                      disabled={busy}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          publish_mode: e.target.value,
                        })
                      }
                    >
                      <option value="check_auto">检查通过自动发布</option>
                      <option value="all_manual">全部人工确认</option>
                    </select>
                  </label>
                  <p className="muted">
                    发布方式只影响之后的候选；切换不会发布现有待确认版本。
                  </p>
                  <div className="button-row">
                    <button disabled={busy}>保存画像设置</button>
                    <button
                      className="secondary"
                      type="button"
                      disabled={busy}
                      onClick={() => setSettings(null)}
                    >
                      取消设置
                    </button>
                  </div>
                </form>
              ) : (
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    setSettings({
                      enabled: value.settings.enabled,
                      publish_mode: value.settings.publish_mode,
                      ...expected,
                    })
                  }
                >
                  修改画像设置
                </button>
              )}
            </section>
            <section className="entry-profile-section" aria-label="当前画像">
              <h3>
                {value.current
                  ? `当前已发布 · v${value.current.version}`
                  : "暂无已发布画像"}
              </h3>
              {value.current && (
                <>
                  <p className="muted">
                    生成于 {fullTime(value.current.generated_at)} ·{" "}
                    {source(value.current.source)} · 变化
                    {degree(value.current.change_degree)}
                  </p>
                  {value.current.format_version === 2 ? (
                    currentDetail.data ? (
                      currentDetail.data.sentences.map((item, i) => (
                        <div key={i}>
                          <ProfileItemView value={item} />
                          <Badge>
                            {item.admin_written
                              ? "管理员手写 · 受保护"
                              : "模型生成"}
                          </Badge>
                        </div>
                      ))
                    ) : (
                      <p>{currentDetail.error || "正在读取条目…"}</p>
                    )
                  ) : (
                    <>
                      <Badge>旧版摘要</Badge>
                      <p className="entry-profile-content">
                        {value.current.content}
                      </p>
                    </>
                  )}
                </>
              )}
              {value.possibly_stale && (
                <Notice>
                  可能过时 · {value.stale_basis.length} 条依据已变化
                  <ul>
                    {value.stale_basis.map((b, i) => (
                      <li key={i}>
                        {basisName(b)}：{reason(b.reason || "")}
                      </li>
                    ))}
                  </ul>
                </Notice>
              )}
              <div className="button-row">
                {value.current && (
                  <button
                    className="secondary"
                    onClick={() => view(value.current!.version)}
                  >
                    查看当前依据与检查
                  </button>
                )}
                <button
                  className="secondary"
                  disabled={
                    busy ||
                    !!draft ||
                    (value.current?.format_version === 2 && !currentReady)
                  }
                  onClick={() => {
                    setDraft({
                      content: value.current?.content || "",
                      ...(value.current?.format_version === 2 &&
                      currentDetail.data &&
                      currentReady
                        ? { items: currentDetail.data.sentences.map(editItem) }
                        : !value.current
                          ? { items: [] }
                          : {}),
                      ...expected,
                    });
                    setError("");
                  }}
                >
                  编辑正文
                </button>
                <button
                  disabled={
                    busy ||
                    working ||
                    !value.settings.enabled ||
                    !value.global_enabled
                  }
                  onClick={() =>
                    void change(
                      "/regenerate",
                      "POST",
                      expected,
                      "已提交后台生成，可关闭面板后再查看结果。",
                    )
                  }
                >
                  重新生成
                </button>
              </div>
              <p className="muted">
                首次生成需要近 14 天至少 50
                条消息，每个角色本地日期最多尝试一次。生成与检查各最多 120 秒。
              </p>
              {value.latest_attempt && (
                <div role="status" className="entry-profile-task">
                  <strong>
                    {working
                      ? value.latest_attempt.stage === "checking"
                        ? "正在检查"
                        : "正在生成"
                      : status(
                          value.latest_attempt.version_status ||
                            value.latest_attempt.state,
                        )}
                  </strong>
                  {value.latest_attempt.version_status &&
                    value.latest_attempt.version_status !==
                      value.latest_attempt.state && (
                      <p className="muted">
                        生成时结果：{status(value.latest_attempt.state)}
                        ；候选已处理。
                      </p>
                    )}
                  {value.latest_attempt.reason && (
                    <p>{reason(value.latest_attempt.reason)}</p>
                  )}
                  {value.latest_attempt.version && (
                    <button
                      className="text-button"
                      onClick={() => view(value.latest_attempt!.version!)}
                    >
                      查看任务版本 v{value.latest_attempt.version}
                    </button>
                  )}
                </div>
              )}
            </section>
            {draft && (
              <form
                className="entry-profile-section"
                onSubmit={async (e) => {
                  e.preventDefault();
                  if (
                    await change(
                      "",
                      "PUT",
                      draft.items
                        ? {
                            expected_version: draft.expected_version,
                            items: draft.items.map(itemRequest),
                          }
                        : draft,
                      "编辑已发布，新写句子将按管理员手写保护。",
                    )
                  )
                    setDraft(null);
                }}
              >
                <h3>编辑并直接发布</h3>
                <p>
                  新增或改写的句子标为管理员手写，后续模型生成会原样保留；未改动句子保留原依据。
                </p>
                {draft.items ? (
                  <StructuredProfileEditor
                    items={draft.items}
                    disabled={busy}
                    maxItems={limits.max_items}
                    onChange={(items) => setDraft({ ...draft, items })}
                  />
                ) : (
                  <label>
                    画像正文
                    <textarea
                      rows={8}
                      value={draft.content}
                      disabled={busy}
                      onChange={(e) =>
                        setDraft({ ...draft, content: e.target.value })
                      }
                    />
                  </label>
                )}
                <p>
                  {draft.items
                    ? `${draft.items.length} / ${limits.max_items} 条 · ${size} / ${limits.max_chars} 字 · ${budget.tokens} / ${limits.max_tokens} 估算 token（含分类、引语和标点，无最低字数）`
                    : `${size} 字 / 150—500 字（旧版摘要）`}{" "}
                  · 编辑基于 v{draft.expected_version}
                </p>
                {draft.expected_version !== value.settings.current_version && (
                  <Notice>
                    当前版本已变化。草稿已保留，请先复制草稿、取消编辑并核对最新版本。
                  </Notice>
                )}
                <div className="button-row">
                  <button
                    disabled={
                      busy ||
                      draftInvalid ||
                      draft.expected_version !== value.settings.current_version
                    }
                  >
                    发布编辑
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => setDraft(null)}
                  >
                    取消编辑
                  </button>
                </div>
              </form>
            )}
            {value.pending && (
              <section className="entry-profile-section">
                <h3>待确认候选 · v{value.pending.version}</h3>
                <p className="entry-profile-content">{value.pending.content}</p>
                <button onClick={() => view(value.pending!.version)}>
                  审阅待确认 v{value.pending.version}
                </button>
              </section>
            )}
            {selected !== null && (
              <section
                ref={detailPanel}
                className="entry-profile-section"
                aria-label="版本详情"
              >
                <h3>版本 v{selected} 的依据与检查</h3>
                {detail.error ? (
                  <Notice error>
                    {detail.error}
                    <button onClick={detail.refresh}>重试版本详情</button>
                  </Notice>
                ) : !detail.data ? (
                  <p>正在读取版本…</p>
                ) : (
                  <>
                    <ProfileDetails
                      value={detail.data}
                      openMemory={(id) => {
                        if (!busy) {
                          onClose();
                          openMemory(id);
                        }
                      }}
                    />
                    {detail.data.status === "candidate" && (
                      <>
                        <p>确认前请核对上述逐句依据和检查结果。</p>
                        <div className="button-row">
                          <button
                            disabled={busy}
                            onClick={() =>
                              void change(
                                `/versions/${selected}/confirm`,
                                "POST",
                                expected,
                                "候选已确认发布",
                              )
                            }
                          >
                            确认发布 v{selected}
                          </button>
                        </div>
                        <label>
                          拒绝原因
                          <input
                            maxLength={1000}
                            value={rejection}
                            onChange={(e) => setRejection(e.target.value)}
                          />
                        </label>
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() =>
                            void change(
                              `/versions/${selected}/reject`,
                              "POST",
                              {
                                ...expected,
                                reason: rejection.trim() || "管理员拒绝",
                              },
                              "候选已拒绝，当前画像保留",
                            )
                          }
                        >
                          拒绝候选 v{selected}
                        </button>
                      </>
                    )}
                    {detail.data.status === "published" && (
                      <>
                        <p className="muted">
                          回滚会复制为新的当前版本，原历史保留；过时依据仍会标出。
                        </p>
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() =>
                            void change(
                              `/versions/${selected}/rollback`,
                              "POST",
                              expected,
                              "已复制为新版本并发布",
                            )
                          }
                        >
                          将 v{selected} 复制为新版本
                        </button>
                      </>
                    )}
                  </>
                )}
              </section>
            )}
            <section className="entry-profile-section" aria-label="版本历史">
              <h3>版本历史</h3>
              {history.error && (
                <Notice error>
                  {history.error}
                  <button onClick={history.refresh}>重试历史</button>
                </Notice>
              )}
              {!history.data ? (
                <p>正在读取历史…</p>
              ) : (
                <>
                  <ul className="entry-profile-history">
                    {history.data.items.map((v) => (
                      <li key={v.version}>
                        <div>
                          <strong>v{v.version}</strong> ·{" "}
                          {v.is_current ? "当前" : status(v.status)} ·{" "}
                          {source(v.source)} · 变化{degree(v.change_degree)}
                          <p className="muted">{fullTime(v.generated_at)}</p>
                        </div>
                        <button
                          className="secondary"
                          onClick={() => view(v.version)}
                        >
                          查看 v{v.version}
                        </button>
                      </li>
                    ))}
                  </ul>
                  {!history.data.items.length && <p>还没有版本记录。</p>}
                  <Pagination
                    total={history.data.total}
                    offset={offset}
                    limit={10}
                    change={setOffset}
                  />
                </>
              )}
              <form
                className="entry-profile-compare"
                onSubmit={(e) => {
                  e.preventDefault();
                  setComparison(
                    `${base}/diff?before_version=${Number(before)}&after_version=${Number(after)}`,
                  );
                  diff.refresh();
                }}
              >
                <label>
                  对比起始版本
                  <input
                    type="number"
                    required
                    min={1}
                    max={Number.MAX_SAFE_INTEGER}
                    value={before}
                    onChange={(e) => setBefore(e.target.value)}
                  />
                </label>
                <label>
                  对比目标版本
                  <input
                    type="number"
                    required
                    min={1}
                    max={Number.MAX_SAFE_INTEGER}
                    value={after}
                    onChange={(e) => setAfter(e.target.value)}
                  />
                </label>
                <button>逐句对比</button>
              </form>
              {diff.error && <Notice error>{diff.error}</Notice>}
              {diff.data && (
                <div aria-label="逐句差异">
                  {!diff.data.changed && <p>两版内容与依据相同。</p>}
                  {diff.data.sentences.map((d, i) => (
                    <div className={`entry-profile-diff ${d.op}`} key={i}>
                      <strong>
                        {{
                          equal:
                            JSON.stringify(d.before) === JSON.stringify(d.after)
                              ? "保留"
                              : "依据或手写标记变化",
                          replace: "改写",
                          insert: "新增",
                          delete: "删除",
                        }[d.op] || d.op}
                      </strong>
                      {d.op === "equal" &&
                      JSON.stringify(d.before) === JSON.stringify(d.after) ? (
                        d.after.map((s, j) => (
                          <DiffSentence key={j} value={s} />
                        ))
                      ) : (
                        <div className="entry-profile-diff-columns">
                          <div>
                            <h4>起始版本</h4>
                            {d.before.map((s, j) => (
                              <DiffSentence key={j} value={s} />
                            ))}
                          </div>
                          <div>
                            <h4>目标版本</h4>
                            {d.after.map((s, j) => (
                              <DiffSentence key={j} value={s} />
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </Dialog>
  );
}

function ProfileDetails({
  value,
  openMemory,
}: {
  value: ProfileDetail;
  openMemory: (id: number) => void;
}) {
  const checks = value.checks;
  const model = checks.model;
  const format = checks.format_version || 1;
  // Rejected versions retain invalid model JSON for audit; it is not a typed result.
  const validModel =
    checks.reason !== "invalid_output" &&
    model &&
    typeof model.reason === "string" &&
    Array.isArray(model.sentences) &&
    model.sentences.length === checks.checked_sentences?.length &&
    model.sentences.every(
      (row, index) =>
        row &&
        typeof row === "object" &&
        row.index === index + 1 &&
        typeof row.supported === "boolean" &&
        typeof row.scene_qualified === "boolean" &&
        typeof row.reason === "string" &&
        Array.isArray(row.violations) &&
        row.violations.every((v) => typeof v === "string"),
    );
  return (
    <>
      <p>
        {status(value.status)} · {source(value.source)} · 变化
        {degree(value.change_degree)} · {fullTime(value.generated_at)}
      </p>
      {format === 1 && <Badge>旧版摘要</Badge>}
      {value.possibly_stale && <Notice>此版本依据可能过时。</Notice>}
      <ol className="entry-profile-sentences">
        {value.sentences.map((sentence, index) => (
          <li key={index}>
            <ProfileItemView value={sentence} />
            <Badge>
              {sentence.admin_written ? "管理员手写 · 受保护" : "模型生成"}
            </Badge>
            {sentence.admin_written && !sentence.basis.length && (
              <p className="muted">管理员直接提供，无模型依据。</p>
            )}
            {sentence.basis.map((basis, i) => (
              <div className="entry-profile-evidence" key={i}>
                {basis.memory_id ? (
                  <button
                    className="text-button"
                    onClick={() => openMemory(basis.memory_id!)}
                  >
                    {basisName(basis)}
                  </button>
                ) : (
                  <strong>{basisName(basis)}</strong>
                )}
                {basis.evidence ? (
                  <>
                    <p>{basis.evidence.content}</p>
                    {basis.evidence.occurred_at && (
                      <small>{fullTime(basis.evidence.occurred_at)}</small>
                    )}
                    {basis.evidence.source_messages?.map((m) => (
                      <blockquote key={m.message_id}>
                        {m.content}
                        <footer>
                          本入口原消息 #{m.message_id} ·{" "}
                          {fullTime(m.occurred_at)}
                        </footer>
                      </blockquote>
                    ))}
                  </>
                ) : (
                  <p>此版本未保存来源摘录，可按依据编号核对历史。</p>
                )}
                {basis.memory && (
                  <small>
                    当前修订 {basis.memory.revision} ·{" "}
                    {{ active: "有效", forgotten: "已遗忘", deleted: "已删除" }[
                      basis.memory.lifecycle
                    ] || basis.memory.lifecycle}
                  </small>
                )}
              </div>
            ))}
          </li>
        ))}
      </ol>
      <h4>检查结果：{checks.passed ? "通过" : "未通过"}</h4>
      {checks.reason && <p>{reason(checks.reason, format)}</p>}
      {checks.quote_validation && (
        <p>
          引用原话核对：{checks.quote_validation.passed} /{" "}
          {checks.quote_validation.total}{" "}
          条通过（含被删条目的引语；不代表语义支持）
        </p>
      )}
      {!!checks.quote_validation_items?.length && (
        <details>
          <summary>逐条引用核对</summary>
          <ul>
            {checks.quote_validation_items.map((item) => (
              <li key={item.index}>
                候选第 {item.index} 项：{item.passed} / {item.total} 条原话通过
                {item.quotes
                  .filter((quote) => !quote.passed)
                  .map((quote) => (
                    <p key={quote.index}>
                      引语 {quote.index}：
                      {reason(quote.reason || "invalid_quote", format)}
                    </p>
                  ))}
              </li>
            ))}
          </ul>
        </details>
      )}
      {checks.deterministic?.characters != null && (
        <p>
          正文 {checks.deterministic.characters} 字 ·{" "}
          {checks.deterministic.tokens ?? "未记录"} 估算 token
        </p>
      )}
      <p>
        确定性检查：
        {checks.deterministic?.passed ? "通过" : "存在删句或错误，见下方记录"}
      </p>
      {checks.errors?.length ? (
        <ul>
          {checks.errors.map((e, i) => (
            <li key={i}>{reason(e, format)}</li>
          ))}
        </ul>
      ) : null}
      {validModel && model ? (
        <>
          <p>模型判断：{model.reason}</p>
          <ol>
            {model.sentences.map((s, i) => (
              <li key={s.index}>
                <p>{checks.checked_sentences?.[i]?.text}</p>
                <p>
                  依据{s.supported ? "支持" : "不支持"} · 场景限定
                  {s.scene_qualified ? "通过" : "未通过"} ·{" "}
                  {checks.checked_sentences?.[i]?.meaning &&
                  typeof s.meaning_supported === "boolean"
                    ? `含义${s.meaning_supported ? "有依据" : "无依据，已移除"} · `
                    : ""}
                  {s.violations.length
                    ? s.violations.join("、")
                    : "未发现禁止内容"}
                </p>
                <p>{s.reason}</p>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p>
          {model
            ? "模型检查输出格式无效，本版未通过检查。"
            : checks.admin_edit
              ? "管理员编辑只做确定性检查。"
              : "本版未执行模型检查。"}
        </p>
      )}
      {!!checks.removed_meanings?.length && (
        <>
          <h4>已删含义记录</h4>
          <p>说法本身可以保留；没有含义不表示生成失败。</p>
          <ul>
            {checks.removed_meanings.map((item, i) => (
              <li key={i}>
                候选第 {item.index} 项：{item.meaning}
                <p>{reason(item.reason, format)}</p>
              </li>
            ))}
          </ul>
        </>
      )}
      {!!checks.admin_warnings?.length && (
        <>
          <h4>手写条目检查提醒</h4>
          <ul>
            {checks.admin_warnings.map((item, i) => (
              <li key={i}>
                第 {item.index} 项：{item.result.violations.join("、")} ·{" "}
                {item.result.reason}
              </li>
            ))}
          </ul>
        </>
      )}
      {!!checks.deleted_sentences?.length && (
        <>
          <h4>{format === 2 ? "已删条目记录" : "已删句记录"}</h4>
          <ul>
            {checks.deleted_sentences.map((s, i) => (
              <li key={i}>
                <p>
                  {typeof s.sentence?.text === "string"
                    ? s.sentence.text
                    : `候选第 ${s.index} 项`}
                </p>
                <p>
                  {s.stage === "deterministic"
                    ? "引用与格式核对："
                    : s.stage === "model"
                      ? "模型检查："
                      : ""}
                  {reason(s.reason, format)}
                </p>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function DiffSentence({ value }: { value: ProfileSentence }) {
  return (
    <div>
      <ProfileItemView value={value} />
      <small>
        {value.author === "admin" ? "管理员手写" : "模型生成"}
        {value.basis?.length
          ? ` · ${value.basis.map(basisName).join("；")}`
          : ""}
      </small>
    </div>
  );
}
