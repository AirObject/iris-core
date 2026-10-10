import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Trial from "./Trial";
import { setCSRF } from "./api";
import { mediaFixture } from "./media-fixtures";
import { personaSnapshotFixture } from "./persona-fixtures";
import { response } from "./lifecycle-fixtures";
import type { Media, Message } from "./types";

const entry = {
  id: "trial-a",
  name: "试用 A",
  kind: "group",
  pace: "realtime",
};
const png = (name = "cat.png") =>
  new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], name, {
    type: "image/png",
  });
let requests: { url: string; init?: RequestInit; body: any }[];
let messages: Message[];
let uploadError: { message: string; status: number } | null;
let messageError: boolean;
let delayUpload: (() => Promise<Response>) | null;
let media: Media;
let sequence: number;
const createObjectURL = vi.fn();
const revokeObjectURL = vi.fn();
beforeEach(() => {
  requests = [];
  messages = [];
  uploadError = null;
  messageError = false;
  delayUpload = null;
  media = { ...mediaFixture };
  sequence = 0;
  localStorage.clear();
  setCSRF("trial-media-csrf");
  createObjectURL
    .mockReset()
    .mockImplementation(() => `blob:preview-${++sequence}`);
  revokeObjectURL.mockReset();
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url === "/admin/api/trial")
        return response({
          entries: [entry, { ...entry, id: "trial-b", name: "试用 B" }],
          speakers: [{ id: "user", name: "我", is_default: true }],
          role_name: "Iris",
        });
      if (url === "/admin/api/media") {
        if (delayUpload) return delayUpload();
        if (uploadError)
          return response(
            {
              error: {
                code: "media_unavailable",
                message: uploadError.message,
              },
            },
            uploadError.status,
          );
        return response(
          {
            ...media,
            id: `uploaded-${requests.filter((r) => r.url === url).length}`,
          },
          201,
        );
      }
      if (url.endsWith("/messages")) {
        if (messageError) throw new TypeError("connection lost");
        messages = [
          {
            id: 1,
            content: body.content,
            kind: "message",
            learning_state: "pending",
            sender_name: "我",
            received_at: "2026-10-10T08:00:00Z",
            media: body.media_ids.map((id: string) => ({ ...media, id })),
          },
        ];
        return response({ message_id: 1 }, 201);
      }
      if (url.endsWith("/prepare"))
        return response({
          memories: [],
          hints: [],
          persona: {},
          goals: [],
          state: {},
          recall_id: "recall-test",
        });
      if (url.includes("/trial/entries/"))
        return response({
          entry,
          messages,
          has_older: false,
          recent_memories: [],
          goals: [],
          state: {},
          persona: {},
        });
      if (url === "/admin/api/persona") return response(personaSnapshotFixture);
      if (url === "/admin/api/state") return response({});
      if (url.includes("/notifications?"))
        return response({ items: [], total: 0 });
      throw new Error(`Unexpected request ${url}`);
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCSRF("");
});
const uploads = () => requests.filter((r) => r.url === "/admin/api/media");
const sends = () => requests.filter((r) => r.url.endsWith("/messages"));
async function show() {
  const view = render(
    <Trial status={null} refreshStatus={vi.fn()} openMemory={vi.fn()} />,
  );
  await screen.findByLabelText("选择图片");
  return view;
}
const choose = (files: File[]) =>
  fireEvent.change(screen.getByLabelText("选择图片"), { target: { files } });
const send = () =>
  userEvent.click(screen.getByRole("button", { name: "发送消息" }));

test("图片预览可移除；只发图片时先上传再带媒体 ID 发送，复用会话及 CSRF", async () => {
  await show();
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  choose([png()]);
  expect(
    screen.getByRole("img", { name: "待发送图片：cat.png" }),
  ).toHaveAttribute("src", "blob:preview-1");
  expect(uploads()).toHaveLength(0);
  await userEvent.click(
    screen.getByRole("button", { name: "移除图片 cat.png" }),
  );
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1");
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  choose([png()]);
  await send();
  await waitFor(() => expect(sends()).toHaveLength(1));
  expect(uploads()[0].body).toEqual({
    content_type: "image/png",
    data_base64: "iVBORw0KGgo=",
  });
  expect(sends()[0].body).toMatchObject({
    content: "",
    media_ids: ["uploaded-1"],
    speaker_id: "user",
  });
  expect(requests.indexOf(uploads()[0])).toBeLessThan(
    requests.indexOf(sends()[0]),
  );
  for (const request of [uploads()[0], sends()[0]])
    expect(request.init).toMatchObject({
      credentials: "same-origin",
      headers: {
        "X-Iris-CSRF": "trial-media-csrf",
        "Content-Type": "application/json; charset=utf-8",
      },
    });
  expect(await screen.findByText(/消息已接收/)).toBeVisible();
  expect(screen.queryByLabelText("待发送图片")).not.toBeInTheDocument();
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-2");
});

