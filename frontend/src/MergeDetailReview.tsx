import type { MergeDetails } from "./consolidation-types";
export const mergeReviewLabel = "合并待核对（双方互有细节，不代表事实矛盾）";
export function MergeDetailReview({
  details,
  ids,
  openMemory,
  disabled = false,
}: {
  details?: MergeDetails | null;
  ids: number[];
  openMemory: (id: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="merge-detail-review">
      <p>
        双方各有另一条没有的细节，未自动合并。采纳只确认建议，不执行合并，也不改正文和相信程度。
      </p>
      {(["a", "b"] as const).map((side, i) => (
        <div key={side}>
          {ids[i] != null ? (
            <button
              disabled={disabled}
              className="text-button"
              onClick={() => openMemory(ids[i])}
            >
              记忆 #{ids[i]} 的独有细节
            </button>
          ) : (
            <strong>第 {i + 1} 条记忆的独有细节</strong>
          )}
          {details?.[side]?.length ? (
            <ul>
              {details[side].map((text, j) => (
                <li key={j}>{text}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">未保存独有细节，请核对报告正文。</p>
          )}
        </div>
      ))}
    </div>
  );
}
