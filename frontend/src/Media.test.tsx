import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { MessageMedia } from "./Media";
import { SourceView } from "./Memory";
import { mediaFixture } from "./media-fixtures";
import type { Media, Message } from "./types";

test.each([
  ["host", "宿主提供", "窗边有一只猫。", "2026-10-10T08:00:00Z", "宿主提供"],
  [
    "system",
    "本系统理解",
    "桌上有一本书。",
    "2026-10-10T08:00:00Z",
    "本系统理解",
  ],
  ["refused", "被拒绝", "敏感信息无法访问", "2026-10-10T08:00:00Z", "被拒绝"],
  [
    "unprocessed",
    "未理解",
    "[图片，未理解]",
    "2026-10-10T08:00:00Z",
    "理解失败",
  ],
  ["unprocessed", "未理解", "[图片，未理解]", null, "等待理解／理解中"],
] as const)(
  "按来源和完成时间展示 %s，不把完成等同成功",
  (source, label, text, completed, expected) => {
    render(
      <MessageMedia
        media={[
          {
            ...mediaFixture,
            understanding_source: source,
            understanding_source_label: label,
            understanding_text: text,
            completed_at: completed,
          },
        ]}
      />,
    );
    expect(screen.getByText(expected)).toBeVisible();
    expect(screen.getByText(text)).toBeVisible();
    expect(
      screen.getByRole("link", { name: "查看图片 1 原图" }),
    ).toHaveAttribute("href", mediaFixture.file_url);
    expect(screen.getByRole("img", { name: "图片 1" })).toHaveAttribute(
      "src",
      mediaFixture.file_url,
    );
  },
);

test("原图读取失败保留说明，音视频为附件，不假装图片正在理解", () => {
  render(
    <MessageMedia
      media={[
        mediaFixture,
        {
          ...mediaFixture,
          id: "audio-one",
          kind: "audio",
          content_type: "audio/ogg",
          file_url: "/admin/api/media/audio-one/file",
          understanding_text: "[音频，未理解]",
        },
      ]}
    />,
  );
  fireEvent.error(screen.getByRole("img"));
  expect(screen.getByText("图片不可用或已清理")).toBeVisible();
  expect(screen.getByRole("link", { name: "查看音频附件 2" })).toHaveAttribute(
    "href",
    "/admin/api/media/audio-one/file",
  );
  expect(screen.getAllByText("等待理解／理解中")).toHaveLength(1);
});

test("来源及前后文显示媒体，说明文本按纯文本展示", async () => {
  const media: Media = {
    ...mediaFixture,
    understanding_source: "host",
    understanding_source_label: "宿主提供",
    understanding_text: "<b>宿主原文</b>",
  };
  const message: Message = {
    id: 9,
    kind: "message",
    content: "看看图片",
    sender_name: "小林",
    learning_state: "learned",
    received_at: "2026-10-10T08:00:00Z",
    media: [media],
  };
  render(
    <SourceView
      source={{
        id: 1,
        kind: "message",
        message_id: 9,
        message,
        context: [message],
      }}
      openMemory={() => {}}
    />,
  );
  await userEvent.click(screen.getByText("来源消息 · 小林"));
  expect(
    screen.getAllByText("<b>宿主原文</b>", {
      selector: ".media-description",
    })[0],
  ).toBeVisible();
  await userEvent.click(screen.getByText("查看前后文"));
  expect(screen.getAllByRole("img", { name: "图片 1" })).toHaveLength(2);
  const context = document.querySelector(".context-message")! as HTMLElement;
  expect(
    within(context).getByText("<b>宿主原文</b>").querySelector("b"),
  ).toBeNull();
});
