import { BulkMemoryDialog } from "./BulkMemory";
import { useState, type FormEvent } from "react";
import { api, ApiError, errorText, json, useData } from "./api";
import {
  Badge,
  Dialog,
  Empty,
  Notice,
  Pagination,
  fullTime,
  lifecycleLabel,
} from "./ui";
import type {
  Alias,
  Message,
  Page,
  PersonDetail as Detail,
  PersonLink,
  PersonSummary,
} from "./types";

export default function People({
  initialQuery = "",
  onChange,
}: {
  initialQuery?: string;
  onChange: () => void;
}) {
  const initial = new URLSearchParams(initialQuery);
  const [selected, setSelected] = useState(initial.get("id"));
  const [mergeNotice, setMergeNotice] = useState("");
  const [filter, setFilter] = useState({
    text: "",
    pending_only: initial.get("pending_only") === "true",
    include_merged: false,
  });
  const [applied, setApplied] = useState(filter);
  const [offset, setOffset] = useState(0);
  const params = new URLSearchParams({
    text: applied.text,
    pending_only: String(applied.pending_only),
    include_merged: String(applied.include_merged),
    limit: "30",
    offset: String(offset),
  });
  const list = useData<Page<PersonSummary>>(
    selected ? null : `/people?${params}`,
    5000,
  );
  const changed = () => {
    list.refresh();
    onChange();
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">名字背后的人</p>
          <h1>人物</h1>
          <p>核对身份与证据，再决定是否为同一个人。</p>
        </div>
      </div>
      {mergeNotice && <Notice>{mergeNotice}</Notice>}
      {selected ? (
        <PersonDetail
          key={selected}
          id={selected}
          openPerson={setSelected}
          onBack={() => setSelected(null)}
          onChange={changed}
          onMerged={(target) => {
            setMergeNotice("已确认联系并完成人物合并，以下为保留人物的资料。");
            setSelected(target);
          }}
        />
      ) : (
        <>
          <form
            className="panel people-filters"
            onSubmit={(e) => {
              e.preventDefault();
              setApplied({ ...filter });
              setOffset(0);
              list.refresh();
            }}
          >
            <label>
              搜索名字或别名
              <input
                maxLength={100}
                value={filter.text}
                onChange={(e) => setFilter({ ...filter, text: e.target.value })}
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={filter.pending_only}
                onChange={(e) =>
                  setFilter({ ...filter, pending_only: e.target.checked })
                }
              />
              只看待确认
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={filter.include_merged}
                onChange={(e) =>
                  setFilter({ ...filter, include_merged: e.target.checked })
                }
              />
              包含已合并人物
            </label>
            <button className="primary">搜索人物</button>
          </form>
          {list.error && (
            <Notice error>
              {list.error}
              <button onClick={list.refresh}>重新加载</button>
            </Notice>
          )}
          {!list.data ? (
            <p role="status">正在读取人物…</p>
          ) : (
            <>
              <p className="muted">共 {list.data.total} 位人物</p>
              {!list.data.items.length && <Empty title="当前范围没有人物" />}
              <div className="entry-grid">
                {list.data.items.map((person) => (
                  <article className="panel person-card" key={person.id}>
                    <h2>{person.name}</h2>
                    <small className="muted">人物标识：{person.id}</small>
                    {!!person.pending_links && (
                      <p>
                        <Badge tone="purple">
                          可能是同一人 · {person.pending_links} 条待确认
                        </Badge>
                      </p>
                    )}
                    {person.merged_into && (
                      <p>
                        <Badge>已合并</Badge>
                      </p>
                    )}
                    <p>
                      别名：
                      {person.aliases.map((a) => a.alias).join("、") || "暂无"}
                    </p>
                    <p>相关记忆 {person.memory_count} 条</p>
                    <button
                      className="text-button"
                      onClick={() => setSelected(person.id)}
                      aria-label={`查看人物 ${person.name}`}
                    >
                      查看人物 →
                    </button>
                  </article>
                ))}
              </div>
              <Pagination
                total={list.data.total}
                offset={offset}
                change={setOffset}
              />
            </>
          )}
        </>
      )}
    </>
  );
}

function Evidence({ messages }: { messages: Message[] }) {
  return (
    <details className="person-evidence" open>
      <summary>证据消息 · {messages.length} 条</summary>
      {!messages.length && <p className="muted">没有可展示的证据消息。</p>}
      {messages.map((message) => (
        <blockquote key={message.id}>
          <small>
            {message.sender_name || message.sender_subject_id} ·{" "}
            {message.entry_name || message.entry_id} · #{message.id} ·{" "}
            {fullTime(message.occurred_at || message.received_at)}
          </small>
          <p>{message.content}</p>
          {message.quote_content && (
            <div className="evidence-quote">
              <small>
                引用
                {message.quote_author_subject_id
                  ? ` · 作者标识 ${message.quote_author_subject_id}`
                  : ""}
              </small>
              <p>{message.quote_content}</p>
            </div>
          )}
        </blockquote>
      ))}
    </details>
  );
}
type Action =
  | { kind: "deny"; link: PersonLink }
  | { kind: "merge"; link: PersonLink }
  | { kind: "alias"; alias: Alias };
