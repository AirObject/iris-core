import { Badge, Notice, fullTime } from "./ui";
import {
  summaryKinds,
  summaryReason,
  summaryStatuses,
} from "./person-summary-labels";
import type {
  SummaryBasis,
  SummaryCheckItem,
  SummaryDetail,
  SummaryItem,
  SummaryVersion,
} from "./person-summary-types";

function checkedModel(
  value: unknown,
): { reason: string; items: SummaryCheckItem[] } | null {
  if (!value || typeof value !== "object") return null;
  const model = value as Record<string, unknown>;
  if (typeof model.reason !== "string" || !Array.isArray(model.items))
    return null;
  if (
    !model.items.every((item: unknown) => {
      if (!item || typeof item !== "object") return false;
      const row = item as Record<string, unknown>;
      return (
        Number.isInteger(row.index) &&
        typeof row.supported === "boolean" &&
        typeof row.scene_qualified === "boolean" &&
        typeof row.reason === "string" &&
        Array.isArray(row.violations) &&
        row.violations.every((v: unknown) => typeof v === "string")
      );
    })
  )
    return null;
  return model as { reason: string; items: SummaryCheckItem[] };
}
function discardedText(value: unknown) {
  if (!value || typeof value !== "object") return "原条目格式无效或未保存";
  const text = (value as Record<string, unknown>).text;
  return typeof text === "string" ? text : "原条目格式无效或未保存";
}
export function SummaryStale({
  value,
  openMemory,
}: {
  value: SummaryBasis[];
  openMemory: (id: number) => void;
}) {
  if (!value.length) return null;
  return (
    <Notice>
      <strong>可能过时</strong>
      <ul>
        {value.map((item, i) => (
          <li key={i}>
            {item.memory_id != null && (
              <button
                className="text-button"
                onClick={() => openMemory(item.memory_id!)}
              >
                记忆 #{item.memory_id}
              </button>
            )}
            {item.revision != null && `（依据修订 ${item.revision}）`}
            {item.sentence_index != null &&
              `第 ${item.sentence_index + 1} 条：`}
            {summaryReason(item.reason || "")}
          </li>
        ))}
      </ul>
    </Notice>
  );
}
export function SummaryItemView({
  item,
  openMemory,
}: {
  item: SummaryItem;
  openMemory: (id: number) => void;
}) {
  return (
    <div className="summary-item">
      <p>
        {item.kind && <Badge>{summaryKinds[item.kind] || item.kind}</Badge>}{" "}
        <Badge>
          {item.admin_written || item.author === "admin"
            ? "管理员手写"
            : "模型生成"}
        </Badge>
      </p>
      <p className="entry-profile-content">{item.body || item.text}</p>
      {item.time_range && <p>时间范围：{item.time_range}</p>}
      {item.valid_until && <p>有效至：{item.valid_until}</p>}
      {item.expires_at && <p>到期时间：{fullTime(item.expires_at)}</p>}
      <details className="source">
        <summary>记忆依据 · {item.basis.length} 条</summary>
        {!item.basis.length && (
          <p>此条没有模型记忆依据；管理员手写内容由管理员负责核对。</p>
        )}
        {item.basis.map((b, i) => (
          <div className="entry-profile-evidence" key={i}>
            <button
              className="text-button"
              onClick={() => openMemory(b.memory_id!)}
            >
              记忆 #{b.memory_id}
            </button>{" "}
            · 依据修订 {b.revision}
            {b.evidence ? (
              <>
                <p className="entry-profile-content">{b.evidence.content}</p>
                <small>
                  依据快照 ·{" "}
                  {fullTime(b.evidence.event_time || b.evidence.created_at)}
                  {b.evidence.truncated && " · 摘录已截短"}
                </small>
              </>
            ) : (
              <p className="muted">没有保存的依据正文快照。</p>
            )}
            {b.memory ? (
              <details>
                <summary>当前记忆 · 修订 {b.memory.revision}</summary>
                <p>{b.memory.content}</p>
              </details>
            ) : (
              <p className="muted">当前记忆已不可用。</p>
            )}
          </div>
        ))}
      </details>
    </div>
  );
}
export function SummaryVersionMeta({ value }: { value: SummaryVersion }) {
  return (
    <p className="muted">
      v{value.version} · {summaryStatuses[value.status] || value.status} ·{" "}
      {(
        {
          periodic: "定期更新",
          regenerate: "管理员重新生成",
          edit: "管理员编辑",
          rollback: "回滚",
        } as Record<string, string>
      )[value.source] || value.source}{" "}
      · 生成于 {fullTime(value.generated_at)} · 变化程度：
      {({ small: "小", medium: "中", large: "大" } as Record<string, string>)[
        value.change_degree
      ] || value.change_degree}
    </p>
  );
}
export function SummaryDetails({
  value,
  openMemory,
}: {
  value: SummaryDetail;
  openMemory: (id: number) => void;
}) {
  const c = value.checks,
    model = checkedModel(c.model);
  if (value.cleared_at)
    return (
      <>
        <SummaryVersionMeta value={value} />
        <Notice>
          因彻底清除而清空 · {fullTime(value.cleared_at)}。此版本不能回滚。
        </Notice>
      </>
    );
  return (
    <>
      <SummaryVersionMeta value={value} />
      {value.invalidated_reason && (
        <Notice>
          {summaryReason(value.invalidated_reason)}，此版本不可回滚。
        </Notice>
      )}
      <SummaryStale value={value.stale_basis} openMemory={openMemory} />
      {value.sentences.map((item, i) => (
        <SummaryItemView key={i} item={item} openMemory={openMemory} />
      ))}
      {!value.sentences.length && <p>此版本没有摘要条目。</p>}
      <details className="source">
        <summary>检查与删条记录</summary>
        <p>
          检查结果：{c.passed ? "通过" : "未通过"}
          {c.admin_edit && "（管理员编辑，未调用模型检查）"}
        </p>
        {c.reason && <p>{summaryReason(c.reason)}</p>}
        {c.errors?.map((e, i) => (
          <p key={i}>{summaryReason(e)}</p>
        ))}
        {c.deterministic && (
          <p>
            规则检查：{c.deterministic.passed ? "通过" : "未通过"}
            {c.deterministic.characters != null &&
              ` · ${c.deterministic.characters} 字`}
            {c.deterministic.tokens != null &&
              ` · ${c.deterministic.tokens} token`}
          </p>
        )}
        {model ? (
          <>
            <p>模型检查：{model.reason}</p>
            {model.items.map((r, i) => (
              <div className="entry-profile-evidence" key={r.index}>
                <strong>检查条目 {r.index}</strong>
                <p>{c.checked_sentences?.[i]?.text}</p>
                <p>
                  依据支持：{r.supported ? "是" : "否"} · 场景限定：
                  {r.scene_qualified ? "充分" : "不足"}
                </p>
                <p>{r.reason}</p>
                {!!r.violations.length && (
                  <p>问题：{r.violations.join("、")}</p>
                )}
              </div>
            ))}
          </>
        ) : (
          <p>
            {c.model == null
              ? "没有模型检查记录。"
              : "模型检查记录格式无效，无法逐条展示。"}
          </p>
        )}
        {!!c.deleted_sentences?.length && (
          <>
            <h4>已删条目</h4>
            {c.deleted_sentences.map((r, i) => (
              <div key={i}>
                <p>
                  第 {r.index} 条 ·{" "}
                  {r.stage === "references" ? "引用核对" : "模型检查"}
                </p>
                <p>{r.sentence?.text || discardedText(r.item)}</p>
                <p>{summaryReason(r.reason)}</p>
                {!!r.violations?.length && <p>{r.violations.join("、")}</p>}
              </div>
            ))}
          </>
        )}
        {c.admin_warnings?.map((r, i) => (
          <Notice key={i}>
            手写条目 {r.index} 的检查提醒：{r.reason} ·{" "}
            {r.violations.join("、")}
          </Notice>
        ))}
        {c.rollback_from != null && <p>从 v{c.rollback_from} 回滚新建。</p>}
      </details>
    </>
  );
}
