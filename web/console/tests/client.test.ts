import { afterEach, describe, expect, it, vi } from "vitest";
import { ConsoleClient, ApiError, type Transport } from "../src/api/client";
import { createMockTransport } from "../src/mock/server";
import type { Session } from "../src/api/design";
const response = (data: unknown, status = 200) =>
  new Response(
    JSON.stringify({
      data,
      meta: {
        request_id: "req",
        contract_version: "1.0.0",
        as_of: "2026-09-06T00:00:00.123456Z",
        warnings: ["test-warning"],
      },
    }),
    { status },
  );
const failure = (
  status: number,
  code: string,
  kind: string,
  retryable = false,
  retryAfter?: string,
) =>
  new Response(
    JSON.stringify({
      error: { code, details: { kind }, retryable },
      request_id: "req-error",
    }),
    { status, headers: retryAfter ? { "Retry-After": retryAfter } : {} },
  );
afterEach(() => vi.useRealTimers());
describe("Console transport and session protocol", () => {
  it("login JSON has same-origin credentials and Console header, no prior CSRF or idempotency", async () => {
    const mock = createMockTransport();
    const spy = vi.fn<Transport>((...args) => mock(...args));
    const client = new ConsoleClient(spy);
    await client.login("test-only");
    const [path, init] = spy.mock.calls[0]!;
    expect(path).toBe("/console/v1/auth/login");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe('{"key":"test-only"}');
    const h = new Headers(init.headers);
    expect(h.get("X-IMC-Console")).toBe("1");
    expect(h.has("X-IMC-CSRF")).toBe(false);
    expect(h.has("Idempotency-Key")).toBe(false);
    expect(h.has("Authorization")).toBe(false);
  });
  it("restores memory-only CSRF and handles 204 logout", async () => {
    const client = new ConsoleClient(createMockTransport());
    await client.login("mock");
    await client.restore();
    expect(client.session?.csrf_token).toBe("mock-csrf");
    await client.logout();
    expect(client.session).toBeNull();
  });
  it.each(["csrf", "reauth"] as const)(
    "replays %s once with the same action key and payload",
    async (scenario) => {
      const mock = createMockTransport({ scenario });
      const spy = vi.fn<Transport>((...args) => mock(...args));
      const client = new ConsoleClient(spy);
      await client.login("mock");
      client.onReauth = () => client.reauth("mock");
      const key = crypto.randomUUID();
      await client.action("/memory/notes/notes-1", {
        method: "PATCH",
        key,
        body: { expected_revision: 1, fields: { title: "draft" } },
      });
      const writes = spy.mock.calls.filter(([path]) =>
        path.endsWith("/memory/notes/notes-1"),
      );
      expect(writes).toHaveLength(2);
      expect(
        new Set(
          writes.map(([, i]) => new Headers(i.headers).get("Idempotency-Key")),
        ),
      ).toEqual(new Set([key]));
      expect(writes[0]![1].body).toBe(writes[1]![1].body);
    },
  );
  it("double-click single-flight issues only one business request", async () => {
    let done: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      done = resolve;
    });
    const spy = vi.fn<Transport>(async () => {
      await gate;
      return response({ id: "one" });
    });
    const client = new ConsoleClient(spy);
    const key = crypto.randomUUID();
    const a = client.action("/exports", { method: "POST", body: {}, key });
    const b = client.action("/exports", { method: "POST", body: {}, key });
    done();
    expect((await a).data).toEqual((await b).data);
    expect(spy).toHaveBeenCalledTimes(1);
  });
  it("refresh is user-activity only and single-flight, uses dedicated key", async () => {
    const mock = createMockTransport();
    const spy = vi.fn<Transport>((...args) => mock(...args));
    const client = new ConsoleClient(spy);
    await client.login("mock");
    client.session!.session.idle_expires_at = new Date(
      Date.now() + 1000,
    ).toISOString();
    await client.request("/bootstrap");
    expect(spy.mock.calls.some(([p]) => p.endsWith("/auth/refresh"))).toBe(
      false,
    );
    await Promise.all([
      client.activity(),
      client.activity(),
      client.activity(),
    ]);
    const refresh = spy.mock.calls.filter(([p]) => p.endsWith("/auth/refresh"));
    expect(refresh).toHaveLength(1);
    expect(new Headers(refresh[0]![1].headers).get("Idempotency-Key")).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });
  it("absolute expiry clears data without an authentication loop", async () => {
    const mock = createMockTransport();
    const spy = vi.fn<Transport>((...args) => mock(...args));
    const client = new ConsoleClient(spy);
    await client.login("mock");
    client.session!.session.expires_at = "2000-01-01T00:00:00.000000Z";
    await expect(client.activity()).rejects.toMatchObject({ status: 401 });
    expect(client.session).toBeNull();
    expect(spy).toHaveBeenCalledTimes(1);
  });
  it("a second csrf_failed stops instead of looping or treating all 403 as logout", async () => {
    const mock = createMockTransport();
    const spy = vi.fn<Transport>((path, init) =>
      path.includes("/memory/")
        ? Promise.resolve(failure(403, "access_denied", "csrf_failed"))
        : mock(path, init),
    );
    const client = new ConsoleClient(spy);
    await client.login("mock");
    await expect(
      client.request("/memory/notes", { method: "POST" }),
    ).rejects.toMatchObject({ kind: "csrf_failed" });
    expect(spy.mock.calls.filter(([p]) => p.includes("/memory/"))).toHaveLength(
      2,
    );
    expect(client.session).not.toBeNull();
  });
  it("does not log out on non-retryable 503 or retry revision conflicts", async () => {
    const mock = createMockTransport();
    const client = new ConsoleClient(mock);
    await client.login("mock");
    client.transport = vi.fn(async () =>
      failure(409, "revision_mismatch", "revision_conflict"),
    );
    await expect(
      client.request("/memory/notes", { method: "POST" }),
    ).rejects.toMatchObject({ code: "revision_mismatch" });
    expect(client.transport).toHaveBeenCalledTimes(1);
    client.transport = vi.fn(async () =>
      failure(503, "not_ready", "maintenance"),
    );
    await expect(client.request("/memory/notes")).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(client.session).not.toBeNull();
    expect(client.transport).toHaveBeenCalledTimes(1);
  });
  it("obeys Retry-After and retryable with bounded retries", async () => {
    vi.useFakeTimers();
    const spy = vi.fn<Transport>(async () =>
      failure(429, "access_denied", "rate_limited", true, "2"),
    );
    const client = new ConsoleClient(spy);
    const pending = client.request("/stats/overview");
    const outcome = expect(pending).rejects.toMatchObject({ status: 429 });
    await vi.advanceTimersByTimeAsync(1999);
    expect(spy).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(spy).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    await outcome;
    expect(spy).toHaveBeenCalledTimes(3);
  });
  it("permissions changing during CSRF recovery prevents replay and increments epoch", async () => {
    const mock = createMockTransport();
    const client = new ConsoleClient(mock);
    await client.login("mock");
    const original = client.epoch;
    const spy = vi.fn<Transport>(async (path) =>
      path.endsWith("/auth/session")
        ? response({
            ...client.session,
            permissions: [],
            grants: { ...client.session!.grants, permissions: [] },
          } satisfies Partial<Session>)
        : failure(403, "access_denied", "csrf_failed"),
    );
    client.transport = spy;
    await expect(
      client.request("/memory/notes", { method: "POST" }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(client.epoch).toBeGreaterThan(original);
    expect(spy.mock.calls.filter(([p]) => p.includes("/memory/"))).toHaveLength(
      1,
    );
  });
  it("does not deliver a late response after session switch", async () => {
    let resolve: (r: Response) => void = () => {};
    const client = new ConsoleClient(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const pending = client.request("/memory/notes");
    client.clear();
    resolve(response([{ id: "private" }]));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
  it("keeps meta warnings, request ID, microseconds, int64 string intact", async () => {
    const client = new ConsoleClient(async () =>
      response({
        count: "900719925474099312345",
        at: "2026-09-06T00:00:00.123456Z",
      }),
    );
    const r = await client.request<{ count: string; at: string }>(
      "/stats/overview",
    );
    expect(r.data.count).toBe("900719925474099312345");
    expect(r.data.at.endsWith("123456Z")).toBe(true);
    expect(r.meta.warnings).toEqual(["test-warning"]);
  });
  it("supports raw byte upload and stream response with credentials", async () => {
    const blob = new Blob(["test"]);
    const spy = vi.fn<Transport>(async () => response({ uploaded: true }));
    const client = new ConsoleClient(spy);
    await client.request("/imports/one/file", {
      method: "PUT",
      raw: blob,
      contentType: "application/x-ndjson",
    });
    expect(spy.mock.calls[0]![1].body).toBe(blob);
    expect(new Headers(spy.mock.calls[0]![1].headers).get("Content-Type")).toBe(
      "application/x-ndjson",
    );
    client.transport = async () => new Response("bytes");
    const r = await client.request<Response>("/exports/one/download", {
      stream: true,
    });
    expect(await r.data.text()).toBe("bytes");
  });
  it("refuses alternate API planes and unsafe relative paths", async () => {
    const client = new ConsoleClient();
    for (const path of ["https://example.com", "//host", "/../v1"])
      await expect(client.request(path)).rejects.toThrow("Console");
  });
});

it("refresh lost response retries the dedicated key before alias expiry", async () => {
  const mock = createMockTransport();
  const client = new ConsoleClient(mock);
  await client.login("mock");
  client.session!.session.idle_expires_at = new Date(
    Date.now() + 1000,
  ).toISOString();
  let lost = true;
  const keys: string[] = [];
  client.transport = async (path, init) => {
    if (path.endsWith("/auth/refresh")) {
      keys.push(new Headers(init.headers).get("Idempotency-Key")!);
      if (lost) {
        lost = false;
        throw new TypeError("test transport failure");
      }
    }
    return mock(path, init);
  };
  await client.activity();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});
