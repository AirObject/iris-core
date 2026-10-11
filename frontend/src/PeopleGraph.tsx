import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  edgeKey,
  edgeLabel,
  graphFrame,
  graphLabels,
  graphLayout,
} from "./peopleGraphLayout";
import { useData } from "./api";
import { Badge, Dialog, Empty, Notice, Pagination } from "./ui";
import type { Entry, Person } from "./types";

export type GraphNode = {
  id: string;
  name: string;
  kind: string;
  aliases: string[];
  account_count: number;
  memory_count: number;
};
type GraphLink = {
  id: number;
  kind: "same_as" | "roleplay";
  source: string;
  target: string;
  belief: number;
  worlds: (string | null)[];
  evidence_message_count: number;
};
export type GraphEdge = {
  source: string;
  target: string;
  memory_count: number;
  relationship_count: number;
  relationship_summaries: { id: number; summary: string }[];
  links: GraphLink[];
};
export type GraphData = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  total_nodes: number;
  total_edges: number;
  truncated: boolean;
  node_limit: number;
  edge_limit: number;
};
type Filters = {
  entry_id: string;
  person_id: string;
  hops: string;
  min_memories: string;
  node_limit: string;
  edge_limit: string;
};
type Evidence = {
  items: {
    id: number;
    content: string;
    kind: string;
    revision: number;
    cooccurrence: boolean;
    link_evidence: boolean;
  }[];
  total: number;
  links: GraphLink[];
};
const short = (text: string, size: number) =>
  Array.from(text).length > size
    ? Array.from(text).slice(0, size).join("") + "…"
    : text;
const memoryUrl = (id: string) =>
  `#/memories?person_id=${encodeURIComponent(id)}&lifecycle=active`;
const params = (values: Record<string, string>) =>
  new URLSearchParams(
    Object.entries(values).filter(([, value]) => value),
  ).toString();

function EdgeKinds({ edge }: { edge: GraphEdge }) {
  return (
    <span className="graph-kinds">
      {edge.memory_count > 0 && <Badge>共同涉及 {edge.memory_count}</Badge>}
      {edge.relationship_count > 0 && (
        <Badge tone="purple">关系记忆 {edge.relationship_count}</Badge>
      )}
      {edge.links.some((l) => l.kind === "same_as") && (
        <Badge tone="purple">可能同一人 · 未确认</Badge>
      )}
      {edge.links.some((l) => l.kind === "roleplay") && (
        <Badge>扮演 · 虚构角色</Badge>
      )}
    </span>
  );
}

