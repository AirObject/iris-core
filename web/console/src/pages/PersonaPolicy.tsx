import { useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/generated";
import type { Action } from "../api/design";
import { ActionButton, ActionDialog, QueryState, TextData, useQuery } from "../components/core";

type Policy = components["schemas"]["PersonaPolicyView"];

export function PersonaPolicy({ path, onChanged }: { path: string; onChanged: () => void }) {
  const query = useQuery<Policy>(path);
  const [selection, setSelection] = useState<{ action: Action; revision: number }>();
  const policy = query.data;
  return <section className="panel" aria-label="人格演进策略">
    <h2>人格演进策略{policy ? ` · Revision ${policy.revision}` : ""}</h2>
    <p>策略控制未来的提案与自动演进。替换策略保留旧版本，不修改已发布的人格内容。</p>
    <p>locked 禁止提案演进；manual 要求人工审批；bounded_auto 允许满足策略约束的后台提案自动发布。</p>
    <button onClick={query.refresh}>刷新策略</button>
    <QueryState query={query}>
      {policy && <>
        <p>模式：{policy.config.mode} · 更新时间：<time dateTime={policy.created_at}>{new Date(policy.created_at).toLocaleString()}</time></p>
        <p>内容 Hash：{policy.content_hash}</p>
        <dl>
          <dt>允许修改的字段</dt><dd><TextData value={policy.config.allowed_fields} /></dd>
          <dt>必须人工审阅的字段</dt><dd><TextData value={policy.config.sensitive_fields} /></dd>
          <dt>变化上限</dt><dd>单次 {policy.config.max_single_delta} · 累计 {policy.config.max_cumulative_delta}</dd>
          <dt>证据要求</dt><dd>至少 {policy.config.min_evidence} 条、{policy.config.min_distinct_sources} 个来源；置信度 ≥ {policy.config.min_confidence}</dd>
          <dt>累计窗口 / 冷却期 / 观察期（微秒）</dt><dd>{policy.config.cumulative_window_us} / {policy.config.cooldown_us} / {policy.config.observation_us}</dd>
          <dt>证据最小时间跨度（微秒）</dt><dd>{policy.config.min_evidence_span_us}</dd>
          <dt>回滚阈值</dt><dd>{policy.config.rollback_threshold}</dd>
        </dl>
        {policy.actions.map((action) => <ActionButton key={action.id} action={action}
          onClick={() => setSelection({ action, revision: policy.revision })} />)}
      </>}
    </QueryState>
    {selection && <ActionDialog action={selection.action} path={path}
      onClose={() => { setSelection(undefined); onChanged(); }} onSuccess={onChanged}
      bodyBuilder={(fields, reason) => ({ expected_revision: selection.revision, reason_code: reason, config: fields })}
      loadLatest={async () => {
        const { data } = await api.request<Policy>(path);
        return { revision: data.revision, fields: data.config, label: `服务器最新 Policy Revision ${data.revision}` };
      }} />}
  </section>;
}
