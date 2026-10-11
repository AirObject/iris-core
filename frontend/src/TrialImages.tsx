import { useEffect, useRef, useState } from "react";
import { api, ApiError, json } from "./api";
import { imageTypes, mediaSize } from "./Media";
import type { Media } from "./types";

type DraftImage = { key: string; file: File; url: string };
const maxBytes = 10 * 1024 * 1024;
function readImage(file: File, signal: AbortSignal): Promise<string> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const abort = () => reader.abort();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.onerror = () =>
      reject(
        new ApiError(
          "image_read_failed",
          `无法读取图片 ${file.name}，请重新选择`,
          0,
        ),
      );
    reader.onabort = () => reject(new DOMException("Aborted", "AbortError"));
    reader.onloadend = () => signal.removeEventListener("abort", abort);
    signal.addEventListener("abort", abort, { once: true });
    reader.readAsDataURL(file);
  });
}

export function useTrialImages() {
  const [images, setImages] = useState<DraftImage[]>([]);
  const current = useRef<DraftImage[]>([]);
  const uploaded = useRef(new Map<string, string>());
  const replace = (items: DraftImage[]) => {
    current.current = items;
    setImages(items);
  };
  useEffect(
    () => () => {
      for (const item of current.current) URL.revokeObjectURL(item.url);
      uploaded.current.clear();
    },
    [],
  );
  function add(files: File[]) {
    if (current.current.length + files.length > 100)
      throw new ApiError("too_many_images", "每条消息最多发送 100 张图片", 0);
    for (const file of files) {
      if (!imageTypes.includes(file.type))
        throw new ApiError(
          "unsupported_media_type",
          `${file.name}：仅支持 PNG、JPEG、GIF、WebP 图片`,
          0,
        );
      if (file.size > maxBytes)
        throw new ApiError(
          "media_too_large",
          `${file.name}：图片超过单文件 10 MiB 上限`,
          413,
        );
      if (!file.size)
        throw new ApiError("empty_image", `${file.name}：图片文件为空`, 0);
    }
    replace([
      ...current.current,
      ...files.map((file) => ({
        file,
        key: crypto.randomUUID(),
        url: URL.createObjectURL(file),
      })),
    ]);
  }
  function remove(key: string) {
    const item = current.current.find((image) => image.key === key);
    if (item) URL.revokeObjectURL(item.url);
    uploaded.current.delete(key);
    replace(current.current.filter((image) => image.key !== key));
  }
  function clear() {
    for (const item of current.current) URL.revokeObjectURL(item.url);
    uploaded.current.clear();
    replace([]);
  }
  async function upload(
    signal: AbortSignal,
    progress: (value: string) => void,
  ) {
    const ids: string[] = [];
    const items = current.current;
    for (const [index, item] of items.entries()) {
      signal.throwIfAborted();
      let id = uploaded.current.get(item.key);
      if (!id) {
        progress(`正在上传图片 ${index + 1}/${items.length}…`);
        const data_base64 = await readImage(item.file, signal);
        signal.throwIfAborted();
        const result = await api<Media>("/media", {
          ...json("POST", { content_type: item.file.type, data_base64 }),
          signal,
        });
        signal.throwIfAborted();
        id = result.id;
        uploaded.current.set(item.key, id);
      }
      ids.push(id);
    }
    return ids;
  }
  return { images, add, remove, clear, upload };
}

export function TrialImagePicker({
  images,
  busy,
  add,
  remove,
}: {
  images: DraftImage[];
  busy: boolean;
  add: (files: File[]) => void;
  remove: (key: string) => void;
}) {
  return (
    <div className="trial-images">
      <label>
        选择图片
        <input
          type="file"
          accept={imageTypes.join(",")}
          multiple
          disabled={busy}
          onChange={(event) => {
            add(Array.from(event.target.files || []));
            event.target.value = "";
          }}
        />
      </label>
      <p className="fine-print">
        也可在消息框粘贴图片。PNG／JPEG／GIF／WebP，每张最多 10
        MiB，可只发送图片。
      </p>
      {!!images.length && (
        <ul className="draft-images" aria-label="待发送图片">
          {images.map((item) => (
            <li key={item.key}>
              <img src={item.url} alt={`待发送图片：${item.file.name}`} />
              <span>
                {item.file.name} · {mediaSize(item.file.size)}
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={() => remove(item.key)}
                aria-label={`移除图片 ${item.file.name}`}
              >
                移除
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
