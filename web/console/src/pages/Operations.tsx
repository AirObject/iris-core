import { useState } from "react";
import type { components } from "../api/generated";
import { OperationPanel } from "../components/Operation";
import { Empty, MetaLine, QueryState, useQuery } from "../components/core";

type Operation = components["schemas"]["ConsoleOperation"];
const statuses: Operation["status"][] = ["queued", "running", "paused", "blocked", "completed", "completed_with_warnings", "failed", "cancelled", "cancelled_partial"];

export function OperationsPage() {
  const [status, setStatus] = useState("");
  const [cursor, setCursor] = useState("");
  const [selected, setSelected] = useState<Operation>();
  const params = new URLSearchParams({ limit: "50", ...(status ? {status} : {}), ...(cursor ? {cursor} : {}) });
  const query = useQuery<Operation[]>(`/operations?${params}`);
  return <>
    <h1>批量操作</h1>
    <p>只显示当前密钥及授权范围下接受的操作。接受请求后，可在这里查看实际进度。</p>
    <label>操作状态 <select value={status} onChange={event => {setStatus(event.target.value); setCursor(""); setSelected(undefined);}}>
      <option value="">全部状态</option>
      {statuses.map(value => <option key={value} value={value}>{value}</option>)}
    </select></label>
    <button onClick={() => query.refresh()}>刷新列表</button>
    <QueryState query={query}>
      {!query.data?.length ? <Empty>没有可见的操作。</Empty> : <table><thead><tr><th>操作</th><th>状态</th><th>进度</th><th>创建时间</th><th>详情</th></tr></thead><tbody>
        {query.data.map(operation => <tr key={operation.id}><td>{operation.id}</td><td>{operation.status}</td><td>{operation.progress.processed} / {operation.progress.total}</td><td>{operation.created_at}</td><td><button onClick={() => setSelected(operation)}>查看进度</button></td></tr>)}
      </tbody></table>}
    </QueryState>
    {query.meta && <MetaLine meta={query.meta} />}
    {query.meta?.page?.has_more && <button onClick={() => {setCursor(query.meta?.page?.next_cursor ?? ""); setSelected(undefined);}}>下一页</button>}
    {cursor && <button onClick={() => {setCursor(""); setSelected(undefined);}}>返回首页</button>}
    {selected && <OperationPanel key={selected.id} initial={selected} />}
  </>;
}
