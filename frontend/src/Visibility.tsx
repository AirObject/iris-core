import { useState, type FormEvent } from "react";
import { api, errorText, json } from "./api";
import { Notice } from "./ui";
import type { EntryVisibility, MemoryDetail, Person } from "./types";

export function entryName(id: string, entries: Person[]) {
  const entry = entries.find((item) => item.id === id);
  return entry ? `${entry.name}（${id}）` : id;
}

export function entryVisibilityLabel(
  value: EntryVisibility,
  entries: Person[],
) {
  if (value.visibility === "shared") return "全部入口共享（默认）";
  if (value.visibility === "entry_only") return "仅本入口";
  if (value.visibility === "entries")
    return `指定入口：${value.visible_in.map((id) => entryName(id, entries)).join("、")}`;
  return "未返回，请重新加载";
}

export function memoryVisibilityLabel(
  value: MemoryDetail["visibility"] | undefined,
  entries: Person[],
) {
  if (!value) return "未返回，请重新加载详情";
  if (value.shared) return "全部入口";
  if (!value.visible_in.length) return "仅管理员可见";
  return value.visible_in.map((id) => entryName(id, entries)).join("、");
}

export function EntryVisibilityForm({
  entry,
  entries,
  value,
  busy,
  setBusy,
  onSaved,
}: {
  entry: Person;
  entries: Person[];
  value: EntryVisibility;
  busy: boolean;
  setBusy: (value: boolean) => void;
  onSaved: () => void;
}) {
  const [mode, setMode] = useState(value.visibility);
  const [selected, setSelected] = useState(
    value.visible_in.filter((id) => id !== entry.id),
  );
  const [error, setError] = useState("");

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api(
        `/entries/${encodeURIComponent(entry.id)}/visibility`,
        json("PATCH", {
          visibility: mode,
          visible_in: mode === "entries" ? selected : [],
        }),
      );
      onSaved();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="entry-settings-form entry-visibility-form" onSubmit={save}>
      <h3>记忆可见范围</h3>
      <p className="muted" id="entry-visibility-help">
        这是查询时生效的设置。修改后立即影响该入口来源的全部已有记忆，无需重新学习，可随时改回。
        多个来源或派生依据的限制共同生效，最终范围可在记忆详情查看。
      </p>
      {error && <Notice error>{error}</Notice>}
      <fieldset className="lifecycle-fieldset" disabled={busy}>
        <label>
          记忆可见范围
          <select
            value={mode}
            aria-describedby="entry-visibility-help"
            onChange={(event) =>
              setMode(event.target.value as EntryVisibility["visibility"])
            }
          >
            <option value="shared">全部入口共享（默认）</option>
            <option value="entry_only">仅本入口</option>
            <option value="entries">指定入口</option>
          </select>
        </label>
        {mode === "entries" && (
          <fieldset className="lifecycle-fieldset">
            <legend>选择可见入口（可多选）</legend>
            <label className="check-label">
              <input type="checkbox" checked disabled />
              {entryName(entry.id, [entry])} · 本入口始终包含
            </label>
            {entries
              .filter((item) => item.id !== entry.id)
              .map((item) => (
                <label className="check-label" key={item.id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(item.id)}
                    onChange={(event) => {
                      setSelected(
                        event.target.checked
                          ? [...selected, item.id]
                          : selected.filter((id) => id !== item.id),
                      );
                    }}
                  />
                  {entryName(item.id, entries)}
                </label>
              ))}
            {entries.every((item) => item.id === entry.id) && (
              <p className="muted">暂无其他入口可选。</p>
            )}
          </fieldset>
        )}
        <p className="fine-print">可见范围与下方的节奏、过滤分别保存。</p>
        <button className="primary" disabled={busy}>
          {busy ? "正在保存…" : "保存可见范围"}
        </button>
      </fieldset>
    </form>
  );
}
