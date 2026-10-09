import { useState } from "react";
import { useData } from "./api";
import { Badge, Notice, RoleTime, lifecycleLabel } from "./ui";
import type { MemoryDetail } from "./types";
import type {
  PersonaBasis,
  PersonaSentence,
  PersonaVersion,
  PersonaDiffResult,
} from "./persona-types";
import {
  personaSources,
  personaStatuses,
  personaDegrees,
  personaOrigins,
  personaReason,
} from "./persona-labels";

export type PersonaCatalog = {
  entries: { id: string; name: string; kind: string }[];
};
export type PersonaActionKind = "edit" | "confirm" | "reject" | "rollback";
export type PersonaActions = (
  kind: PersonaActionKind,
  version: PersonaVersion,
) => void;

function Evidence({
  basis,
  catalog,
  openMemory,
}: {
  basis: PersonaBasis;
  catalog: PersonaCatalog | null;
  openMemory: (id: number) => void;
}) {
  // The version freezes its evidence text. Metadata below is explicitly the
  // current memory projection; never substitute it for a missing old revision.
  const detail = useData<MemoryDetail>(
    basis.memory ? `/memories/${basis.memory_id}` : null,
    5000,
  );
  const memory = detail.data;
  const entryIds = [
    ...new Set(
      [
        memory?.entry_id,
        ...(memory?.sources || []).map((s) => s.message?.entry_id),
      ].filter((id): id is string => !!id),
    ),
  ];
  return (
    <article className="persona-evidence">
      <h4>记忆 #{basis.memory_id}</h4>
      <p className="muted">
        记录修订 {basis.revision} ·{" "}
        {basis.memory
          ? `当前修订 ${basis.memory.revision} · ${lifecycleLabel(basis.memory.lifecycle)}`
          : "当前记忆不可用"}
      </p>
      <p className="persona-content">
        {basis.content_at_revision ?? "该修订正文不可用"}
      </p>
      {memory && (
        <>
          <p className="muted">
            当前记忆记录时间：
            <RoleTime value={memory.created_at} />
            ；事件时间：
            <RoleTime value={memory.event_time ?? null} />
          </p>
          <p className="muted">
            当前来源入口类型：
            {entryIds.length
              ? entryIds
                  .map((id) => {
                    const entry = catalog?.entries.find((e) => e.id === id);
                    return entry
                      ? `${({ group: "群聊", private: "私聊" } as Record<string, string>)[entry.kind] || entry.kind} · ${entry.name}`
                      : `入口 ${id}（类型暂不可用）`;
                  })
                  .join("；")
              : memory.sources.some((s) => s.kind === "initial_setting")
                ? "初始设定"
                : "未记录入口"}
          </p>
          <small>
            以上时间和入口来自当前记忆详情，不是该版本生成时的历史快照。
          </small>
        </>
      )}
      {detail.loading && <p>正在读取当前记忆元数据…</p>}
      {detail.error && (
        <Notice error>
          {detail.error}
          <button onClick={detail.refresh}>重试记忆元数据</button>
        </Notice>
      )}
      <button
        className="text-button"
        disabled={!basis.memory}
        onClick={() => openMemory(basis.memory_id)}
      >
        查看记忆 #{basis.memory_id}
      </button>
    </article>
  );
}
function Sentence({
  sentence,
  index,
  catalog,
  openMemory,
}: {
  sentence: PersonaSentence;
  index: number;
  catalog: PersonaCatalog | null;
  openMemory: (id: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const handwritten = sentence.admin_written || sentence.origin === "admin";
  return (
    <li className="persona-sentence">
      <div className="persona-sentence-line">
        <span className="muted">{index + 1}.</span>
        <p>{sentence.text}</p>
        <Badge tone={handwritten ? "warning" : ""}>
          {handwritten
            ? "手写"
            : personaOrigins[sentence.origin] || sentence.origin}
        </Badge>
      </div>
      <button
        className="text-button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? "收起" : "展开"}第 {index + 1} 句依据
      </button>
      {expanded && (
        <div className="persona-basis">
          <p>
            该句的依据日期：
            {sentence.dates.length ? sentence.dates.join("、") : "未记录日期"}（
            {sentence.date_count} 个日期）
            {sentence.initial_setting && " · 含初始设定"}
          </p>
          {sentence.basis.map((b) => (
            <Evidence
              key={`${b.memory_id}-${b.revision}`}
              basis={b}
              catalog={catalog}
              openMemory={openMemory}
            />
          ))}
          {!sentence.basis.length && (
            <p className="muted">
              {handwritten
                ? "管理员手写内容，没有记忆依据；手写标记不代表模型检查通过。"
                : "初始模板，没有逐句记忆依据。"}
            </p>
          )}
        </div>
      )}
    </li>
  );
}
const record = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
function Verdict({ value, index }: { value: unknown; index: number }) {
  const verdict = record(value);
  if (!verdict) return <li>第 {index + 1} 句：检查结果格式不完整</li>;
  const verdictLabel = (v: unknown, yes: string, no: string) =>
    typeof v === "boolean" ? (v ? yes : no) : "未报告";
  return (
    <li>
      第 {typeof verdict.index === "number" ? verdict.index : index + 1}{" "}
      句：依据支持 {verdictLabel(verdict.supported, "是", "否")}；虚构{" "}
      {verdictLabel(verdict.fabricated, "是", "否")}；场景限定{" "}
      {verdictLabel(verdict.scene_qualified, "是", "否")}
      {typeof verdict.reason === "string" && <p>{verdict.reason}</p>}
      {Array.isArray(verdict.violations) &&
        verdict.violations.some((v) => typeof v === "string") && (
          <p>
            违反要求：
            {verdict.violations
              .filter((v): v is string => typeof v === "string")
              .join("；")}
          </p>
        )}
    </li>
  );
}
export function PersonaChecks({ version }: { version: PersonaVersion }) {
  const { checks } = version;
  const model = record(checks.model);
  return (
    <div className="persona-checks">
      <h3>检查结果</h3>
      <p>
        {checks.passed === true
          ? "综合检查通过"
          : checks.passed === false
            ? "综合检查未通过"
            : "没有综合检查结论"}
        {checks.administrator_published && " · 管理员直接发布"}
      </p>
      <p className="muted">手写标记表示内容的来源，不代表模型检查通过。</p>
      <h4>确定性检查</h4>
      <p>
        {checks.deterministic?.passed === true
          ? "通过"
          : checks.deterministic?.passed === false
            ? "未通过"
            : "没有确定性检查记录"}
      </p>
      {!!checks.deterministic?.errors?.length && (
        <ul>
          {checks.deterministic.errors.map((e, i) => (
            <li key={i}>{personaReason(e)}</li>
          ))}
        </ul>
      )}
      {!!checks.deterministic?.warnings?.length && (
        <ul className="muted">
          {checks.deterministic.warnings.map((e, i) => (
            <li key={i}>提示：{personaReason(e)}</li>
          ))}
        </ul>
      )}
      <h4>模型检查</h4>
      {checks.model == null ? (
        <p>未进行模型检查</p>
      ) : !model ? (
        <p>模型检查结果格式不完整</p>
      ) : (
        <>
          {typeof model.change_degree === "string" && (
            <p>
              判断的变化程度：
              {personaDegrees[model.change_degree] || model.change_degree}
            </p>
          )}
          {typeof model.reason === "string" && <p>{model.reason}</p>}
          {Array.isArray(model.sentences) ? (
            <ul>
              {model.sentences.map((v, i) => (
                <Verdict key={i} value={v} index={i} />
              ))}
            </ul>
          ) : (
            <p>逐句检查结果格式不完整</p>
          )}
        </>
      )}
      {!!checks.model_errors?.length && (
        <ul>
          {checks.model_errors.map((e, i) => (
            <li key={i}>{personaReason(e)}</li>
          ))}
        </ul>
      )}
      {checks.admin_content_removed_or_changed && (
        <Notice>
          候选删去或改动了手写内容，变化程度按“大”处理。默认发布方式需要确认；“全部自动”模式会直接发布检查通过的候选。
        </Notice>
      )}
    </div>
  );
}
export function PersonaVersionView({
  id,
  title,
  catalog,
  openMemory,
  onAction,
  locked = false,
  currentID,
  history = false,
}: {
  id: number;
  title: string;
  catalog: PersonaCatalog | null;
  openMemory: (id: number) => void;
  onAction?: PersonaActions;
  locked?: boolean;
  currentID?: number;
  history?: boolean;
}) {
  const detail = useData<PersonaVersion>(`/persona/versions/${id}`, 5000);
  const v = detail.data;
  return (
    <section className="panel persona-version" aria-label={title}>
      {v && (
        <h2>
          {title} · v{id}
        </h2>
      )}
      {detail.error && (
        <Notice error>
          {detail.error}
          <button onClick={detail.refresh}>重试版本详情</button>
        </Notice>
      )}
      {!v ? (
        <p>正在读取版本详情…</p>
      ) : (
        <>
          <div className="persona-meta">
            <Badge
              tone={
                v.status === "pending" || v.status === "rejected"
                  ? "warning"
                  : ""
              }
            >
              {personaStatuses[v.status]}
            </Badge>
            <span>{personaSources[v.source] || v.source}</span>
            <span>
              变化程度：
              {v.change_degree
                ? personaDegrees[v.change_degree] || v.change_degree
                : "未记录"}
            </span>
          </div>
          <p className="muted">
            生成时间：
            <RoleTime value={v.generated_at} /> · 发布时间：
            <RoleTime value={v.published_at} />
          </p>
          <p className="muted">
            {v.base_version_id ? `基于 v${v.base_version_id}` : "初始版本"}
            {v.rollback_of && ` · 回滚自 v${v.rollback_of}`}
          </p>
          <ol className="persona-sentences">
            {v.sentences.map((s, i) => (
              <Sentence
                key={`${i}-${s.text}`}
                sentence={s}
                index={i}
                catalog={catalog}
                openMemory={openMemory}
              />
            ))}
          </ol>
          {!v.sentences.length && (
            <p className="persona-content">{v.content}</p>
          )}
          {!!v.rejection_reasons.length && (
            <div className="notice error">
              <h3>拒绝原因</h3>
              <ul>
                {v.rejection_reasons.map((reason, i) => (
                  <li key={i}>{personaReason(reason)}</li>
                ))}
              </ul>
            </div>
          )}
          <PersonaChecks version={v} />
          {v.settings && (
            <details>
              <summary>本次候选接受时的设置</summary>
              <p>
                发布方式：
                {{
                  small_medium_auto: "小或中自动发布",
                  all_auto: "全部自动",
                  all_manual: "全部人工确认",
                }[v.settings.publish_mode] || v.settings.publish_mode}
              </p>
              <h4>生成目标</h4>
              <p className="persona-content">{v.settings.goal}</p>
              <h4>监管要求</h4>
              <p className="persona-content">{v.settings.rules}</p>
            </details>
          )}
          <div className="persona-actions">
            {onAction && currentID && v.status === "pending" && (
              <>
                <button
                  className="primary"
                  disabled={locked || !!detail.error}
                  onClick={() => onAction("confirm", v)}
                >
                  确认发布候选
                </button>
                <button
                  disabled={locked || !!detail.error}
                  onClick={() => onAction("reject", v)}
                >
                  拒绝候选
                </button>
              </>
            )}
            {onAction &&
              currentID &&
              history &&
              ["current", "history"].includes(v.status) && (
                <button
                  disabled={locked || !!detail.error}
                  onClick={() => onAction("rollback", v)}
                >
                  回滚到此版本
                </button>
              )}
            <a href={`#/operations?object_type=persona&object_id=${id}`}>
              查看版本操作记录
            </a>
          </div>
        </>
      )}
    </section>
  );
}
function DiffSentences({
  sentences,
  start,
}: {
  sentences: PersonaSentence[];
  start: number;
}) {
  return sentences.length ? (
    <ol className="persona-diff-sentences" start={start}>
      {sentences.map((s, i) => (
        <li key={i}>
          <p>{s.text}</p>
          <small>
            {personaOrigins[s.origin] || s.origin} · 依据：
            {s.basis.length
              ? s.basis
                  .map((b) => `#${b.memory_id} 修订 ${b.revision}`)
                  .join("、")
              : "无"}{" "}
            · 日期：{s.dates.join("、") || "未记录"}（{s.date_count} 个日期）
            {s.initial_setting && " · 初始设定"}
          </small>
        </li>
      ))}
    </ol>
  ) : (
    <p className="muted">无句子</p>
  );
}
export function PersonaDiff({
  before,
  after,
}: {
  before: number;
  after: number;
}) {
  const data = useData<PersonaDiffResult>(
    `/persona/diff?before_version=${before}&after_version=${after}`,
  );
  return (
    <section
      className="persona-diff"
      aria-label={`版本差异 v${before} 到 v${after}`}
    >
      <h3>
        逐句差异：v{before} → v{after}
      </h3>
      {data.error && (
        <Notice error>
          {data.error}
          <button onClick={data.refresh}>重试版本对比</button>
        </Notice>
      )}
      {!data.data && !data.error && <p>正在读取差异…</p>}
      {data.data?.changes.length === 0 && (
        <p>这两个版本的句子与依据没有差异。</p>
      )}
      {data.data?.changes.map((c, i) => (
        <article className={`persona-change persona-change-${c.kind}`} key={i}>
          <h4>
            {
              {
                insert: "新增句子",
                delete: "删除句子",
                replace: "改写句子",
                basis_changed: "正文相同，依据或归属变化",
              }[c.kind]
            }
          </h4>
          <div className="persona-diff-columns">
            <div>
              <strong>之前 · v{before}</strong>
              <DiffSentences sentences={c.before} start={c.before_start} />
            </div>
            <div>
              <strong>之后 · v{after}</strong>
              <DiffSentences sentences={c.after} start={c.after_start} />
            </div>
          </div>
        </article>
      ))}
    </section>
  );
}
