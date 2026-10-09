import { useData } from "./api";
import { Badge, Notice } from "./ui";
import type { PersonaSnapshot } from "./persona-types";
export function PersonaSummary() {
  const snapshot = useData<PersonaSnapshot>("/persona", 2000);
  const data = snapshot.data;
  return (
    <section className="persona-summary" aria-label="试用 persona">
      {snapshot.error && (
        <Notice error>
          {snapshot.error}。persona 可能尚未更新。
          <button onClick={snapshot.refresh}>重试 persona</button>
        </Notice>
      )}
      {data?.needs_update && (
        <p>
          <Badge tone="warning">待更新</Badge> {data.stale_basis_count}{" "}
          条依据已失效，当前版本仍在使用。
        </p>
      )}
      {data?.pending && (
        <p>
          <Badge tone="warning">待确认</Badge> 候选 v{data.pending.id}{" "}
          正在等待确认。
        </p>
      )}
      <details open>
        <summary>
          当前 persona{data?.current ? ` · v${data.current.id}` : ""}
        </summary>
        <p className="persona-content">
          {data
            ? data.current?.content || "暂无 persona 版本"
            : "正在读取 persona…"}
        </p>
      </details>
      <a href="#/persona">查看 persona 与自我</a>
    </section>
  );
}
