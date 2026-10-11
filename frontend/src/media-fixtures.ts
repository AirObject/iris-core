import type { Media, ImageUsage } from "./types";

export const mediaFixture: Media = {
  id: "image-one",
  kind: "image",
  content_type: "image/png",
  size_bytes: 1024,
  understanding_text: "[图片，未理解]",
  understanding_source: "unprocessed",
  understanding_source_label: "未理解",
  completed_at: null,
  file_url: "/admin/api/media/image-one/file",
};
export const imageUsageFixture: ImageUsage = {
  calls: 5,
  tokens: 120,
  prompt_tokens: 90,
  completion_tokens: 30,
  reasoning_tokens: 10,
  calls_without_usage: 1,
  failures: 3,
  refusals: 1,
  failure_rate: 0.6,
  duration_ms: 2500,
  p50_ms: 400,
  p95_ms: 900,
  max_ms: 1000,
  timeouts: 2,
};
