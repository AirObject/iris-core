import { useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/generated";
import type { Action, Resource } from "../api/design";
import { ActionButton, ActionDialog, QueryState, TextData, useQuery } from "../components/core";

type Context = components["schemas"]["PersonaProposalContext"];
type Selection = { action: Action; base: number; policy: number | null; proposalId?: string };

export function PersonaProposals({ path, onChanged }: { path: string; onChanged: () => void }) {
  const create = useQuery<Context>(`${path}/commands`);
  const [cursor, setCursor] = useState("");
  const [previous, setPrevious] = useState<string[]>([]);
  const list = useQuery<Resource[]>(`${path}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
  const [proposalId, setProposalId] = useState<string>();
  const [selection, setSelection] = useState<Selection>();
  const refresh = () => { create.refresh(); list.refresh(); };
  return <section className="panel" aria-label="Persona 提案">
    <h2>Persona 提案</h2>
    <p>创建提案后等待人工审阅。批准会发布新的人格版本；拒绝保留提案及审阅历史。</p>
    <button onClick={refresh}>刷新提案</button>
    <QueryState query={create}>
      {create.data && <>
        <p>当前人格版本 {create.data.current_revision} · Policy {create.data.expected_policy_revision} · {create.data.policy_mode}</p>
        {create.data.policy_mode === "locked" && <p>当前 Policy 已锁定提案创建和发布。</p>}
        {create.data.actions.map((action) => <ActionButton key={action.id} action={action}
          onClick={() => setSelection({ action, base: create.data!.base_revision, policy: create.data!.expected_policy_revision })} />)}
      </>}
    </QueryState>
    <QueryState query={list}>
      {list.data?.length === 0 && <p>暂无提案。</p>}
      {list.data?.map((proposal) => <article key={proposal.id}>
        <p>基准版本 {String(proposal.fields.base_revision)} · {proposal.status} · {proposal.created_at}</p>
        <TextData value={proposal.fields.patch} />
        <button onClick={() => setProposalId(proposal.id)}>审阅提案 {proposal.id}</button>
      </article>)}
      {previous.length > 0 && <button onClick={() => {
        setCursor(previous[previous.length - 1] ?? ""); setPrevious(previous.slice(0, -1));
      }}>上一页提案</button>}
      {list.meta?.page?.has_more && <button onClick={() => {
        setPrevious([...previous, cursor]); setCursor(list.meta?.page?.next_cursor ?? "");
      }}>下一页提案</button>}
    </QueryState>
    {proposalId && <ProposalDetail key={proposalId} path={`${path}/${encodeURIComponent(proposalId)}`}
      onSelect={setSelection} onClose={() => setProposalId(undefined)} />}
    {selection && <ActionDialog action={selection.action}
      path={selection.proposalId ? `${path}/${encodeURIComponent(selection.proposalId)}:${selection.action.id}` : path}
      onClose={() => { setSelection(undefined); onChanged(); }} onSuccess={onChanged}
      loadLatest={async () => {
        const target = selection.proposalId ? `${path}/${encodeURIComponent(selection.proposalId)}` : `${path}/commands`;
        const { data } = await api.request<Context>(target);
        return { revision: data.base_revision, label: "服务器最新提案上下文", fields: {
          base_revision: data.base_revision, current_revision: data.current_revision,
          policy_revision: data.expected_policy_revision, policy_mode: data.policy_mode,
          status: data.proposal?.status ?? null, proposal: data.proposal?.fields ?? null,
        } };
      }}
      bodyBuilder={(fields, reason) => ({
        base_revision: selection.base, reason_code: reason,
        ...(selection.action.id !== "reject" ? { expected_policy_revision: selection.policy } : {}),
        ...(selection.action.id === "create" ? {
          fields: { patch: fields.patch, confidence: fields.confidence, ttl_us: fields.ttl_us },
          evidence_refs: fields.evidence_refs,
        } : {}),
      })} />}
  </section>;
}

function ProposalDetail({ path, onSelect, onClose }: {
  path: string; onSelect: (selection: Selection) => void; onClose: () => void;
}) {
  const detail = useQuery<Context>(path);
  const view = detail.data;
  const proposal = view?.proposal;
  return <section className="panel" aria-label="提案详情">
    <h3>提案详情</h3>
    <button onClick={detail.refresh}>刷新提案详情</button>
    <button onClick={onClose}>关闭提案详情</button>
    <QueryState query={detail}>
      {view && proposal && <>
        <p>状态：{proposal.status} · 基准版本 {view.base_revision} · 当前版本 {view.current_revision ?? "不可见"}</p>
        <p>Policy {view.expected_policy_revision ?? "不可见"} · {view.policy_mode}</p>
        <h4>提案修改</h4><TextData value={proposal.fields.patch} />
        <p>置信度：{String(proposal.fields.confidence)}</p>
        <p>来源：{String(proposal.fields.generator)} · {String(proposal.fields.generator_version)}</p>
        <p>到期时间：<time dateTime={String(proposal.fields.expires_at)}>{new Date(String(proposal.fields.expires_at)).toLocaleString()}</time></p>
        <h4>证据引用</h4><TextData value={proposal.source_refs} />
        {proposal.status === "proposed" && !view.available_actions.includes("approve") && <p>此提案当前不可批准。请核对权限、有效期、基准版本与 Policy。</p>}
        {view.actions.map((action) => <ActionButton key={action.id} action={action}
          onClick={() => onSelect({ action, base: view.base_revision, policy: view.expected_policy_revision, proposalId: proposal.id })} />)}
      </>}
    </QueryState>
  </section>;
}
