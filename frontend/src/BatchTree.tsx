import { useState, type CSSProperties } from "react";
import { useData } from "./api";
import { Badge, Notice, batchLabel, time } from "./ui";
import type { Batch } from "./types";

export type LearningBatch = Batch & {
  parent_batch_id?: number | null;
  root_batch_id?: number | null;
  split_path?: string;
  execution_state?: string;
  context_mode?: "full" | "target_only";
  relearning?: boolean;
  can_relearn?: boolean;
  relearn_blocked_reason?: string | null;
  split?: {
    node_count: number;
    leaf_count: number;
    attempt_count: number;
    gap_count: number;
    waiting_messages: number;
    running_messages: number;
    succeeded_messages: number;
    refused_messages: number;
    abandoned_messages: number;
  };
};
type TreeNode = LearningBatch & {
  target_ids: number[];
  target_range: { start: number; end: number; count: number };
  attempts_remaining: number;
  gap: { batch_id: number; reason: string } | null;
};
type TreePage = {
  root_batch_id: number;
  subtree_batch_id: number;
  root_state: string;
  items: TreeNode[];
  total: number;
  limit: number;
  offset: number;
};
type CallHistory = {
  attempts: { number: number; calls: { id: number }[] }[];
  unassigned_calls: { id: number }[];
};
export const isSplitParent = (batch: LearningBatch) =>
  batch.execution_state === "split" || batch.state === "split";
export const contextLabel = (mode?: string) =>
  ({ full: "完整", target_only: "仅目标" })[mode || ""] || "未记录";