function PersonDetail({
  id,
  openPerson,
  onBack,
  onChange,
  onMerged,
}: {
  id: string;
  openPerson: (id: string) => void;
  onBack: () => void;
  onChange: () => void;
  onMerged: (target: string) => void;
}) {
  const snapshot = useData<Detail>(`/people/${encodeURIComponent(id)}`);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [alias, setAlias] = useState("");
  const [action, setAction] = useState<Action | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const person = snapshot.data;
  const locked = busy || stale || snapshot.loading || !!snapshot.error;
  const refresh = () => {
    setAction(null);
    setError("");
    setStale(false);
    snapshot.refresh();
  };
  const fail = (e: unknown) => {
    const conflict = e instanceof ApiError && e.status === 409;
    setStale(conflict);
    setError(
      conflict ? "资料或联系已变化，请刷新人物资料后重新核对。" : errorText(e),
    );
  };
  const succeeded = (message: string) => {
    setAction(null);
    setNotice(message);
    setError("");
    snapshot.refresh();
    onChange();
  };
  async function addAlias(e: FormEvent) {
    e.preventDefault();
    if (!person || locked) return;
    const value = alias.trim();
    if (!value || Array.from(value).length > 100) {
      setError("别名需为 1 至 100 个字符");
      return;
    }
    if (value.toLocaleLowerCase() === person.name.trim().toLocaleLowerCase()) {
      setError("别名不能与人物名字相同");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(
        `/people/${encodeURIComponent(id)}/aliases`,
        json("POST", { alias: value, expected_revision: person.revision }),
      );
      setAlias("");
      succeeded("已添加别名");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function confirmAction() {
    if (!person || !action || action.kind === "merge" || locked) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (action.kind === "deny")
        await api(
          `/people/links/${action.link.id}/deny`,
          json("POST", { expected_revision: action.link.revision }),
        );
      else
        await api(
          `/people/${encodeURIComponent(id)}/aliases/${action.alias.id}`,
          json("DELETE", { expected_revision: person.revision }),
        );
      succeeded(
        action.kind === "deny"
          ? "联系已否认，后续学习不会重新提出这对人物的同一人联系。"
          : "已删除别名，后续学习不会自动加回。必要时可手动添加。",
      );
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="person-detail">
      <div className="button-row">
        <button className="text-button" onClick={onBack}>
          ← 返回人物列表
        </button>
        <button disabled={busy || snapshot.loading} onClick={refresh}>
          刷新人物资料
        </button>
      </div>
      {(snapshot.error || error) && !action && (
        <Notice error>{error || snapshot.error}</Notice>
      )}
      {notice && <Notice>{notice}</Notice>}
      {!person ? (
        <p role="status">正在读取人物资料…</p>
      ) : (
        <>
          {bulkOpen && (
            <BulkMemoryDialog
              scope={{ subject_id: person.id }}
              name={person.name}
              onClose={() => setBulkOpen(false)}
              onChanged={() => {
                snapshot.refresh();
                onChange();
              }}
            />
          )}
          <section className="panel">
            <h2>{person.name}</h2>
            {!person.merged_into && (
              <button
                className="secondary"
                disabled={locked}
                onClick={() => setBulkOpen(true)}
              >
                批量遗忘／删除
              </button>
            )}
            <p className="muted">
              人物标识：{person.id} · 资料修订 {person.revision}
            </p>
            {person.merged_into && (
              <Notice>
                此人物已合并，原标识保留用于追溯。
                <button onClick={() => openPerson(person.canonical_id)}>
                  查看合并后的人物
                </button>
              </Notice>
            )}
            <h3>平台身份</h3>
            {!person.platform_identities.length && (
              <p className="muted">暂无平台身份。</p>
            )}
            {person.platform_identities.map((identity) => (
              <dl
                className="identity-facts"
                key={`${identity.platform}:${identity.account_id}`}
              >
                <div>
                  <dt>平台</dt>
                  <dd>
                    {identity.platform === "iris-trial"
                      ? "试用"
                      : identity.platform}
                  </dd>
                </div>
                <div>
                  <dt>账号</dt>
                  <dd>{identity.account_id}</dd>
                </div>
                <div>
                  <dt>显示名</dt>
                  <dd>{identity.display_name}</dd>
                </div>
              </dl>
            ))}
            <h3>相关记忆 · {person.memory_count} 条</h3>
            <p>
              {Object.entries(person.memory_counts)
                .map(([state, count]) => `${lifecycleLabel(state)} ${count} 条`)
                .join(" · ") || "暂无相关记忆"}
            </p>
            <a
              href={`#/memories?${new URLSearchParams({ person_id: person.canonical_id, lifecycle: "all" })}`}
            >
              查看关于此人的记忆
            </a>
          </section>
          <section className="panel">
            <h3>别名</h3>
            {!person.aliases.length && <p className="muted">暂无别名。</p>}
            <ul className="alias-list">
              {person.aliases.map((a) => (
                <li key={a.id}>
                  <span>{a.alias}</span>
                  {!!a.evidence_message_ids.length && (
                    <small className="muted">
                      证据消息{" "}
                      {a.evidence_message_ids.map((n) => `#${n}`).join("、")}
                    </small>
                  )}
                  {!person.merged_into && (
                    <button
                      disabled={locked}
                      className="text-button danger-text"
                      aria-label={`删除别名 ${a.alias}`}
                      onClick={() => {
                        setError("");
                        setAction({ kind: "alias", alias: a });
                      }}
                    >
                      删除
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {!person.merged_into && (
              <form className="inline-form" onSubmit={addAlias} noValidate>
                <label>
                  新别名
                  <input
                    value={alias}
                    maxLength={100}
                    disabled={locked}
                    onChange={(e) => setAlias(e.target.value)}
                  />
                </label>
                <button disabled={locked} className="secondary">
                  添加别名
                </button>
              </form>
            )}
          </section>
          <section className="panel">
            <h3>可能是同一人</h3>
            {!person.same_as.length && <p className="muted">暂无身份联系。</p>}
            {person.same_as.map((link) => (
              <article className="person-link" key={link.id}>
                <h4>{link.subjects.map((s) => s.name).join(" ↔ ")}</h4>
                <p>
                  <Badge tone={link.status === "possible" ? "purple" : ""}>
                    {link.status === "possible"
                      ? "待确认"
                      : link.status === "denied"
                        ? "已否认"
                        : "已确认"}
                  </Badge>{" "}
                  相信程度 {link.belief} / 100
                </p>
                <div className="button-row">
                  {link.subjects
                    .filter((s) => s.id !== id)
                    .map((s) => (
                      <button
                        key={s.id}
                        className="text-button"
                        onClick={() => openPerson(s.id)}
                      >
                        查看 {s.name} 的资料
                      </button>
                    ))}
                </div>
                <Evidence messages={link.evidence_messages} />
                {link.status === "possible" && !person.merged_into && (
                  <div className="button-row">
                    <button
                      disabled={locked}
                      className="primary"
                      onClick={() => {
                        setError("");
                        setAction({ kind: "merge", link });
                      }}
                    >
                      确认并合并
                    </button>
                    <button
                      disabled={locked}
                      onClick={() => {
                        setError("");
                        setAction({ kind: "deny", link });
                      }}
                    >
                      否认联系
                    </button>
                  </div>
                )}
              </article>
            ))}
          </section>
          <section className="panel">
            <h3>扮演关系</h3>
            {!person.roleplay.length && <p className="muted">暂无扮演关系。</p>}
            {person.roleplay.map((link) => (
              <article className="person-link" key={link.id}>
                <h4>
                  {link.actor.name} 扮演 {link.character.name}
                </h4>
                <p>
                  <Badge>虚构角色</Badge> · 相信程度 {link.belief} / 100
                </p>
                <p>
                  所属场景：{link.worlds.map((w) => w ?? "场景未知").join("、")}
                </p>
                <p className="muted">
                  扮演关系不代表同一人，角色经历不归入现实人物。
                </p>
                <Evidence messages={link.evidence_messages} />
              </article>
            ))}
          </section>
          {action?.kind === "merge" ? (
            <MergeDialog
              link={action.link}
              initialTarget={id}
              onClose={() => setAction(null)}
              onRefresh={refresh}
              onSuccess={(target) => {
                succeeded("");
                onMerged(target);
              }}
            />
          ) : (
            action && (
              <Dialog
                title={action.kind === "deny" ? "否认人物联系" : "删除别名"}
                onClose={() => setAction(null)}
                closeDisabled={busy}
              >
                {action.kind === "deny" ? (
                  <p>
                    否认 {action.link.subjects.map((s) => s.name).join("与")}{" "}
                    是同一人。否认后，后续学习不会改回待确认，也不会再次提出这对人物的同一人联系。
                  </p>
                ) : (
                  <p>
                    删除别名“{action.alias.alias}
                    ”及其别名证据记录，后续学习不会自动加回。管理员仍可手动添加。
                  </p>
                )}
                {error && (
                  <Notice error>
                    {error}
                    {stale && <button onClick={refresh}>刷新人物资料</button>}
                  </Notice>
                )}
                <div className="button-row">
                  <button onClick={() => setAction(null)} disabled={busy}>
                    取消
                  </button>
                  <button
                    className="danger-button"
                    disabled={locked}
                    onClick={confirmAction}
                  >
                    {action.kind === "deny" ? "确认否认" : "确认删除别名"}
                  </button>
                </div>
              </Dialog>
            )
          )}
        </>
      )}
    </section>
  );
}

function MergeDialog({
  link,
  initialTarget,
  onClose,
  onRefresh,
  onSuccess,
}: {
  link: PersonLink;
  initialTarget: string;
  onClose: () => void;
  onRefresh: () => void;
  onSuccess: (target: string) => void;
}) {
  const first = useData<Detail>(
    `/people/${encodeURIComponent(link.subjects[0].id)}`,
  );
  const second = useData<Detail>(
    `/people/${encodeURIComponent(link.subjects[1].id)}`,
  );
  const [target, setTarget] = useState(initialTarget);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false),
    [stale, setStale] = useState(false),
    [error, setError] = useState("");
  const people = first.data && second.data ? [first.data, second.data] : null;
  const kept = people?.find((p) => p.id === target),
    source = people?.find((p) => p.id !== target);
  const unavailable =
    !!first.error ||
    !!second.error ||
    first.loading ||
    second.loading ||
    people?.some((p) => !!p.merged_into);
  async function confirm() {
    if (!kept || !source || busy || stale || unavailable || !acknowledged)
      return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ target_id: string }>(
        `/people/links/${link.id}/confirm`,
        json("POST", {
          target_id: kept.id,
          expected_revision: link.revision,
          expected_source_revision: source.revision,
          expected_target_revision: kept.revision,
        }),
      );
      onSuccess(result.target_id);
    } catch (e) {
      const conflict = e instanceof ApiError && e.status === 409;
      setStale(conflict);
      setError(
        conflict
          ? "资料或联系已变化，请刷新人物资料后重新核对。"
          : errorText(e),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="确认并合并人物" onClose={onClose} closeDisabled={busy}>
      {(error || first.error || second.error) && (
        <Notice error>
          {error || first.error || second.error}
          <button disabled={busy} onClick={onRefresh}>
            刷新人物资料
          </button>
        </Notice>
      )}
      {!people ? (
        <p role="status">正在核对双方资料…</p>
      ) : (
        <>
          {people.some((p) => p.merged_into) && (
            <Notice error>
              人物已合并，请刷新后核对。
              <button onClick={onRefresh}>刷新人物资料</button>
            </Notice>
          )}
          <fieldset
            className="lifecycle-fieldset"
            disabled={busy || stale || !!unavailable}
          >
            <label>
              保留的人物
              <select
                value={target}
                onChange={(e) => {
                  setTarget(e.target.value);
                  setAcknowledged(false);
                }}
              >
                {people.map((p) => (
                  <option value={p.id} key={p.id}>
                    {p.name} · {p.id}
                  </option>
                ))}
              </select>
            </label>
            <div className="purge-confirm">
              <strong>
                将“{source?.name}”合并到“{kept?.name}”
              </strong>
              <p>
                平台身份、有效别名、记忆的涉及人和说话人、人物联系及从属人物会转到保留方。被合并方的名字成为别名，原人物保留为合并占位。
              </p>
              <p>
                记忆正文、修订号与分数保持原值。联系冲突时否认优先。M2
                不提供撤销合并，请先核对双方账号与证据。
              </p>
              <p>
                {source?.name}：{source?.platform_identities.length}{" "}
                个平台身份，{source?.memory_count} 条相关记忆。
              </p>
              <p>
                {kept?.name}：{kept?.platform_identities.length} 个平台身份，
                {kept?.memory_count} 条相关记忆。
              </p>
              {people.map((p) => (
                <div className="merge-identity" key={p.id}>
                  <strong>
                    {p.name} · {p.id}
                  </strong>
                  <p>
                    账号：
                    {p.platform_identities
                      .map((i) => `${i.platform} / ${i.account_id}`)
                      .join("；") || "未绑定平台账号"}
                  </p>
                  <p>
                    别名：{p.aliases.map((a) => a.alias).join("、") || "暂无"}
                  </p>
                </div>
              ))}
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                />
                我已核对身份，理解合并后无法在界面撤销
              </label>
            </div>
          </fieldset>
          <div className="button-row">
            <button disabled={busy} onClick={onClose}>
              取消
            </button>
            <button
              className="danger-button"
              disabled={busy || stale || !!unavailable || !acknowledged}
              onClick={confirm}
            >
              {busy ? "正在合并…" : "确认合并"}
            </button>
          </div>
        </>
      )}
    </Dialog>
  );
}
