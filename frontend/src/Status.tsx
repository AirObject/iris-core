import {
  Badge,
  Empty,
  Notice,
  batchLabel,
  healthLabel,
  seconds,
  time,
} from "./ui";
import { useData } from "./api";
import { MaintenancePanel } from "./Maintenance";
import { EntryQueueWait } from "./EntrySettings";
import { modelNames, type Settings } from "./Settings";
import type { Entry, Status as StatusData } from "./types";

const purposes: Record<string, string> = {
  learning: "学习",
  learning_repair: "学习 JSON 修正",
  trial_reply: "试用回复",
  trial_reply_repair: "回复 JSON 修正",
  embedding: "向量生成",
  retrieval_query: "召回查询",
  health_probe: "健康探测",
  recall_judge: "召回判断",
  connection_check: "连接测试",
};
export default function Status({
  data,
  openMemory,
}: {
  data: StatusData | null;
  openMemory: (id: number) => void;
}) {
  const catalog = useData<{ entries: Entry[] }>("/catalog");
  const settings = useData<Pick<Settings, "recall_judge">>("/settings", 5000);
  if (!data) return <Empty title="正在读取运行状态" />;
  const latency = data.learning_latency_24h;
  const errors = data.models.filter((m) => m.result_category !== "success");
  const names = new Map(catalog.data?.entries.map((e) => [e.id, e.name]));
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">每一步，都看得见</p>
          <h1>运行状态</h1>
          <p>服务、模型和学习队列的当前情况。</p>
        </div>
        <Badge tone={data.scheduler.running ? "green" : "danger"}>
          {data.scheduler.running ? "调度运行中" : "调度未运行"}
        </Badge>
      </div>
      <div className="stat-grid">
        <section className="panel metric">
          <span>待学习消息</span>
          <strong>
            {data.entries.reduce((n, e) => n + e.pending_count, 0)}
          </strong>
          <small>{data.scheduler.max_concurrent} 个学习批次可同时运行</small>
        </section>
        <section className="panel metric">
          <span>今日 token 用量</span>
          <strong>{data.usage.today.tokens.toLocaleString()}</strong>
          <small>
            {data.usage.today.calls} 次请求 ·{" "}
            {data.usage.today.reasoning_tokens.toLocaleString()} 推理
            token（包含在总量内）
          </small>
        </section>
        <section className="panel metric">
          <span>近 24 小时学习 P95</span>
          <strong>{seconds(latency.p95_ms)}</strong>
          <small>{latency.count} 次学习调用</small>
        </section>
        <section className="panel metric">
          <span>近 24 小时学习超时</span>
          <strong>{latency.timeouts}</strong>
          <small>
            共享学习总预算 <b>{data.timeouts_seconds.learning} 秒</b>
          </small>
        </section>
      </div>
      <MaintenancePanel openMemory={openMemory} />
      <div className="status-grid">
        <section className="panel">
          <div className="panel-heading">
            <h2>模型用途</h2>
            <span className="muted">实时健康状态</span>
          </div>
          {Object.entries(data.model_health).map(([kind, h]) => (
            <div className="model-row" key={kind}>
              <div className="model-title">
                <strong>{modelNames[kind] || kind}</strong>
                <Badge tone={h.state === "normal" ? "green" : "danger"}>
                  {healthLabel(h.state)}
                </Badge>
              </div>
              <p className="muted">
                {kind === "chat"
                  ? "用于学习与角色回复"
                  : kind === "recall_judge"
                    ? "判断相关候选能否回答当前请求"
                    : "用于记忆向量与语义检索"}
              </p>
              {kind === "recall_judge" && (
                <>
                  {data.timeouts_seconds.recall_judge != null && (
                    <p>
                      总预算 {data.timeouts_seconds.recall_judge} 秒（含排队）
                    </p>
                  )}
                  {settings.error && (
                    <p className="muted">
                      判断开关状态暂时无法读取。
                      <button onClick={settings.refresh}>重新读取</button>
                    </p>
                  )}
                  {settings.data?.recall_judge?.enabled === false ? (
                    <p>召回判断已关闭，保留基础召回。</p>
                  ) : (
                    h.state !== "normal" && (
                      <p>召回判断暂停或退避时降级，保留仍有效的原召回。</p>
                    )
                  )}
                  {h.retry_at && <p>退避截止：{time(h.retry_at)}</p>}
                  <a href="#/settings">管理召回判断设置</a>
                </>
              )}
              {h.last_error && (
                <p className="error-summary">最近错误：{h.last_error}</p>
              )}
              {h.next_probe_at && (
                <small>下次探测：{time(h.next_probe_at)}</small>
              )}
            </div>
          ))}
        </section>
        <section className="panel">
          <h2>用量与学习</h2>
          <dl className="status-facts">
            <div>
              <dt>每日 token 上限</dt>
              <dd>
                {data.budget.limit == null
                  ? "不限"
                  : data.budget.limit.toLocaleString()}
              </dd>
            </div>
            <div>
              <dt>未报告 token 的调用</dt>
              <dd>{data.usage.today.calls_without_usage} 次</dd>
            </div>
            <div>
              <dt>学习耗时 P50 / 最大</dt>
              <dd>
                {seconds(latency.p50_ms)} / {seconds(latency.max_ms)}
              </dd>
            </div>
            <div>
              <dt>缺少向量的记忆</dt>
              <dd>{data.missing_vectors} 条</dd>
            </div>
            <div>
              <dt>记忆缺口</dt>
              <dd>{data.memory_gap_count} 段</dd>
            </div>
          </dl>
          <p className="fine-print">
            今日按角色时区统计。服务商未报告的用量和在途调用不计入已知总量；未配置价格，暂不估算费用。
          </p>
        </section>
      </div>
      <section className="panel">
        <div className="panel-heading">
          <h2>入口队列</h2>
          <span className="muted">接收、学习和形成记忆分别记录</span>
        </div>
        {!data.entries.length ? (
          <p className="quiet">暂无入口。</p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>入口</th>
                  <th>待学习</th>
                  <th>当前批次</th>
                  <th>过滤与等待</th>
                  <th>最近结束的批次</th>
                  <th>未记住的消息</th>
                </tr>
              </thead>
              <tbody>
                {data.entries.map((e) => (
                  <tr key={e.entry_id}>
                    <td>{names.get(e.entry_id) || e.entry_id}</td>
                    <td>{e.pending_count}</td>
                    <td>{batchLabel(e.current_batch)}</td>
                    <td>
                      <EntryQueueWait value={e.queue_wait} />
                    </td>
                    <td>{batchLabel(e.latest_batch)}</td>
                    <td>{e.gap_message_count || 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <div className="status-grid">
        <section className="panel">
          <h2>最近学习调用</h2>
          {!data.learning_calls_24h.length ? (
            <p className="quiet">近 24 小时没有学习调用。</p>
          ) : (
            <div className="call-list">
              {data.learning_calls_24h.slice(0, 12).map((c, i) => (
                <div className="call" key={i}>
                  <span>
                    批次 #{c.batch_id} · {purposes[c.purpose] || c.purpose}
                  </span>
                  <strong>{seconds(c.duration_ms)}</strong>
                  {c.timed_out ? (
                    <Badge tone="danger">超时</Badge>
                  ) : c.error_summary ? (
                    <Badge tone="danger">失败</Badge>
                  ) : (
                    <Badge tone="green">调用完成</Badge>
                  )}
                  {c.error_summary && <small>{c.error_summary}</small>}
                </div>
              ))}
            </div>
          )}
          <p className="fine-print">
            模型调用完成不等于批次成功；JSON 校验和记忆写入结果请看批次。
          </p>
        </section>
        <section className="panel">
          <h2>错误摘要</h2>
          <p className="muted section-help">
            来自现有状态接口的最近用途错误与调度错误
          </p>
          {data.scheduler.last_error && (
            <Notice error>{data.scheduler.last_error}</Notice>
          )}
          {errors.map((e) => (
            <div className="error-entry" key={e.purpose}>
              <strong>
                {purposes[e.purpose] || e.purpose} · {e.model}
              </strong>
              <p>{e.error_summary || e.result_category}</p>
              <small>{time(e.created_at)}</small>
            </div>
          ))}
          {!errors.length && !data.scheduler.last_error && (
            <p className="quiet">最近的用途调用没有错误。</p>
          )}
        </section>
      </div>
    </>
  );
}