test("支持粘贴图片并和文字、多张图片按选择顺序发送", async () => {
  await show();
  choose([png("first.png")]);
  fireEvent.paste(screen.getByLabelText("消息内容"), {
    clipboardData: { files: [png("pasted.png")] },
  });
  expect(
    screen.getByRole("img", { name: "待发送图片：pasted.png" }),
  ).toBeVisible();
  await userEvent.type(screen.getByLabelText("消息内容"), "今天看到的猫");
  await send();
  await waitFor(() => expect(sends()).toHaveLength(1));
  expect(sends()[0].body).toMatchObject({
    content: "今天看到的猫",
    media_ids: ["uploaded-1", "uploaded-2"],
  });
});

test.each([
  [
    () => new File(["svg"], "bad.svg", { type: "image/svg+xml" }),
    "仅支持 PNG、JPEG、GIF、WebP",
  ],
  [
    () => Object.defineProperty(png(), "size", { value: 10 * 1024 * 1024 + 1 }),
    "超过单文件 10 MiB",
  ],
  [() => new File([], "empty.png", { type: "image/png" }), "图片文件为空"],
])("类型、大小或空文件错误在上传前给出原因", async (file, error) => {
  await show();
  choose([file()]);
  expect(screen.getByRole("alert")).toHaveTextContent(error);
  expect(screen.queryByLabelText("待发送图片")).not.toBeInTheDocument();
  expect(uploads()).toHaveLength(0);
});

test.each([
  [413, "图片上传请求超过 14 MiB"],
  [503, "媒体存储暂时不可用"],
  [400, "媒体类型不支持或与文件头不符"],
])("上传失败 %s 保留图片和正文，不发送无图消息", async (status, message) => {
  await show();
  choose([png()]);
  await userEvent.type(screen.getByLabelText("消息内容"), "草稿");
  uploadError = { status, message };
  await send();
  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(screen.getByLabelText("消息内容")).toHaveValue("草稿");
  expect(
    screen.getByRole("img", { name: "待发送图片：cat.png" }),
  ).toBeVisible();
  expect(sends()).toHaveLength(0);
  uploadError = null;
  await send();
  expect(await screen.findByText(/消息已接收/)).toBeVisible();
});

test("消息回执丢失重试复用上传 ID 与去重键，移除图片后改用新键", async () => {
  await show();
  choose([png()]);
  await userEvent.type(screen.getByLabelText("消息内容"), "正文");
  messageError = true;
  await send();
  await screen.findByRole("alert");
  await send();
  await waitFor(() => expect(sends()).toHaveLength(2));
  expect(uploads()).toHaveLength(1);
  expect(sends()[0].body).toEqual(sends()[1].body);
  await userEvent.click(
    screen.getByRole("button", { name: "移除图片 cat.png" }),
  );
  messageError = false;
  await send();
  await waitFor(() => expect(sends()).toHaveLength(3));
  expect(sends()[2].body.media_ids).toEqual([]);
  expect(sends()[2].body.dedupe_key).not.toBe(sends()[0].body.dedupe_key);
});

test("上传中禁用重复提交，离开入口中止后续发送并释放预览", async () => {
  let finish!: (value: Response) => void;
  delayUpload = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  await show();
  choose([png()]);
  await send();
  await waitFor(() => expect(uploads()).toHaveLength(1));
  expect(screen.getByRole("button", { name: "发送消息" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "移除图片 cat.png" }),
  ).toBeDisabled();
  await userEvent.selectOptions(screen.getByLabelText("当前入口"), "trial-b");
  expect(uploads()[0].init?.signal?.aborted).toBe(true);
  await act(async () => finish(response(mediaFixture, 201)));
  expect(sends()).toHaveLength(0);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1");
});

test("消息轮询更新理解结果，不调用回复准备或产生召回", async () => {
  vi.useFakeTimers();
  messages = [
    {
      id: 1,
      content: "图片",
      kind: "message",
      learning_state: "batched",
      received_at: "2026-10-10T08:00:00Z",
      media: [{ ...mediaFixture }],
    },
  ];
  render(<Trial status={null} refreshStatus={vi.fn()} openMemory={vi.fn()} />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(screen.getByText("等待理解／理解中")).toBeVisible();
  messages[0] = {
    ...messages[0],
    media: [
      {
        ...mediaFixture,
        understanding_source: "system",
        understanding_source_label: "本系统理解",
        understanding_text: "窗边有猫",
        completed_at: "2026-10-10T08:01:00Z",
      },
    ],
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2100);
  });
  expect(screen.getByText("窗边有猫")).toBeVisible();
  expect(screen.queryByText("等待理解／理解中")).not.toBeInTheDocument();
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test("多图中途失败只重传未成功的图片，成功前不发送消息", async () => {
  await show();
  choose([png("first.png"), png("second.png")]);
  let attempt = 0;
  delayUpload = async () => {
    attempt += 1;
    if (attempt === 2)
      return response({ error: { message: "第二张上传失败" } }, 503);
    return response(
      { ...mediaFixture, id: attempt === 1 ? "first-upload" : "second-upload" },
      201,
    );
  };
  await send();
  expect(await screen.findByRole("alert")).toHaveTextContent("第二张上传失败");
  expect(sends()).toHaveLength(0);
  await send();
  await waitFor(() => expect(sends()).toHaveLength(1));
  expect(uploads()).toHaveLength(3);
  expect(sends()[0].body.media_ids).toEqual(["first-upload", "second-upload"]);
});
