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