function Drawing({
  data,
  onEdge,
}: {
  data: GraphData;
  onEdge: (edge: GraphEdge) => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const [availableWidth, setAvailableWidth] = useState(900);
  const [hovered, setHovered] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const tooltipId = useId();
  useEffect(() => {
    const element = scroll.current;
    if (!element) return;
    const resize = () => {
      const width = Math.floor(element.getBoundingClientRect().width);
      if (width > 0) setAvailableWidth(width);
    };
    resize();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const frame = useMemo(
    () => graphFrame(data.nodes.length, availableWidth),
    [data.nodes.length, availableWidth],
  );
  const positions = useMemo(
    () => graphLayout(data.nodes, data.edges, availableWidth),
    [data, availableWidth],
  );
  const labels = useMemo(
    () => graphLabels(data.nodes, data.edges, positions, frame),
    [data, positions, frame],
  );
  const names = new Map(data.nodes.map((node) => [node.id, node.name]));
  const preview = data.edges.find(
    (edge) => edgeKey(edge) === (focused || hovered),
  );
  return (
    <>
      <div
        ref={scroll}
        className="graph-scroll"
        tabIndex={0}
        aria-label="关系图画布，可横向滚动"
      >
        <svg
          viewBox={`0 0 ${frame.width} ${frame.height}`}
          style={{ minWidth: frame.width }}
          role="group"
          aria-label="人物关系图"
          className={`people-graph-svg${frame.compact ? " compact" : ""}`}
        >
          <title>人物关系图</title>
          <desc>
            点击节点打开人物的全部有效记忆，点击连线查看依据。悬停或聚焦连线查看摘要，也可使用画布下方的人物和连线列表。
          </desc>
          {data.edges.map((edge) => {
            const a = positions[edge.source],
              b = positions[edge.target];
            const label = `查看${names.get(edge.source)}与${names.get(edge.target)}的连线依据`;
            const key = edgeKey(edge);
            const textPoint = labels[key];
            const style = edge.links.some((l) => l.kind === "same_as")
              ? "possible"
              : edge.links.length
                ? "roleplay"
                : edge.relationship_count
                  ? "relationship"
                  : "cooccurrence";
            return (
              <g
                key={key}
                className={`graph-edge ${style}`}
                role="button"
                tabIndex={0}
                aria-label={label}
                aria-describedby={preview === edge ? tooltipId : undefined}
                onMouseEnter={() => setHovered(key)}
                onMouseLeave={() => setHovered(null)}
                onFocus={() => setFocused(key)}
                onBlur={() => setFocused(null)}
                onClick={() => onEdge(edge)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onEdge(edge);
                  }
                }}
              >
                <title>
                  {label}；{edge.memory_count} 条共同涉及记忆
                </title>
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  className="graph-edge-hit"
                />
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  strokeWidth={Math.min(
                    7,
                    1.5 + Math.log2(1 + edge.memory_count),
                  )}
                />
                {textPoint && (
                  <text
                    x={textPoint.x}
                    y={textPoint.y}
                    textAnchor="middle"
                    dominantBaseline="middle"
                  >
                    {edgeLabel(edge)}
                  </text>
                )}
              </g>
            );
          })}
          {data.nodes.map((node) => (
            <a
              key={node.id}
              href={memoryUrl(node.id)}
              aria-label={`查看${node.name}的有效记忆`}
              className={`graph-node ${node.kind === "self" ? "self" : ""}`}
            >
              <title>
                {node.name}；别名：{node.aliases.join("、") || "暂无"}；
                {node.account_count} 个账号；{node.memory_count} 条有效记忆
              </title>
              <circle
                cx={positions[node.id].x}
                cy={positions[node.id].y}
                r={
                  frame.compact
                    ? 14
                    : Math.min(27, 18 + Math.log2(1 + node.memory_count))
                }
              />
              <text
                x={positions[node.id].x}
                y={positions[node.id].y + 5}
                textAnchor="middle"
                className="graph-node-mark"
              >
                {node.kind === "self" ? "我" : Array.from(node.name)[0]}
              </text>
              <rect
                className="graph-node-label-background"
                x={positions[node.id].x - (frame.compact ? 44 : 78)}
                y={positions[node.id].y + (frame.compact ? 20 : 30)}
                width={frame.compact ? 88 : 156}
                height={frame.compact ? 33 : 38}
                rx="4"
              />
              <text
                x={positions[node.id].x}
                y={positions[node.id].y + (frame.compact ? 32 : 44)}
                textAnchor="middle"
              >
                {short(node.name, frame.compact ? 6 : 10)}
              </text>
              <text
                x={positions[node.id].x}
                y={positions[node.id].y + (frame.compact ? 48 : 63)}
                textAnchor="middle"
                className="graph-node-count"
              >
                {node.memory_count} 条记忆
              </text>
            </a>
          ))}
        </svg>
      </div>
      <div className="graph-preview">
        {preview ? (
          <div id={tooltipId} role="tooltip">
            <strong>
              {names.get(preview.source)} · {names.get(preview.target)}
            </strong>
            <p>{edgeLabel(preview)}（数字为共同涉及的有效记忆数）</p>
            {preview.relationship_summaries.map((summary) => (
              <p key={summary.id}>{summary.summary}</p>
            ))}
            {!preview.relationship_summaries.length && (
              <p>点击连线查看联系记录与支撑记忆。</p>
            )}
          </div>
        ) : (
          <p>
            悬停或用键盘聚焦连线可查看摘要；数字为共同涉及的有效记忆数。拥挤处省略标签，完整内容见下方列表。
          </p>
        )}
      </div>
    </>
  );
}

function EdgeEvidence({
  edge,
  names,
  entryId,
  onClose,
  openMemory,
}: {
  edge: GraphEdge;
  names: Map<string, string>;
  entryId: string;
  onClose: () => void;
  openMemory: (id: number) => void;
}) {
  const [offset, setOffset] = useState(0);
  const query = params({
    source_id: edge.source,
    target_id: edge.target,
    entry_id: entryId,
    limit: "30",
    offset: String(offset),
  });
  const detail = useData<Evidence>(`/people-graph/edge?${query}`);
  return (
    <Dialog
      title={`连线依据：${names.get(edge.source)} · ${names.get(edge.target)}`}
      onClose={onClose}
    >
      <p>
        这里只显示当前仍有效的记忆。身份／扮演联系来自独立联系记录；同来源记忆不等于确认身份，也不把角色经历归给扮演者。
      </p>
      {detail.error && (
        <Notice error>
          {detail.error}
          <button onClick={detail.refresh}>重新读取依据</button>
        </Notice>
      )}
      {detail.loading && <p role="status">正在读取连线依据…</p>}
      {detail.data && (
        <>
          {detail.data.links.map((link) => (
            <article className="graph-link-record" key={link.id}>
              <h3>
                {link.kind === "same_as"
                  ? "可能同一人 · 未确认"
                  : "扮演 · 虚构角色"}
              </h3>
              <p>
                {names.get(link.source)} {link.kind === "roleplay" ? "→" : "↔"}{" "}
                {names.get(link.target)} · 相信程度 {link.belief}
              </p>
              {link.kind === "roleplay" && (
                <p>
                  场景：
                  {link.worlds.map((world) => world || "场景未记录").join("、")}
                </p>
              )}
              <p>
                联系 #{link.id} · {link.evidence_message_count} 条来源消息
              </p>
              <a
                href={`#/people?id=${encodeURIComponent(link.source)}`}
                onClick={onClose}
              >
                到人物详情核对联系与消息
              </a>
            </article>
          ))}
          <p>共 {detail.data.total} 条支撑／同来源记忆</p>
          {!detail.data.items.length && (
            <Empty title="没有可展示的有效支撑记忆">
              联系记录可能只有来源消息；遗忘、删除或清除的记忆不会在这里显示。
            </Empty>
          )}
          <div className="graph-evidence-list">
            {detail.data.items.map((memory) => (
              <article key={memory.id}>
                <p>
                  <Badge>{memory.kind}</Badge> #{memory.id} · 修订{" "}
                  {memory.revision}
                </p>
                <p className="memory-content">{memory.content}</p>
                <p>
                  {memory.cooccurrence && <Badge>共同涉及两人</Badge>}{" "}
                  {memory.link_evidence && <Badge>同来源联系证据</Badge>}
                </p>
                <button
                  onClick={() => {
                    onClose();
                    openMemory(memory.id);
                  }}
                >
                  查看记忆 #{memory.id}
                </button>
              </article>
            ))}
          </div>
          <Pagination
            total={detail.data.total}
            offset={offset}
            change={setOffset}
          />
        </>
      )}
    </Dialog>
  );
}

export default function PeopleGraph({
  initialQuery = "",
  openMemory,
}: {
  initialQuery?: string;
  openMemory: (id: number) => void;
}) {
  const initial = new URLSearchParams(initialQuery);
  const [filter, setFilter] = useState<Filters>({
    entry_id: initial.get("entry_id") || "",
    person_id: initial.get("person_id") || "",
    hops: "1",
    min_memories: "1",
    node_limit: "60",
    edge_limit: "100",
  });
  const [applied, setApplied] = useState(filter);
  const [selected, setSelected] = useState<GraphEdge | null>(null);
  const catalog = useData<{ people: Person[]; entries: Entry[] }>("/catalog");
  const graph = useData<GraphData>(`/people-graph?${params(applied)}`);
  const names = new Map(
    graph.data?.nodes.map((node) => [node.id, node.name]) || [],
  );
  const apply = (value: Filters) => {
    setSelected(null);
    setApplied({ ...value });
    if (params(value) === params(applied)) graph.refresh();
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">从已有经历看人物联系</p>
          <h1>人物关系图</h1>
          <p>点击人物查看记忆，点击连线追溯依据。</p>
        </div>
        <a href="#/people" className="button">
          返回人物列表
        </a>
      </div>
      <form
        className="panel graph-filters"
        onSubmit={(event) => {
          event.preventDefault();
          apply(filter);
        }}
      >
        <label>
          来源入口
          <select
            value={filter.entry_id}
            onChange={(event) =>
              setFilter({ ...filter, entry_id: event.target.value })
            }
          >
            <option value="">全部入口</option>
            {catalog.data?.entries.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          聚焦人物
          <select
            value={filter.person_id}
            onChange={(event) =>
              setFilter({ ...filter, person_id: event.target.value })
            }
          >
            <option value="">所有人物</option>
            {catalog.data?.people
              .filter((person) => person.id !== "scene")
              .map((person) => (
                <option key={person.id} value={person.id}>
                  {person.name}
                  {person.id === "self" ? "（角色）" : ""} · {person.id}
                </option>
              ))}
          </select>
        </label>
        <label>
          范围
          <select
            value={filter.hops}
            disabled={!filter.person_id}
            onChange={(event) =>
              setFilter({ ...filter, hops: event.target.value })
            }
          >
            <option value="1">一跳联系</option>
            <option value="2">两跳联系</option>
          </select>
        </label>
        <label>
          共现最少记忆数
          <input
            type="number"
            min="1"
            max="1000000"
            required
            value={filter.min_memories}
            onChange={(event) =>
              setFilter({ ...filter, min_memories: event.target.value })
            }
          />
        </label>
        <label>
          节点上限
          <input
            type="number"
            min="2"
            max="100"
            required
            value={filter.node_limit}
            onChange={(event) =>
              setFilter({ ...filter, node_limit: event.target.value })
            }
          />
        </label>
        <label>
          连线上限
          <input
            type="number"
            min="1"
            max="200"
            required
            value={filter.edge_limit}
            onChange={(event) =>
              setFilter({ ...filter, edge_limit: event.target.value })
            }
          />
        </label>
        <button className="primary" disabled={graph.loading}>
          生成关系图
        </button>
      </form>
      {catalog.error && (
        <Notice error>
          筛选目录：{catalog.error}
          <button onClick={catalog.refresh}>重新读取目录</button>
        </Notice>
      )}
      <details className="panel graph-explanation">
        <summary>数据口径与图例</summary>
        <p>
          只统计 active 且未彻底清除的记忆。共现按同一记忆的 about
          涉及人两两计数，每条只算一次；共现不等于关系事实。“关系”类记忆另外显示正文摘要。
        </p>
        <p>
          入口筛选只认至少一条直接来源消息在该入口的记忆，不沿派生依据扩大范围。身份／扮演联系按其来源消息筛选，可没有支撑记忆；共现阈值不隐藏这些独立联系。
        </p>
        <p>
          节点记忆数包括作为涉及人或说话人的有效记忆，别名与账号数为当前主体资料。self
          显示为角色节点，scene
          不作为人物；已合并主体只显示保留人物。点击人物打开其全部入口的有效记忆列表。
        </p>
        <p>
          线条越粗，共同涉及记忆越多；紫色为关系记忆，虚线为可能同一人，绿色为扮演。位置和距离只用于排布，没有关系强弱含义。图中读数不调用模型，不产生召回或使用记录。
        </p>
      </details>
      {graph.error && (
        <Notice error>
          {graph.error}
          <button onClick={graph.refresh}>重新读取关系图</button>
        </Notice>
      )}
      {graph.loading && <p role="status">正在生成只读关系图…</p>}
      {graph.data && (
        <>
          <p aria-live="polite">
            当前显示 {graph.data.nodes.length} 个节点、{graph.data.edges.length}{" "}
            条连线。
          </p>
          {graph.data.truncated && (
            <Notice>
              筛选后共有 {graph.data.total_nodes} 个节点、
              {graph.data.total_edges}{" "}
              条连线，超过显示上限。按共同涉及记忆数从高到低保留，关系记忆数和主体标识用于同权重排序；聚焦人物优先保留。可缩小入口或聚焦范围后重新查看。
            </Notice>
          )}
          {!graph.data.nodes.length ? (
            <Empty title="当前范围没有人物关系数据">
              可以改选入口或人物；遗忘、删除和清除的记忆不参与关系图。
            </Empty>
          ) : (
            <>
              <section className="panel graph-panel" aria-label="关系图与图例">
                <div className="graph-legend">
                  <span>实线：共现</span>
                  <span>紫色：关系记忆</span>
                  <span>虚线：可能同一人</span>
                  <span>绿色：扮演</span>
                </div>
                <Drawing data={graph.data} onEdge={setSelected} />
                <p className="muted">
                  窄屏可横向滚动画布。下方列表提供相同的跳转和证据操作。
                </p>
              </section>
              <section aria-label="图中人物">
                <h2>图中人物</h2>
                <div className="graph-node-list">
                  {graph.data.nodes.map((node) => (
                    <article className="panel" key={node.id}>
                      <h3>
                        <a
                          href={memoryUrl(node.id)}
                          aria-label={`查看${node.name}的有效记忆`}
                        >
                          {node.name}
                        </a>{" "}
                        {node.kind === "self" && <Badge>角色</Badge>}
                      </h3>
                      <small className="muted">{node.id}</small>
                      <p>别名：{node.aliases.join("、") || "暂无"}</p>
                      <p>
                        {node.account_count} 个账号 · {node.memory_count}{" "}
                        条有效记忆
                      </p>
                      <button
                        className="text-button"
                        aria-label={`聚焦${node.name}`}
                        onClick={() => {
                          const next = { ...filter, person_id: node.id };
                          setFilter(next);
                          apply(next);
                        }}
                      >
                        以此人为中心
                      </button>
                    </article>
                  ))}
                </div>
              </section>
              <section aria-label="图中连线">
                <h2>图中连线</h2>
                {!graph.data.edges.length && (
                  <p className="muted">当前阈值和范围没有连线。</p>
                )}
                <div className="graph-edge-list">
                  {graph.data.edges.map((edge) => (
                    <article
                      className="panel"
                      key={JSON.stringify([edge.source, edge.target])}
                    >
                      <h3>
                        {names.get(edge.source)} · {names.get(edge.target)}
                      </h3>
                      <EdgeKinds edge={edge} />
                      <p>{edge.memory_count} 条共同涉及记忆</p>
                      {edge.relationship_summaries.map((memory) => (
                        <p key={memory.id}>
                          关系记忆 #{memory.id}：{memory.summary}
                        </p>
                      ))}
                      {edge.relationship_count >
                        edge.relationship_summaries.length && (
                        <small>更多关系正文见连线依据。</small>
                      )}
                      <button
                        onClick={() => setSelected(edge)}
                        aria-label={`查看${names.get(edge.source)}与${names.get(edge.target)}的连线依据`}
                      >
                        查看连线依据
                      </button>
                    </article>
                  ))}
                </div>
              </section>
            </>
          )}
        </>
      )}
      {selected && (
        <EdgeEvidence
          key={JSON.stringify([
            selected.source,
            selected.target,
            applied.entry_id,
          ])}
          edge={selected}
          names={names}
          entryId={applied.entry_id}
          onClose={() => setSelected(null)}
          openMemory={openMemory}
        />
      )}
    </>
  );
}
