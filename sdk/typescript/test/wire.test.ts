import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { AsyncIrisMemoryClient } from "../src/index.js";

const fixtureRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../schemas/fixtures");

interface RecordedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: Record<string, unknown> | null;
}

/**
 * Round-4 wire assertions: the §25.3 lease proof must actually reach the
 * request body on note actions, task/step transitions and event ACKs — the
 * typed input is not enough if the hand-built body silently drops it.
 */
function stubFetch(responses: unknown[]): { calls: RecordedCall[]; restore: () => void } {
  const calls: RecordedCall[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: String(init?.method ?? "GET"),
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body === undefined ? null : (JSON.parse(String(init.body)) as Record<string, unknown>),
    });
    return new Response(JSON.stringify(responses[calls.length - 1] ?? {}), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return { calls, restore: () => void (globalThis.fetch = original) };
}

async function readFixture(name: string): Promise<unknown> {
  return JSON.parse(await readFile(resolve(fixtureRoot, "valid", name), "utf8")) as unknown;
}

function call(calls: readonly RecordedCall[], index: number): RecordedCall {
  const recorded = calls[index];
  assert.ok(recorded !== undefined, `expected recorded call ${index}`);
  return recorded;
}

test("noteAction carries the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("note-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.noteAction("note-1", "archive", {
      expected_revision: 1,
      reason: "done",
      idempotencyKey: "k1",
      lease_id: "lease-7",
      lease_epoch: 4,
    });
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/notes\/note-1:archive$/);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "k1");
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-7");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 4);
});

test("task and step transitions carry the lease proof on the wire", async () => {
  const taskView = (await readFixture("task-view.json")) as { steps: unknown[] };
  const stub = stubFetch([taskView, taskView.steps[0], taskView]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.transitionTask("task-1", {
      target: "wait",
      expected_revision: 1,
      reason: "blocked upstream",
      idempotencyKey: "k2",
      lease_id: "lease-8",
      lease_epoch: 9,
    });
    await client.transitionTaskStep("task-1", "step-1", {
      target: "start",
      expected_revision: 1,
      reason: "work begins",
      idempotencyKey: "k3",
      lease_id: "lease-8",
      lease_epoch: 9,
    });
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 2);
  const taskCall = call(stub.calls, 0);
  const stepCall = call(stub.calls, 1);
  assert.match(taskCall.url, /:transition$/);
  assert.equal(taskCall.body?.lease_id, "lease-8");
  assert.equal(taskCall.body?.lease_epoch, 9);
  // The transport-only idempotencyKey must ride the HEADER, never the body.
  assert.equal(taskCall.body?.idempotencyKey, undefined);
  assert.equal(taskCall.headers["Idempotency-Key"], "k2");
  assert.match(stepCall.url, /steps\/step-1:transition$/);
  assert.equal(stepCall.body?.lease_id, "lease-8");
  assert.equal(stepCall.body?.lease_epoch, 9);
  assert.equal(stepCall.headers["Idempotency-Key"], "k3");
});

test("ackCognitiveEvent carries the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("cognitive-event-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.ackCognitiveEvent("event-1", {
      idempotencyKey: "k4",
      lease_id: "lease-9",
      lease_epoch: 2,
    });
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/cognitive-events\/event-1:ack$/);
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-9");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 2);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "k4");
});
