import { Badge, fullTime } from "./ui";
import type { TrialDiagnostic } from "./types";

export function TrialDiagnosticView({
  value,
  openMemory,
}: {
  value: TrialDiagnostic;
  openMemory: (id: number) => void;
}) {
  return (
    <div className="trial-diagnostic" aria-label="召回候选与判断分">
      <p className="fine-print">
        触发消息 #{value.message_id} · 准备于 {fullTime(value.prepared_at)}
        <br />
        召回记录 {value.recall_id}
      </p>
      {!value.candidates.length && <p>本次没有额外召回候选。</p>}
      {value.candidates.map((item) => (
        <div key={item.id} className="entry-profile-evidence">
          <button className="text-button" onClick={() => openMemory(item.id)}>
            记忆 #{item.id} · 当时修订 {item.revision}
          </button>
          <p>
            {item.reason === "person_highlight"
              ? "人物要点（不参与判断）"
              : item.support == null
                ? "判断分：未取得"
                : `判断分：${item.support} / 100`}{" "}
            <Badge>
              {(
                {
                  retained: "保留",
                  removed: "已去掉",
                  stale: "复核时失效",
                } as Record<string, string>
              )[item.outcome] || item.outcome}
            </Badge>
          </p>
        </div>
      ))}
      <p className="fine-print">
        以上是当次准备的诊断，链接打开记忆的当前详情；查看诊断不会再次召回或增加使用次数。
      </p>
    </div>
  );
}
