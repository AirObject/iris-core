import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/generated";
import type { Action, Resource } from "../api/design";
import { ActionButton, ActionDialog, FieldsForm, QueryState, TextData, useQuery } from "../components/core";
import { Related } from "./Memory";
import { PersonaProposals } from "./PersonaProposals";
import { PersonaPolicy } from "./PersonaPolicy";

type Commands = components["schemas"]["PersonaCommandsView"];
type StateView = components["schemas"]["PersonaStateView"];

export function PersonasPage() {
  const [params, setParams] = useSearchParams();
  const agent = params.get("agent_id") ?? "";
  return <>
    <h1>Persona 人格</h1>
    <FieldsForm fields={[{ key: "agent_id", label: "选择已有 Agent", type: "lookup", lookup: "agents" }]}
      value={{ agent_id: agent }} onChange={(value) => setParams({ agent_id: String(value.agent_id ?? "") })} />
    {agent ? <PersonaWorkspace key={agent} agent={agent} /> : <p>选择 Agent 查看当前人格。</p>}
  </>;
}

function PersonaWorkspace({ agent }: { agent: string }) {
  const path = `/personas/${encodeURIComponent(agent)}`;
  const current = useQuery<Resource>(path);
  const commands = useQuery<Commands>(`${path}/commands`);
  const [selected, setSelected] = useState<{ action: Action; resource: Resource; policy: number }>();
  const [generation, setGeneration] = useState(0);
  const [notice, setNotice] = useState("");
  const refresh = () => { current.refresh(); commands.refresh(); setGeneration((value) => value + 1); };
  const record = current.data;
  const reviewedTogether = record && commands.data?.expected_revision === record.revision
    && commands.data?.expected_policy_revision === record.fields.policy_revision;
  return <>
    <p>发布和回滚都会创建新版本。回滚保留目标内容与证据引用，提交时重新检查权限、Policy 与证据。</p>
    <button onClick={refresh}>刷新 Persona</button>
    {notice && <p role="status">{notice}</p>}
    <QueryState query={current}>
      {record && <section className="panel">
        <h2>Current · Revision {record.revision}</h2>
        <p>Policy Revision {String(record.fields.policy_revision)} · {String(record.fields.policy_mode)}</p>
        <p>内容 Hash：{String(record.fields.content_hash)}</p>
        <p>更新时间：{record.updated_at}</p>
        {["core", "traits", "narrative"].map((layer) => <details open key={layer}>
          <summary>{layer}</summary><TextData value={record.fields[layer]} />
        </details>)}
        <details><summary>证据引用</summary><TextData value={record.source_refs} /></details>
        <QueryState query={commands}>
          {reviewedTogether ? commands.data?.actions.map((action) => <ActionButton key={action.id}
            action={action} resource={record} onClick={() => setSelected({
              action, resource: { ...record, fields: {
                ...record.fields,
                // ADR-0008's frozen empty bootstrap layers are edited as empty objects.
                ...Object.fromEntries(["core", "traits", "narrative"].map((layer) => [layer,
                  record.fields[layer] === "" || record.fields[layer] === "[]" ? "{}" : record.fields[layer],
                ])),
                source_refs: record.source_refs ?? [],
              } },
              policy: commands.data!.expected_policy_revision,
            })} />) : <p>Current 或 Policy 已变化，请刷新后重新审阅。</p>}
        </QueryState>
      </section>}
    </QueryState>
    <PersonaStatePanel key={`state-${generation}`} path={`${path}/state`} />
    <PersonaPolicy key={`policy-${generation}`} path={`${path}/policy`} onChanged={refresh} />
    <Related key={`history-${generation}`} path={`${path}/history`} title="Persona 历史" />
    <PersonaProposals key={`proposals-${generation}`} path={`${path}/proposals`} onChanged={refresh} />
    {selected && <ActionDialog action={selected.action} resource={selected.resource}
      path={`${path}${selected.action.id === "publish" ? "/revisions" : ":rollback"}`}
      onClose={() => { setSelected(undefined); refresh(); }} onSuccess={() => {
        setNotice("已创建新的 Persona Revision。当前版本与历史已刷新。"); refresh();
      }} loadLatest={async () => {
        const { data } = await api.request<Resource>(path);
        return { revision: data.revision, fields: data.fields,
          label: `服务器最新 Persona Revision ${data.revision} · Policy ${String(data.fields.policy_revision)}` };
      }} bodyBuilder={(fields, reason) => ({
        expected_revision: selected.resource.revision, expected_policy_revision: selected.policy,
        reason_code: reason,
        ...(selected.action.id === "publish" ? {
          fields: { core: fields.core, traits: fields.traits, narrative: fields.narrative },
          source_refs: fields.source_refs ?? [],
        } : { target_revision: fields.target_revision }),
      })} />}
  </>;
}

function PersonaStatePanel({ path }: { path: string }) {
  const query = useQuery<StateView>(path);
  const [selected, setSelected] = useState<{ action: Action; revision: number }>();
  const [notice, setNotice] = useState("");
  const view = query.data;
  return <section className="panel" aria-label="Persona 状态">
    <h2>Persona 状态{view ? ` · Revision ${view.expected_revision}` : ""}</h2>
    <p>状态独立保存。到期后返回设定的基线；清理到期状态会保留历史记录。</p>
    <button onClick={query.refresh}>刷新状态</button>
    {notice && <p role="status">{notice}</p>}
    <QueryState query={query}>
      {view && <>
        {view.current ? <>
          <h3>当前状态</h3><TextData value={view.current.fields.state} />
          <h3>到期基线</h3><TextData value={view.current.fields.baseline} />
          <p>到期时间：<time dateTime={String(view.current.fields.expires_at)}>
            {new Date(String(view.current.fields.expires_at)).toLocaleString()}
          </time></p>
          <details><summary>状态证据引用</summary><TextData value={view.current.source_refs} /></details>
        </> : <p>尚未设置 Persona 状态。</p>}
        {view.actions.map((action) => <ActionButton key={action.id} action={action}
          onClick={() => setSelected({ action, revision: view.expected_revision })} />)}
      </>}
    </QueryState>
    {selected && <ActionDialog action={selected.action}
      path={`${path}${selected.action.id === "clear" ? ":clear" : ""}`}
      onClose={() => { setSelected(undefined); query.refresh(); }} onSuccess={() => {
        setNotice("Persona 状态已保存。当前人格版本保持不变。"); query.refresh();
      }} loadLatest={async () => {
        const { data } = await api.request<StateView>(path);
        return { revision: data.expected_revision, fields: data.current?.fields ?? {},
          label: `服务器最新 State Revision ${data.expected_revision}` };
      }} bodyBuilder={(fields, reason) => ({
        expected_revision: selected.revision, reason_code: reason,
        ...(selected.action.id === "update" ? {
          fields: { state: fields.state, baseline: fields.baseline, ttl_us: fields.ttl_us },
          source_refs: fields.source_refs ?? [],
        } : {}),
      })} />}
  </section>;
}
