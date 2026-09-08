import { useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/generated";
import type { Action, Resource } from "../api/design";
import { ActionButton, ActionDialog, QueryState, TextData, useQuery } from "../components/core";

type Context = components["schemas"]["PersonaDraftContext"];
type Selection = { action: Action; view: Context };

export function PersonaDrafts({ path, onChanged }: { path: string; onChanged: () => void }) {
  const create = useQuery<Context>(`${path}/commands`);
  const [cursor, setCursor] = useState("");
  const [previous, setPrevious] = useState<string[]>([]);
  const list = useQuery<Resource[]>(`${path}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
  const [draftId, setDraftId] = useState<string>();
  const [selection, setSelection] = useState<Selection>();
  const refresh = () => { create.refresh(); list.refresh(); };
  return <section className="panel" aria-label="Persona 草稿">
    <h2>Persona 草稿</h2>
    <p>保存草稿不会发布人格。发布需重新认证，并核对当前人格、Policy 和证据。丢弃不可撤销。</p>
    <button onClick={refresh}>刷新草稿</button>
    <QueryState query={create}>
      {create.data && <>
        <p>当前人格版本 {create.data.base_revision ?? "不可见"} · Policy {create.data.expected_policy_revision ?? "不可见"}</p>
        {create.data.actions.map((action) => <ActionButton key={action.id} action={action}
          onClick={() => setSelection({ action, view: create.data! })} />)}
      </>}
    </QueryState>
    <QueryState query={list}>
      {list.data?.length === 0 && <p>暂无可见草稿。</p>}
      {list.data?.map((draft) => <article key={draft.id}>
        <p>草稿 Revision {draft.revision} · {draft.status} · 人格基准 {String(draft.fields.base_revision)} · Policy {String(draft.fields.policy_revision)}</p>
        <button onClick={() => setDraftId(draft.id)}>审阅草稿 {draft.id}</button>
      </article>)}
      {previous.length > 0 && <button onClick={() => {
        setCursor(previous[previous.length - 1] ?? ""); setPrevious(previous.slice(0, -1));
      }}>上一页草稿</button>}
      {list.meta?.page?.has_more && <button onClick={() => {
        setPrevious([...previous, cursor]); setCursor(list.meta?.page?.next_cursor ?? "");
      }}>下一页草稿</button>}
    </QueryState>
    {draftId && <DraftDetail key={draftId} path={`${path}/${encodeURIComponent(draftId)}`}
      onSelect={setSelection} onClose={() => setDraftId(undefined)} />}
    {selection && <ActionDialog action={selection.action}
      path={selection.view.draft ? `${path}/${encodeURIComponent(selection.view.draft.id)}${selection.action.id === "update" ? "" : `:${selection.action.id}`}` : path}
      onClose={() => { setSelection(undefined); onChanged(); }} onSuccess={onChanged}
      loadLatest={async () => {
        const target = selection.view.draft ? `${path}/${encodeURIComponent(selection.view.draft.id)}` : `${path}/commands`;
        const { data } = await api.request<Context>(target);
        return { revision: data.expected_revision,
          label: `服务器最新草稿 Revision ${data.expected_revision} · Persona ${data.base_revision} · Policy ${data.expected_policy_revision}`,
          fields: { ...data.draft?.fields, current_persona: data.current?.fields ?? null,
            available_actions: data.available_actions } };
      }} bodyBuilder={(fields, reason) => ({
        expected_revision: selection.view.expected_revision, reason_code: reason,
        ...(selection.action.id !== "discard" ? {
          base_revision: selection.action.id === "publish" ? selection.view.draft!.fields.base_revision : selection.view.base_revision,
          expected_policy_revision: selection.action.id === "publish" ? selection.view.draft!.fields.policy_revision : selection.view.expected_policy_revision,
        } : {}),
        ...(["create", "update"].includes(selection.action.id) ? {
          fields: { core: fields.core, traits: fields.traits, narrative: fields.narrative }, source_refs: fields.source_refs ?? [],
        } : {}),
      })} />}
  </section>;
}

function DraftDetail({ path, onSelect, onClose }: {
  path: string; onSelect: (selection: Selection) => void; onClose: () => void;
}) {
  const detail = useQuery<Context>(path);
  const view = detail.data;
  const draft = view?.draft;
  return <section className="panel" aria-label="草稿详情">
    <h3>草稿详情</h3>
    <button onClick={detail.refresh}>刷新草稿详情</button>
    <button onClick={onClose}>关闭草稿详情</button>
    <QueryState query={detail}>
      {view && draft && <>
        <p>状态：{draft.status} · 草稿 Revision {draft.revision}</p>
        <p>已保存基准 Persona {String(draft.fields.base_revision)} · Policy {String(draft.fields.policy_revision)}</p>
        <p>当前 Persona {view.base_revision ?? "不可见"} · Policy {view.expected_policy_revision ?? "不可见"}</p>
        <h4>保存的完整人格</h4><TextData value={draft.fields.content} />
        <h4>来源证据</h4><TextData value={draft.source_refs} />
        <details><summary>对照当前人格</summary><TextData value={view.current?.fields ?? null} /></details>
        {draft.status === "draft" && !view.available_actions.includes("publish") && <p>当前不能发布，请核对权限、基准和证据。保存修改前需明确审阅最新人格与 Policy。</p>}
        {draft.status === "draft" && !view.available_actions.includes("discard") && <p>当前不能丢弃，请核对权限与 Hold 保护。</p>}
        {view.actions.map((action) => <ActionButton key={action.id} action={action}
          onClick={() => onSelect({ action, view })} />)}
      </>}
    </QueryState>
  </section>;
}
