import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api, ConsoleClient, type Transport } from "../src/api/client";
import {
  cas,
  convertUnits,
  uniqueById,
  type Bootstrap,
  type ImportReport,
  type Operation,
  type ProviderView,
  type Resource,
} from "../src/api/design";
import {
  ActionButton,
  ActionDialog,
  Environment,
  TextData,
} from "../src/components/core";
import { ForgetDialog } from "../src/pages/Memory";
import { OperationPanel, pollingDelay } from "../src/components/Operation";
import { canActivate } from "../src/pages/Configuration";
import { reportUsable } from "../src/pages/Transfers";
import { action, makeResource, resourceTypes } from "../src/mock/fixtures";
import { createMockTransport } from "../src/mock/server";
const bootstrap: Bootstrap = {
  contract_version: "1.0.0",
  permissions: ["memory.read", "memory.write", "memory.forget"],
  modules: ["memory"],
  read_only: false,
  maintenance: false,
  upload_limits: {
    file_bytes: "10",
    record_bytes: "10",
    records: "10",
    json_depth: 10,
  },
  display_timezone: "UTC",
  pending_restart: false,
  import_in_progress: false,
};
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <Environment.Provider value={{ bootstrap, epoch: api.epoch }}>
    {children}
  </Environment.Provider>
);
afterEach(() => {
  cleanup();
  api.clear();
  vi.useRealTimers();
});
describe("domain safeguards", () => {
  it("preserves decimal precision and rejects lossy conversions", () => {
    expect(convertUnits("900719925474099312345", "1000000", "1")).toBe(
      "900719925474099312345000000",
    );
    expect(() => convertUnits("1", "1", "1000")).toThrow();
    expect(
      uniqueById([
        { id: "a", v: 1 },
        { id: "a", v: 2 },
      ]),
    ).toEqual([{ id: "a", v: 2 }]);
    expect(cas({ version_token: "opaque", revision: 1 })).toEqual({
      version_token: "opaque",
    });
  });
  it("does not offer arbitrary updates on append-only, projection, or event resources", () => {
    for (const collection of [
      "observations",
      "claims",
      "relations",
      "artifacts",
      "cognitive-events",
    ])
      expect(
        resourceTypes
          .find((t) => t.collection === collection)!
          .actions.some((a) => a.id === "update"),
      ).toBe(false);
    for (const collection of [
      "vector",
      "fts",
      "profile",
      "graph",
      "recent-context",
    ])
      expect(
        resourceTypes.find((t) => t.collection === collection)!.actions,
      ).toEqual([]);
  });
  it("renders raw body as text", () => {
    render(<TextData value={"<img src=x onerror=alert(1)>"} />);
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeTruthy();
  });
  it("available_actions and blocked_actions override permission; read-only disables writes", () => {
    const resource = {
      available_actions: ["update"],
      blocked_actions: [{ action: "update", reason: "Hold" }],
    };
    const { rerender } = render(
      <ActionButton
        action={action("update", "修改")}
        resource={resource}
        onClick={() => {}}
      />,
      { wrapper },
    );
    expect(screen.getByRole("button").hasAttribute("disabled")).toBe(true);
    rerender(
      <Environment.Provider
        value={{ bootstrap: { ...bootstrap, read_only: true }, epoch: 0 }}
      >
        <ActionButton action={action("update", "修改")} onClick={() => {}} />
      </Environment.Provider>,
    );
    expect(screen.getByRole("button").hasAttribute("disabled")).toBe(true);
  });
  it("requires current report hash and rejects dirty or expired report", () => {
    const report: ImportReport = {
      report_id: "id",
      report_hash: "hash",
      expires_at: new Date(Date.now() + 60000).toISOString(),
      can_commit: true,
      counts: {},
      warnings: [],
      decisions: [],
    };
    expect(reportUsable(report, false)).toBe(true);
    expect(reportUsable(report, true)).toBe(false);
    expect(reportUsable({ ...report, expires_at: "2000-01-01" }, false)).toBe(
      false,
    );
  });
  it("provider activation requires non-dirty, recent, matching-dimension successful probe", () => {
    const p: ProviderView = {
      id: "p",
      revision: 1,
      status: "probed",
      fields: { dimension: 32 },
      available_actions: ["activate"],
      blocked_actions: [],
      probe: {
        ok: true,
        dimension_observed: 32,
        expires_at: new Date(Date.now() + 60000).toISOString(),
      },
      side_effects: {},
      generation: "2",
      serving_generation: "1",
    };
    expect(canActivate(p, false)).toBe(true);
    expect(canActivate(p, true)).toBe(false);
    expect(
      canActivate(
        { ...p, probe: { ...p.probe!, dimension_observed: 64 } },
        false,
      ),
    ).toBe(false);
    expect(canActivate({ ...p, probe: undefined }, false)).toBe(false);
    expect(
      canActivate(
        { ...p, probe: { ...p.probe!, expires_at: "2000-01-01" } },
        false,
      ),
    ).toBe(false);
  });
  it("retains CAS conflict draft and fetches newest data without overwriting", async () => {
    HTMLDialogElement.prototype.showModal = function () {
      this.open = true;
    };
    api.transport = createMockTransport({ scenario: "conflict" });
    await api.login("mock");
    const resource = makeResource(
      resourceTypes.find((r) => r.collection === "notes")!,
      "notes-1",
      "original",
    );
    render(
      <ActionDialog
        action={action("update", "编辑", [
          { key: "title", label: "标题", type: "string" },
        ])}
        path="/memory/notes/notes-1"
        resource={resource}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
      { wrapper },
    );
    await userEvent.clear(screen.getByLabelText("标题"));
    await userEvent.type(screen.getByLabelText("标题"), "retained draft");
    await userEvent.selectOptions(
      screen.getByLabelText("操作原因"),
      "data_correction",
    );
    await userEvent.click(screen.getByText("确认提交"));
    await screen.findByText("服务器最新 Revision 1");
    expect((screen.getByLabelText("标题") as HTMLInputElement).value).toBe(
      "retained draft",
    );
    expect(screen.getByText("确认提交").hasAttribute("disabled")).toBe(true);
  });
  it("Forget requires preview, confirmation, and only submits preview identifiers and reason", async () => {
    HTMLDialogElement.prototype.showModal = function () {
      this.open = true;
    };
    const transport = createMockTransport();
    const spy = vi.fn<Transport>((...a) => transport(...a));
    api.transport = spy;
    await api.login("mock");
    const resource = makeResource(resourceTypes[3]!, "notes-1", "test");
    render(
      <ForgetDialog
        targets={[resource]}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
      { wrapper },
    );
    await waitFor(() =>
      expect(screen.getByText("data_correction")).toBeTruthy(),
    );
    await userEvent.selectOptions(
      screen.getByLabelText("操作原因"),
      "data_correction",
    );
    await userEvent.click(screen.getByText("生成预览"));
    await screen.findByText("按预览提交 Forget");
    expect(screen.getByText("按预览提交 Forget").hasAttribute("disabled")).toBe(
      true,
    );
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByText("按预览提交 Forget"));
    await waitFor(() =>
      expect(spy.mock.calls.some(([p]) => p.endsWith(":forget"))).toBe(true),
    );
    const request = spy.mock.calls.find(([p]) => p.endsWith(":forget"))!;
    expect(Object.keys(JSON.parse(String(request[1].body))).sort()).toEqual([
      "preview_hash",
      "preview_id",
      "reason_code",
    ]);
  });
  it("preview_stale discards confirmation and requires another preview", async () => {
    HTMLDialogElement.prototype.showModal = function () {
      this.open = true;
    };
    api.transport = createMockTransport({ scenario: "preview_stale" });
    await api.login("mock");
    render(
      <ForgetDialog
        targets={[makeResource(resourceTypes[3]!, "notes-1", "test")]}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
      { wrapper },
    );
    await waitFor(() =>
      expect(screen.getByText("data_correction")).toBeTruthy(),
    );
    await userEvent.selectOptions(
      screen.getByLabelText("操作原因"),
      "data_correction",
    );
    await userEvent.click(screen.getByText("生成预览"));
    await screen.findByText("按预览提交 Forget");
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByText("按预览提交 Forget"));
    await screen.findByText("生成预览");
    expect(screen.queryByText("按预览提交 Forget")).toBeNull();
  });
  it("partial import and blocked operations have truthful states; terminal polling stops", async () => {
    vi.useFakeTimers();
    const initial: Operation = {
      id: "op",
      kind: "import",
      status: "cancelled_partial",
      phase: null,
      progress: { processed: "100", total: "250", unit: "records" },
      cancellable: false,
      problems_count: 0,
      result_ref: null,
    };
    api.transport = vi.fn();
    render(<OperationPanel initial={initial} />, { wrapper });
    await vi.advanceTimersByTimeAsync(20000);
    expect(api.transport).not.toHaveBeenCalled();
    expect(screen.getByText(/已提交内容仍保留/)).toBeTruthy();
    expect(pollingDelay(10)).toBe(10000);
  });
  it("mock import invalidates report when mapping changes and returns partial commit honestly", async () => {
    const client = new ConsoleClient(
      createMockTransport({ scenario: "partial" }),
    );
    await client.login("mock");
    const view = await client.request<Resource>("/imports", {
      method: "POST",
      body: { format_id: "imc-data/v1" },
    });
    const base = `/imports/${view.data.id}`;
    await client.request(`${base}:validate`, { method: "POST", body: {} });
    const report = await client.request<ImportReport>(`${base}/report`);
    const result = await client.request<{ operation: Operation }>(
      `${base}:commit`,
      {
        method: "POST",
        body: {
          report_id: report.data.report_id,
          report_hash: report.data.report_hash,
        },
      },
    );
    expect(result.data.operation.status).toBe("cancelled_partial");
    await client.request(`${base}/mapping`, {
      method: "PATCH",
      body: { expected_revision: 1, mapping: {} },
    });
    await expect(
      client.request(`${base}:commit`, {
        method: "POST",
        body: {
          report_id: report.data.report_id,
          report_hash: report.data.report_hash,
        },
      }),
    ).rejects.toMatchObject({ kind: "report_stale" });
  });
});

