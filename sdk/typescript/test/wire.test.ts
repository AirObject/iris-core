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

test("rememberClaim carries the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("claim-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.rememberClaim(
      {
        agent_id: "agent-1",
        predicate: "prefers_language",
        value: { language: "zh" },
        evidence: [
          {
            source_type: "observation",
            source_id: "obs-1",
            relation: "supports",
            source_authority: "user_statement",
          },
        ],
      },
      { idempotencyKey: "claim-1", lease_id: "lease-10", lease_epoch: 5 },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/claims:remember$/);
  assert.equal(call(stub.calls, 0).method, "POST");
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "claim-1");
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-10");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 5);
  assert.equal(call(stub.calls, 0).body?.idempotencyKey, undefined);
  assert.equal(call(stub.calls, 0).body?.idempotency_key, undefined);
});

test("correctClaim carries the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("claim-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.correctClaim(
      "claim-1",
      {
        expected_revision: 1,
        reason: "the user corrected the preference",
        mode: "supersede",
        value: { language: "en" },
      },
      { idempotencyKey: "claim-2", lease_id: "lease-11", lease_epoch: 6 },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/claims\/claim-1:correct$/);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "claim-2");
  assert.equal(call(stub.calls, 0).body?.expected_revision, 1);
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-11");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 6);
  assert.equal(call(stub.calls, 0).body?.idempotencyKey, undefined);
});

test("forgetMemory carries the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("memory-forget-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.forgetMemory(
      {
        selector: { kind: "session", session_id: "s-1", space_id: "sp-1" },
        reason: "user requested erasure",
        erase_content: true,
      },
      { idempotencyKey: "forget-1", lease_id: "lease-12", lease_epoch: 7 },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/memory:forget$/);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "forget-1");
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-12");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 7);
  assert.equal(call(stub.calls, 0).body?.idempotencyKey, undefined);
});

test("createArtifact carries the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("artifact-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.createArtifact(
      {
        agent_id: "agent-1",
        storage_kind: "inline",
        media_type: "text/plain",
        content_base64: "aXJpcw==",
      },
      { idempotencyKey: "artifact-1", lease_id: "lease-13", lease_epoch: 8 },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/artifacts$/);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "artifact-1");
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-13");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 8);
  assert.equal(call(stub.calls, 0).body?.idempotencyKey, undefined);
});

test("transitionEpisode carries target and the lease proof on the wire", async () => {
  const stub = stubFetch([await readFixture("episode-view.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.transitionEpisode(
      "episode-1",
      "seal",
      { expected_revision: 1, reason: "the session is stable" },
      { idempotencyKey: "episode-1", lease_id: "lease-14", lease_epoch: 9 },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/episodes\/episode-1:transition$/);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "episode-1");
  // The transition target rides the BODY — there is no query parameter for it.
  assert.equal(call(stub.calls, 0).body?.target, "seal");
  assert.equal(call(stub.calls, 0).body?.expected_revision, 1);
  assert.equal(call(stub.calls, 0).body?.lease_id, "lease-14");
  assert.equal(call(stub.calls, 0).body?.lease_epoch, 9);
  assert.equal(call(stub.calls, 0).body?.idempotencyKey, undefined);
});
