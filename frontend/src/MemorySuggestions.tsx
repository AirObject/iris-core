import { useState } from "react";
import { api, json } from "./api";
import { Badge, RoleTime } from "./ui";
import { Excerpts } from "./ConsolidationReport";
import { suggestionKinds, suggestionStatuses } from "./consolidation-labels";
import type { MemoryDetail } from "./types";
import type { MemorySuggestion } from "./consolidation-types";

export function MemorySuggestions({
  detail,
  busy,
  blocked,
  setBusy,
  saved,
  failed,
  openMemory,
  close,
}: {
  detail: MemoryDetail;
  busy: boolean;
  blocked: boolean;
  setBusy: (value: boolean) => void;
  saved: (value: MemoryDetail) => void;
  failed: (error: unknown) => void;
  openMemory: (id: number) => void;
  close: () => void;
}) {
  const [action, setAction] = useState<{
    kind: "confirm" | "clear";
    annotation: MemorySuggestion;
    revision: number;
  } | null>(null);
  const locked = busy || blocked;
  async function submit() {
    if (!action || locked) return;
    setBusy(true);
    try {
      saved(
        await api<MemoryDetail>(
          `/memories/${detail.id}/annotations/${action.annotation.id}${action.kind === "confirm" ? "/confirm" : ""}`,
          json(action.kind === "confirm" ? "POST" : "DELETE", {
            expected_revision: action.revision,
          }),
        ),
      );
      setAction(null);
    } catch (e) {
      failed(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="detail-section" aria-label="整理建议">
      <h3>
        整理建议 <Badge>模型建议 · 仅管理员可见</Badge>
      </h3>
      <p className="fine-print">
        建议可能有误，请结合来源核对。未确认和已确认的建议都不会进入宿主召回。
      </p>
      {!detail.consolidation_annotations?.length && (
        <p className="quiet">暂无整理建议。</p>
      )}
      {detail.consolidation_annotations?.map((a) => (
        <article className="maintenance-item" key={a.id}>
          <div className="detail-badges">
            <Badge tone="warning">{suggestionKinds[a.kind] || a.kind}</Badge>
            <Badge>{suggestionStatuses[a.review_status]}</Badge>
            {a.modified_since_annotation && (
              <Badge tone="warning">标注后已修改</Badge>
            )}
            <span className="muted">建议 #{a.id}</span>
          </div>
          <p className="report-text">{a.text}</p>
          <p className="muted">
            标注时修订 {a.memory_revision} · <RoleTime value={a.created_at} />
          </p>
          {a.confirmed_at && (
            <p>
              管理员已确认 · <RoleTime value={a.confirmed_at} />
            </p>
          )}
          <div className="actions">
            {a.related_memory_ids
              .filter((id) => id !== detail.id)
              .map((id) => (
                <button
                  key={id}
                  className="text-button"
                  disabled={busy}
                  onClick={() => openMemory(id)}
                >
                  相关记忆 #{id}
                </button>
              ))}
            {a.superseded_by != null && (
              <button
                className="text-button"
                disabled={busy}
                onClick={() => openMemory(a.superseded_by!)}
              >
                建议的替代记忆 #{a.superseded_by}
              </button>
            )}
          </div>
          {a.report.reason && <p className="report-text">{a.report.reason}</p>}
          <Excerpts value={a.report.source_excerpts} openMemory={openMemory} />
          <a
            href={`#/status?run=${a.report.run_id}`}
            onClick={(e) => {
              if (busy) e.preventDefault();
              else close();
            }}
          >
            查看整理报告 #{a.report.run_id}
          </a>
          <div className="actions suggestion-actions">
            {a.review_status === "pending" && (
              <button
                disabled={locked || !!action}
                onClick={() =>
                  setAction({
                    kind: "confirm",
                    annotation: a,
                    revision: detail.revision,
                  })
                }
              >
                采纳建议 #{a.id}
              </button>
            )}
            <button
              disabled={locked || !!action}
              onClick={() =>
                setAction({
                  kind: "clear",
                  annotation: a,
                  revision: detail.revision,
                })
              }
            >
              清除建议 #{a.id}
            </button>
          </div>
          {action?.annotation.id === a.id && (
            <div
              className="delete-confirm"
              role="group"
              aria-label="确认建议操作"
            >
              <p>
                {action.kind === "confirm"
                  ? "采纳只记录管理员确认，不改正文和相信程度；建议仍只对管理员可见。"
                  : "清除会撤下这条整理建议并保留操作记录，不改正文和相信程度。"}
              </p>
              <div className="actions">
                <button
                  className="primary"
                  disabled={locked}
                  onClick={() => void submit()}
                >
                  {action.kind === "confirm" ? "确认采纳建议" : "确认清除建议"}
                </button>
                <button disabled={busy} onClick={() => setAction(null)}>
                  取消建议操作
                </button>
              </div>
            </div>
          )}
        </article>
      ))}
    </section>
  );
}
