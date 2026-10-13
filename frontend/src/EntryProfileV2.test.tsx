import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EntryProfileHistory } from "./EntryProfileHistory";
import { setCSRF } from "./api";

const saying = {
  kind: "saying",
  label: "翻面",
  meaning: "检查纸背是否渗墨",
  format_version: 2,
  author: "model",
  admin_written: false,
  text: "群内说法：翻面——检查纸背是否渗墨（“翻面看纸背”）。\n",
  basis: [],
  quotes: [
    {
      message_id: 31,
      text: "翻面看纸背",
      field: "quote",
      speaker: "lin",
      speaker_name: "小林",
    },
  ],
};
const rule = {
  kind: "rule",
  label: "不要公开地址",
  meaning: "",
  format_version: 2,
  author: "admin",
  admin_written: true,
  text: "明确群规／禁忌：不要公开地址。\n",
  basis: [],
  quotes: [],
};
let version: any, snapshot: any, writes: any[], failure: number;
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
beforeEach(() => {
  writes = [];
  failure = 0;
  setCSRF("profile-ui-csrf");
  version = {
    version: 1,
    format_version: 2,
    content: saying.text + rule.text,
    status: "published",
    is_current: true,
    source: "regenerate",
    author: "model",
    change_degree: "small",
    generated_at: "2026-10-11T00:00:00Z",
    sentences: [structuredClone(saying), structuredClone(rule)],
    checks: {
      format_version: 2,
      passed: true,
      deterministic: { passed: true },
      quote_validation: { passed: 2, total: 3 },
      quote_validation_items: [
        {
          index: 3,
          passed: 0,
          total: 1,
          quotes: [{ index: 1, passed: false, reason: "quote_not_found" }],
        },
      ],
      checked_sentences: [saying],
      model: {
        reason: "逐项核对",
        sentences: [
          {
            index: 1,
            supported: true,
            meaning_supported: false,
            scene_qualified: true,
            violations: [],
            reason: "说法有依据，含义无依据",
          },
        ],
      },
      removed_meanings: [
        { index: 1, meaning: "查看空气湿度", reason: "材料不支持该含义" },
      ],
      deleted_sentences: [
        {
          index: 3,
          sentence: { label: "拼出的句子" },
          reason: "quote_not_found",
          stage: "deterministic",
        },
      ],
    },
  };
  snapshot = {
    supported: true,
    global_enabled: true,
    settings: {
      current_version: 1,
      enabled: true,
      publish_mode: "check_auto",
      revision: 1,
    },
    current: version,
    pending: null,
    pending_count: 0,
    stale_basis: [],
    possibly_stale: false,
    latest_attempt: null,
    edit_limits: { max_items: 6, max_chars: 500, max_tokens: 500 },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      if (init?.method) {
        writes.push({
          body: JSON.parse(String(init.body)),
          headers: init.headers,
        });
        if (failure)
          return response(
            {
              error: {
                code: "entry_profile_conflict",
                message: "当前版本已变化",
              },
            },
            failure,
          );
        return response({ ...version, version: 2 });
      }
      if (/\/versions\/\d+$/.test(input)) return response(version);
      if (input.includes("/versions?"))
        return response({ items: [version], total: 1 });
      return response(snapshot);
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  setCSRF("");
});
function panel() {
  return render(
    <EntryProfileHistory base="/entries/print/profile" openMemory={vi.fn()} />,
  );
}

test("分类、名称、含义与原话归属分开展示，检查和删含义记录可见", async () => {
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "查看旧版 v1" }),
  );
  expect(await screen.findByText("翻面")).toBeVisible();
  expect(screen.getByText("群内说法")).toBeVisible();
  expect(screen.getByText("明确群规")).toBeVisible();
  expect(screen.getByText("含义：检查纸背是否渗墨")).toBeVisible();
  expect(screen.getByText(/消息 #31 · 转引原话 · 原作者：小林/)).toBeVisible();

  const detail = await screen.findByRole("region", { name: "旧版详情" });
  expect(
    await within(detail).findByText(/引用原话核对：2 \/ 3 条通过/),
  ).toBeVisible();
  expect(
    within(detail).getByText(
      /引用与格式核对：引语不是消息正文或转引原话的连续片段/,
    ),
  ).toBeVisible();
  await userEvent.click(within(detail).getByText("逐条引用核对"));
  expect(
    within(detail).getByText(/候选第 3 项：0 \/ 1 条原话通过/),
  ).toBeVisible();
  expect(within(detail).getByText("已删含义记录")).toBeVisible();
  expect(within(detail).getByText(/没有含义不表示生成失败/)).toBeVisible();
  expect(within(detail).getByText("候选第 3 项")).toBeVisible();
  expect(writes).toEqual([]);
});

test("删条阈值按版本说明，任务摘要不误用旧版 30%", async () => {
  version.checks = {
    ...version.checks,
    passed: false,
    reason: "too_many_deleted",
  };
  snapshot.latest_attempt = {
    id: 5,
    state: "rejected",
    stage: "finished",
    reason: "too_many_deleted",
    version: 1,
  };
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "查看旧版 v1" }),
  );

  expect(
    await screen.findByText("检查累计删除了超过 50% 的模型条目"),
  ).toBeVisible();
  expect(screen.queryByText(/超过 30%/)).not.toBeInTheDocument();
});

test("旧候选、旧发布版均只读，清除标记可见，无审批或回滚请求", async () => {
  version.status = "candidate";
  version.redacted = true;
  version.redaction_reason = "memory_purged";
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "查看旧版 v1" }),
  );
  expect(await screen.findByText(/此旧版本的相关内容已彻底清除/)).toBeVisible();
  expect(screen.getByText("只读历史")).toBeVisible();
  expect(
    screen.queryByRole("button", {
      name: /确认发布|拒绝候选|复制为新版本|回滚/,
    }),
  ).not.toBeInTheDocument();
  expect(writes).toEqual([]);
});

test("无效模型检查输出安全展示，不影响旧版依据阅读", async () => {
  version.checks.reason = "invalid_output";
  version.checks.model = { reason: { invalid: true }, sentences: true };
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "查看旧版 v1" }),
  );
  expect(
    await screen.findByText("模型检查输出格式无效，本版未通过检查。"),
  ).toBeVisible();
  expect(screen.getByText("含义：检查纸背是否渗墨")).toBeVisible();
  expect(writes).toEqual([]);
});
