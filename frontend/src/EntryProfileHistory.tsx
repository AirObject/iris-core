import { useRef, useState } from "react";
import { useData } from "./api";
import { Badge, Notice, Pagination, fullTime } from "./ui";
import { ProfileItemView } from "./EntryProfileItems";
import {
  degree,
  source,
  status,
  reason,
  basisName,
} from "./entry-profile-labels";
import type {
  ProfileVersion,
  ProfileDetail,
  ProfileDiff,
  ProfileSentence,
} from "./entry-profile-types";

export function EntryProfileHistory({
  base,
  openMemory,
}: {
  base: string;
  openMemory: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  const history = useData<{ items: ProfileVersion[]; total: number }>(
    `${base}/versions?limit=10&offset=${offset}`,
  );
  const [selected, setSelected] = useState<number | null>(null);
  const detailPanel = useRef<HTMLElement>(null);
  const detail = useData<ProfileDetail>(
    selected === null ? null : `${base}/versions/${selected}`,
  );
  const [before, setBefore] = useState("1"),
    [after, setAfter] = useState("2");
  const [comparison, setComparison] = useState<string | null>(null);
  const diff = useData<ProfileDiff>(comparison);
  const view = (version: number) => {
    setSelected(version);
    requestAnimationFrame(() =>
      detailPanel.current?.scrollIntoView?.({
        behavior: "smooth",
        block: "start",
      }),
    );
  };
  return (
    <>
      <section className="entry-profile-section" aria-label="历史（旧形式）">
        <h3>历史（旧形式）</h3>
        <p className="muted">
          旧版本只读，保留原有依据与检查记录；不能回滚，也不再确认或拒绝候选。这里的版本号与宿主投影版本无关。
        </p>
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
                    <strong>v{v.version}</strong> · {status(v.status)} ·{" "}
                    {source(v.source)} · 变化{degree(v.change_degree)}
                    <p className="muted">{fullTime(v.generated_at)}</p>
                  </div>
                  <button className="secondary" onClick={() => view(v.version)}>
                    查看旧版 v{v.version}
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
                  d.after.map((s, j) => <DiffSentence key={j} value={s} />)
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

      {selected !== null && (
        <section
          ref={detailPanel}
          className="entry-profile-section"
          aria-label="旧版详情"
        >
          <h3>旧版 v{selected} 的依据与检查</h3>
          <Badge>只读历史</Badge>
          {detail.error ? (
            <Notice error>
              {detail.error}
              <button onClick={detail.refresh}>重试版本详情</button>
            </Notice>
          ) : !detail.data ? (
            <p>正在读取版本…</p>
          ) : (
            <ProfileDetails value={detail.data} openMemory={openMemory} />
          )}
        </section>
      )}
    </>
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
      {value.redacted && (
        <Notice>
          此旧版本的相关内容已彻底清除。
          {value.redaction_reason && `原因：${value.redaction_reason}`}
        </Notice>
      )}
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