export function learningStatus(batch: LearningBatch, targetCount?: number) {
  if (isSplitParent(batch)) {
    if (["waiting", "running"].includes(batch.state)) return "拆小重学中";
    if (batch.state === "succeeded") return "拆小重学已完成";
    if (batch.state === "split") return "历史请求被拒绝 · 已拆分";
    return "拆小重学结束 · 仍有缺口";
  }
  if (batch.parent_batch_id && ["waiting", "running"].includes(batch.state))
    return "拆小重学中";
  // RS terminal refused children are single-message leaves. List summaries
  // omit target counts; tree/detail views also verify their explicit range.
  if (
    batch.state === "refused" &&
    (targetCount === 1 || (targetCount === undefined && batch.parent_batch_id))
  )
    return "该单消息的学习请求被拒绝";
  return batch.relearning ? "重新学习中" : batchLabel(batch);
}
export function callCount(value: CallHistory) {
  return new Set([
    ...value.attempts.flatMap((a) => a.calls.map((c) => c.id)),
    ...value.unassigned_calls.map((c) => c.id),
  ]).size;
}
export function SplitProgress({ batch }: { batch: LearningBatch }) {
  const split = batch.split;
  if (!split) return null;
  const pending = split.waiting_messages + split.running_messages;
  const failed = split.refused_messages + split.abandoned_messages;
  const total = pending + failed + split.succeeded_messages;
  const complete =
    batch.state === "succeeded" &&
    pending === 0 &&
    failed === 0 &&
    split.gap_count === 0;
  return (
    <div className="split-progress">
      <div className="split-metrics" aria-label="子树消息进度">
        <span>
          {batch.parent_batch_id ? "本子树目标" : "原目标"} {total} 条
        </span>
        <span>已成功 {split.succeeded_messages} 条</span>
        <span>仍在补学 {pending} 条</span>
        <span>最终被拒绝 {split.refused_messages} 条</span>
        <span>最终失败 {failed} 条</span>
        <span>剩余缺口 {split.gap_count} 处</span>
      </div>
      <p>批次树节点 {split.node_count} 个</p>
      <p className="muted">
        消息结果仅统计末端子批次；最终失败包含拒绝与尝试用尽。父批次的历史拒绝单独保留。
      </p>
      {complete && <p className="split-complete">全部成功，无缺口</p>}
      {!complete && (
        <p>缺口在对应子批次成功后消失；每处缺口可能包含多条消息。</p>
      )}
    </div>
  );
}
export function BatchLinks({
  batch,
  onOpen,
}: {
  batch: LearningBatch;
  onOpen: (id: number) => void;
}) {
  if (!batch.parent_batch_id) return null;
  return (
    <div className="batch-links">
      {batch.root_batch_id && (
        <button
          className="text-button"
          onClick={() => onOpen(batch.root_batch_id!)}
          aria-label={`查看原始批次 #${batch.root_batch_id}`}
        >
          原始批次 #{batch.root_batch_id} →
        </button>
      )}
      {batch.parent_batch_id !== batch.root_batch_id && (
        <button
          className="text-button"
          onClick={() => onOpen(batch.parent_batch_id!)}
          aria-label={`查看父批次 #${batch.parent_batch_id}`}
        >
          父批次 #{batch.parent_batch_id} →
        </button>
      )}
      <span>路径 {batch.split_path}</span>
    </div>
  );
}
function targetRange(range: TreeNode["target_range"]) {
  return range.count
    ? `第 ${range.start + 1}${range.count > 1 ? `–${range.end}` : ""} 条 · 共 ${range.count} 条`
    : "没有目标消息";
}
export function LeafPosition({ id }: { id: number }) {
  const snapshot = useData<TreePage>(
    `/batches/${id}/tree?limit=1&offset=0`,
    3000,
  );
  const node = snapshot.data?.items.find((item) => item.id === id);
  return (
    <>
      {snapshot.error && (
        <Notice error>
          {snapshot.error}
          <button onClick={snapshot.refresh}>重试读取目标范围</button>
        </Notice>
      )}
      {node && <p>原始目标范围：{targetRange(node.target_range)}</p>}
    </>
  );
}
export function NodeFacts({
  batch,
}: {
  batch: LearningBatch & { attempts_remaining?: number };
}) {
  const parent = isSplitParent(batch);
  return (
    <div className="batch-node-facts">
      <p>上下文模式：{contextLabel(batch.context_mode)}</p>
      {parent ? (
        <p>拆分前已计 {batch.attempt_count ?? 0} 次尝试；执行已交给子批次。</p>
      ) : (
        <p>
          当前额度：已用 {batch.attempt_count ?? 0} / 4，剩余{" "}
          {batch.attempts_remaining ??
            Math.max(0, 4 - (batch.attempt_count ?? 0))}{" "}
          次
        </p>
      )}
      {batch.state === "waiting" && !parent && (
        <p>排队原因未提供；请结合入口和运行状态查看。</p>
      )}
      {!parent && (
        <p>
          下次重试：
          {batch.next_retry_at ? time(batch.next_retry_at) : "无定时时间"}
        </p>
      )}
    </div>
  );
}
function NodeHistory({ id }: { id: number }) {
  const snapshot = useData<CallHistory>(`/batches/${id}`, 3000);
  return (
    <>
      {snapshot.error && (
        <Notice error>
          {snapshot.error}
          <button onClick={snapshot.refresh}>重试读取历史</button>
        </Notice>
      )}
      {!snapshot.data ? (
        <p>正在读取历史…</p>
      ) : (
        <>
          <p>
            历史尝试编号：
            {snapshot.data.attempts.length
              ? snapshot.data.attempts.map((a) => a.number).join("、")
              : "暂无已完成尝试"}
          </p>
          <p>本节点实际模型调用 {callCount(snapshot.data)} 次</p>
          <p className="muted">
            含学习与 JSON 修正请求及未归入尝试的调用；不等于本轮已用额度。
          </p>
        </>
      )}
    </>
  );
}
function NodeHistoryToggle({ id }: { id: number }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="node-history"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>历史尝试与调用 · #{id}</summary>
      {open && <NodeHistory id={id} />}
    </details>
  );
}
export function SplitTree({
  id,
  onOpen,
}: {
  id: number;
  onOpen: (id: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [offset, setOffset] = useState(0);
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const snapshot = useData<TreePage>(
    expanded ? `/batches/${id}/tree?limit=100&offset=${offset}` : null,
    3000,
  );
  const page = snapshot.data;
  return (
    <div className="split-tree">
      <button
        className="secondary"
        aria-expanded={expanded}
        aria-controls={`split-tree-${id}`}
        aria-label={`${expanded ? "收起" : "展开"}拆分树 #${id}`}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? "收起拆分树" : "展开拆分树"}
      </button>
      {expanded && (
        <section id={`split-tree-${id}`} aria-label={`拆分树 #${id}`}>
          <p className="muted">
            范围按原始批次的目标消息顺序，从第 1 条开始；路径中 0 为左支、1
            为右支。节点可展开历史尝试，或进入详情查看消息与调用。
          </p>
          {snapshot.error && (
            <Notice error>
              {snapshot.error}
              <button onClick={snapshot.refresh}>重试读取拆分树</button>
            </Notice>
          )}
          {!page ? (
            <p>正在读取拆分树…</p>
          ) : (
            <>
              <p>
                显示节点 {page.total ? page.offset + 1 : 0}–
                {page.offset + page.items.length} / {page.total}
              </p>
              <ol className="batch-tree-nodes">
                {page.items
                  .filter(
                    (node) =>
                      !collapsed.some(
                        (path) =>
                          node.split_path !== path &&
                          node.split_path?.startsWith(path),
                      ),
                  )
                  .map((node) => {
                    const parent = isSplitParent(node);
                    const path = node.split_path || "";
                    const range = node.target_range;
                    const closed = collapsed.includes(path);
                    return (
                      <li
                        key={node.id}
                        className="batch-tree-node"
                        style={
                          {
                            "--tree-depth": Math.min(path.length, 3),
                          } as CSSProperties
                        }
                      >
                        <div className="section-title">
                          <strong>
                            #{node.id} · {parent ? "拆分父批次" : "子批次"}
                          </strong>
                          <Badge>{learningStatus(node, range.count)}</Badge>
                        </div>
                        <p>{targetRange(range)}</p>
                        <p>路径 {path || "根"}</p>
                        {node.parent_batch_id && (
                          <button
                            className="text-button"
                            aria-label={`查看父批次 #${node.parent_batch_id}`}
                            onClick={() => onOpen(node.parent_batch_id!)}
                          >
                            父批次 #{node.parent_batch_id} →
                          </button>
                        )}
                        <NodeFacts batch={node} />
                        {node.gap && (
                          <p>
                            仍有记忆缺口
                            {["waiting", "running"].includes(node.state)
                              ? " · 拆小重学中"
                              : ""}
                          </p>
                        )}
                        {node.relearn_blocked_reason && !parent && (
                          <p>{node.relearn_blocked_reason}</p>
                        )}
                        <div className="batch-links">
                          {parent && (
                            <button
                              className="text-button"
                              aria-expanded={!closed}
                              aria-label={`${closed ? "展开" : "折叠"}子批次 #${node.id}`}
                              onClick={() =>
                                setCollapsed(
                                  closed
                                    ? collapsed.filter((p) => p !== path)
                                    : [...collapsed, path],
                                )
                              }
                            >
                              {closed ? "展开子批次" : "折叠子批次"}
                            </button>
                          )}
                          {node.id !== id && (
                            <button
                              className="text-button"
                              aria-label={`查看批次 #${node.id}`}
                              onClick={() => onOpen(node.id)}
                            >
                              查看详情
                              {node.can_relearn && !parent
                                ? " / 重新学习"
                                : ""}{" "}
                              →
                            </button>
                          )}
                        </div>
                        <NodeHistoryToggle id={node.id} />
                      </li>
                    );
                  })}
              </ol>
              {page.total > page.limit && (
                <div className="learning-pagination tree-pagination">
                  <button
                    disabled={page.offset === 0}
                    onClick={() => {
                      setOffset(Math.max(0, page.offset - page.limit));
                      setCollapsed([]);
                    }}
                  >
                    上一页节点
                  </button>
                  <button
                    disabled={page.offset + page.items.length >= page.total}
                    onClick={() => {
                      setOffset(page.offset + page.limit);
                      setCollapsed([]);
                    }}
                  >
                    下一页节点
                  </button>
                </div>
              )}
              <p className="muted">
                分页统计的是批次树节点；实际模型调用次数在各节点历史中单独显示。
              </p>
            </>
          )}
        </section>
      )}
    </div>
  );
}
