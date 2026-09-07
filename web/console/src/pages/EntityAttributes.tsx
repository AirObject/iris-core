import type { Accepted, Fields, Resource } from "../api/design";
import { cas } from "../api/design";
import { ActionDialog, QueryState, TextData, useQuery } from "../components/core";

export function EntityAttributes({ path }: { path: string }) {
  const query = useQuery<Resource>(path);
  return <section>
    <h3>当前属性与冲突</h3>
    <p>同级的不同值会保留为冲突，原值继续生效。</p>
    <QueryState query={query}>
      {query.data && <TextData value={query.data.fields.identity_attributes} />}
    </QueryState>
  </section>;
}

export function EntityAttributeDialog(props: Parameters<typeof ActionDialog>[0]) {
  const query = useQuery<Resource>(props.path);
  if (!query.data) return <QueryState query={query}><button onClick={props.onClose}>关闭属性编辑</button></QueryState>;
  const resource = query.data;
  return <ActionDialog {...props} resource={resource}
    bodyBuilder={(fields, reason) => ({
      ...cas(resource),
      expected_attributes_version: resource.fields.attributes_version,
      fields, reason_code: reason,
    })}
    onSuccess={(result) => {
      const data = result.data as Accepted & { fields?: Fields };
      const outcome = String(data.fields?.attribute_outcome ?? "");
      const labels: Record<string, string> = {
        supersede: "属性已记录并生效", ignored: "保留原属性，未覆盖", coexist: "冲突已记录，原值继续生效",
      };
      props.onSuccess({ ...result, data: { ...data, canonical_status: labels[outcome] ?? "已提交" } });
    }} />;
}
