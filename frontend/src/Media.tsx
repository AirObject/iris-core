import { useState } from "react";
import { Badge, fullTime } from "./ui";
import type { Media } from "./types";

export const imageTypes = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
];
export const mediaSize = (bytes: number) =>
  bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
    : `${Math.ceil(bytes / 1024)} KiB`;

export function MessageMedia({ media }: { media?: Media[] }) {
  if (!media?.length) return null;
  return (
    <div className="message-media">
      {media.map((item, index) => (
        <MediaItem key={item.id} item={item} number={index + 1} />
      ))}
    </div>
  );
}
function MediaItem({ item, number }: { item: Media; number: number }) {
  const [unavailable, setUnavailable] = useState(false);
  const image = item.kind === "image" && imageTypes.includes(item.content_type);
  const failed =
    image && item.understanding_source === "unprocessed" && !!item.completed_at;
  const waiting =
    image && item.understanding_source === "unprocessed" && !item.completed_at;
  const kind =
    item.kind === "audio" ? "音频" : item.kind === "video" ? "视频" : "文件";
  return (
    <div className="media-card">
      <a
        href={item.file_url}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={
          image ? `查看图片 ${number} 原图` : `查看${kind}附件 ${number}`
        }
      >
        {image ? (
          unavailable ? (
            <span>图片不可用或已清理</span>
          ) : (
            <img
              src={item.file_url}
              alt={`图片 ${number}`}
              loading="lazy"
              onError={() => setUnavailable(true)}
            />
          )
        ) : (
          `${kind}附件 ${number}`
        )}
      </a>
      <small>
        {item.content_type} · {mediaSize(item.size_bytes)}
      </small>
      <div>
        <Badge
          tone={
            failed || item.understanding_source === "refused" ? "danger" : ""
          }
        >
          {item.understanding_source_label}
        </Badge>
      </div>
      {failed && <strong className="danger-text">理解失败</strong>}
      {waiting && (
        <>
          <strong>等待理解／理解中</strong>
          <small>未启用或暂停时也会保持等待。</small>
        </>
      )}
      <p className="media-description">{item.understanding_text}</p>
      {item.completed_at && (
        <small>完成时间：{fullTime(item.completed_at)}</small>
      )}
    </div>
  );
}
