import { Badge } from "./ui";
import type { ProfileSentence } from "./entry-profile-types";

export const profileKinds: Record<string, string> = {
  topic: "常聊话题",
  saying: "群内说法",
  rule: "明确群规",
};
export function ProfileItemView({ value }: { value: ProfileSentence }) {
  if (!value.kind) return <p className="entry-profile-content">{value.text}</p>;
  return (
    <div className="profile-item-content">
      <p>
        <Badge>{profileKinds[value.kind] || value.kind}</Badge>{" "}
        <strong>{value.label}</strong>
      </p>
      {value.meaning && <p>含义：{value.meaning}</p>}
      {value.quotes?.map((quote, index) => (
        <blockquote key={index}>
          {quote.text}
          <footer>
            消息 #{quote.message_id} ·{" "}
            {quote.field === "content"
              ? "消息正文"
              : quote.field === "quote"
                ? "转引原话"
                : "原话归属未记录"}{" "}
            · 原作者：{quote.speaker_name || quote.speaker || "未记录"}
          </footer>
        </blockquote>
      ))}
    </div>
  );
}

export type ProfileEditItem = {
  original_index: number | null;
  kind: string | null;
  label: string;
  meaning: string;
  quotes: { message_id: number; text: string }[];
  text: string;
  original?: ProfileSentence;
};
export type ProfileDraft = {
  expected_version: number;
  content: string;
  items?: ProfileEditItem[];
};
export function editItem(
  item: ProfileSentence,
  index: number,
): ProfileEditItem {
  return {
    original_index: index,
    kind: item.kind || null,
    label: item.label || "",
    meaning: item.meaning || "",
    quotes: (item.quotes || []).map(({ message_id, text }) => ({
      message_id,
      text,
    })),
    text: item.kind ? "" : item.text,
    original: item,
  };
}
export const itemRequest = ({ original: _, ...item }: ProfileEditItem) => item;
export function renderedItem(item: ProfileEditItem): string {
  if (
    item.original &&
    JSON.stringify(itemRequest(item)) ===
      JSON.stringify(itemRequest(editItem(item.original, item.original_index!)))
  )
    return item.original.text.endsWith("\n")
      ? item.original.text
      : item.original.text + "\n";
  if (!item.kind)
    return item.text.endsWith("\n") ? item.text : item.text + "\n";
  return (
    (item.kind === "rule" ? "明确群规／禁忌" : profileKinds[item.kind]) +
    "：" +
    item.label +
    (item.meaning ? "——" + item.meaning : "") +
    (item.quotes.length
      ? "（" +
        item.quotes
          .map((q) => "“" + q.text.trim().replace(/\s+/g, " ") + "”")
          .join("；") +
        "）"
      : "") +
    "。\n"
  );
}
export function profileDraftBudget(draft: ProfileDraft) {
  const content = draft.items
    ? draft.items.map(renderedItem).join("")
    : draft.content.trim();
  const characters = Array.from(content);
  const han = characters.filter((c) => /[\u4e00-\u9fff]/u.test(c)).length;
  return {
    characters: characters.length,
    tokens: Math.max(1, han + Math.floor((characters.length - han + 2) / 3)),
  };
}
export function StructuredProfileEditor({
  items,
  onChange,
  disabled,
  maxItems,
}: {
  items: ProfileEditItem[];
  onChange: (items: ProfileEditItem[]) => void;
  disabled: boolean;
  maxItems: number;
}) {
  const update = (index: number, changes: Partial<ProfileEditItem>) =>
    onChange(
      items.map((item, i) => (i === index ? { ...item, ...changes } : item)),
    );
  return (
    <>
      {items.map((item, index) => (
        <fieldset
          key={index}
          disabled={disabled}
          className="entry-profile-section"
        >
          <legend>条目 {index + 1}</legend>
          {item.kind ? (
            <>
              <label>
                条目 {index + 1} 类型
                <select
                  value={item.kind}
                  onChange={(e) =>
                    update(index, {
                      kind: e.target.value,
                      meaning: e.target.value === "saying" ? item.meaning : "",
                    })
                  }
                >
                  {Object.entries(profileKinds).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                条目 {index + 1} 名称
                <input
                  required
                  maxLength={40}
                  value={item.label}
                  onChange={(e) => update(index, { label: e.target.value })}
                />
              </label>
              {item.kind === "saying" && (
                <label>
                  条目 {index + 1} 含义（可空）
                  <textarea
                    maxLength={80}
                    value={item.meaning}
                    onChange={(e) => update(index, { meaning: e.target.value })}
                  />
                </label>
              )}
              <p className="muted">
                引用需与本入口消息的连续原话一致；原作者由服务端核对。含义可以留空。
              </p>
              {item.quotes.map((quote, q) => (
                <div key={q} className="entry-profile-evidence">
                  <label>
                    条目 {index + 1} 引用 {q + 1} 消息编号
                    <input
                      type="number"
                      min={1}
                      required
                      value={quote.message_id || ""}
                      onChange={(e) =>
                        update(index, {
                          quotes: item.quotes.map((v, i) =>
                            i === q
                              ? { ...v, message_id: Number(e.target.value) }
                              : v,
                          ),
                        })
                      }
                    />
                  </label>
                  <label>
                    条目 {index + 1} 引用 {q + 1} 原话
                    <textarea
                      required
                      maxLength={100}
                      value={quote.text}
                      onChange={(e) =>
                        update(index, {
                          quotes: item.quotes.map((v, i) =>
                            i === q ? { ...v, text: e.target.value } : v,
                          ),
                        })
                      }
                    />
                  </label>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      update(index, {
                        quotes: item.quotes.filter((_, i) => i !== q),
                      })
                    }
                  >
                    移除条目 {index + 1} 引用 {q + 1}
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="secondary"
                disabled={item.quotes.length >= 3}
                onClick={() =>
                  update(index, {
                    quotes: [...item.quotes, { message_id: 0, text: "" }],
                  })
                }
              >
                添加条目 {index + 1} 引用
              </button>
            </>
          ) : (
            <label>
              条目 {index + 1} 旧手写正文
              <textarea
                required
                value={item.text}
                onChange={(e) => update(index, { text: e.target.value })}
              />
            </label>
          )}
          <button
            type="button"
            className="text-button"
            onClick={() => onChange(items.filter((_, i) => i !== index))}
          >
            删除条目 {index + 1}
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        className="secondary"
        disabled={disabled || items.length >= maxItems}
        onClick={() =>
          onChange([
            ...items,
            {
              original_index: null,
              kind: "saying",
              label: "",
              meaning: "",
              quotes: [],
              text: "",
            },
          ])
        }
      >
        添加画像条目
      </button>
    </>
  );
}