it("switching from validation operation to partial commit replaces the displayed status", () => {
  const initial: Operation = {
    id: "validate",
    kind: "import_validate",
    status: "completed",
    phase: null,
    progress: null,
    cancellable: false,
    problems_count: 0,
    result_ref: null,
  };
  const { rerender } = render(<OperationPanel initial={initial} />, {
    wrapper,
  });
  rerender(
    <OperationPanel
      initial={{
        ...initial,
        id: "commit",
        kind: "import_commit",
        status: "cancelled_partial",
      }}
    />,
  );
  expect(screen.getByText("cancelled_partial")).toBeTruthy();
  expect(screen.queryByText("completed", { exact: true })).toBeNull();
});

it("Forget commits the fixed snapshot and subsequent reads do not return the removed body", async () => {
  const client = new ConsoleClient(createMockTransport());
  await client.login("mock");
  const preview = await client.request<import("../src/api/design").Preview>(
    "/memory:forget-preview",
    {
      method: "POST",
      body: {
        targets: [
          { resource_type: "note", id: "notes-1", expected_revision: 1 },
        ],
        mode: "soft",
        reason_code: "privacy_request",
      },
    },
  );
  await client.request("/memory:forget", {
    method: "POST",
    body: {
      preview_id: preview.data.preview_id,
      preview_hash: preview.data.preview_hash,
      reason_code: "privacy_request",
    },
  });
  const list = await client.request<Resource[]>("/memory/notes");
  expect(list.data.some((r) => r.id === "notes-1")).toBe(false);
  await expect(client.request("/memory/notes/notes-1")).rejects.toMatchObject({
    status: 404,
  });
});
