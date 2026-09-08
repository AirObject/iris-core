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

test("Phase 10 management requests carry Bearer and idempotency headers", async () => {
  const stub = stubFetch([
    { operation_id: "backup-1", kind: "backup", status: "accepted", created_us: 1 },
    { events: [] },
  ]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local", {
      bearerToken: "sdk-test-bearer",
    });
    await client.createBackup({ reason: "verification" }, { idempotencyKey: "backup-1" });
    await client.listAuditEvents("security review", 0, 25);
  } finally {
    stub.restore();
  }
  const recorded = call(stub.calls, 0);
  assert.equal(recorded.headers.Authorization, "Bearer sdk-test-bearer");
  assert.equal(recorded.headers["Idempotency-Key"], "backup-1");
  assert.deepEqual(recorded.body, { reason: "verification" });
  const audit = call(stub.calls, 1);
  assert.match(audit.url, /reason=security\+review/);
  assert.match(audit.url, /limit=25/);
});

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

test("recall posts the full request scope on the wire", async () => {
  const stub = stubFetch([await readFixture("recall-response.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.recall({
      schema_version: 1,
      request_id: "01a060aa-0000-7000-8000-000000000001",
      scope: { agent_id: "agent-1", space_id: "space-1" },
      actors: [{ provider: "qq", external_id: "user-1" }],
      topic: "language preference",
      purpose: "reply",
      token_budget: 2000,
      deadline_at: "2026-09-02T12:00:01.500000+00:00",
    });
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/recall$/);
  const body = call(stub.calls, 0).body as {
    scope?: { agent_id?: string };
    actors?: ReadonlyArray<{ external_id?: string }>;
  };
  assert.equal(body.scope?.agent_id, "agent-1");
  assert.equal(body.actors?.[0]?.external_id, "user-1");
});

test("reportRecallUsage posts stage ids and idempotency key", async () => {
  const stub = stubFetch([await readFixture("recall-usage-report-response.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.reportRecallUsage(
      "01a060aa-0000-7000-8000-000000000001",
      {
        host_cycle_id: "cycle-1",
        persona_revision: 1,
        returned_candidate_ids: ["cand:0123456789abcdef"],
        host_selected_candidate_ids: ["cand:0123456789abcdef"],
        model_visible_candidate_ids: ["cand:0123456789abcdef"],
        reported_at: "2026-09-02T12:00:02+00:00",
      },
      { idempotencyKey: "usage-1" },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/recall\/[^/]+\/usage$/);
  assert.equal(call(stub.calls, 0).headers["Idempotency-Key"], "usage-1");
  assert.equal(call(stub.calls, 0).body?.host_cycle_id, "cycle-1");
});

test("search posts query and scope on the wire", async () => {
  const stub = stubFetch([await readFixture("search-response.json")]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.search({
      agent_id: "agent-1",
      query: "language preference",
      space_id: "space-1",
      limit: 25,
    });
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 1);
  assert.match(call(stub.calls, 0).url, /\/v1\/search$/);
  assert.equal(call(stub.calls, 0).body?.query, "language preference");
  assert.equal(call(stub.calls, 0).body?.space_id, "space-1");
  assert.equal(call(stub.calls, 0).body?.limit, 25);
});

test("persona mutations carry idempotency keys and exact resource paths", async () => {
  const current = (await readFixture("persona-current-response.json")) as {
    revision: Record<string, unknown>;
  };
  const proposal = await readFixture("persona-proposal-view.json");
  const state = {
    persona_state_id: "state-1",
    revision: 1,
    state: { mood: 0.4 },
    baseline: { mood: 0.0 },
    source_refs: [],
    started_us: 1_700_000_000_000_000,
    expires_us: 1_700_003_600_000_000,
    decay_policy: "expire_to_baseline",
    schema_version: 1,
  };
  const stub = stubFetch([
    current.revision,
    state,
    proposal,
    proposal,
    current.revision,
  ]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.publishPersonaRevision(
      "agent/one",
      { expected_revision: 1, core: {}, traits: {}, narrative: {}, reason: "publish" },
      { idempotencyKey: "persona-1" },
    );
    await client.updatePersonaState(
      "agent/one",
      { expected_revision: 0, state: { mood: 0.4 }, baseline: {}, ttl_us: 1000 },
      { idempotencyKey: "persona-2" },
    );
    await client.createPersonaProposal(
      "agent/one",
      {
        base_revision: 1,
        patch: { traits: { style: "warm" } },
        evidence_refs: [{ resource_type: "persona_state", resource_id: "state-1" }],
        confidence: 0.9,
        generator: "test",
        generator_version: "1",
      },
      { idempotencyKey: "persona-3" },
    );
    await client.reviewPersonaProposal(
      "agent/one",
      "proposal/one",
      "approve",
      "reviewed",
      { idempotencyKey: "persona-4" },
    );
    await client.rollbackPersona(
      "agent/one",
      { target_revision: 1, expected_revision: 2, reason: "rollback" },
      { idempotencyKey: "persona-5" },
    );
  } finally {
    stub.restore();
  }
  assert.equal(stub.calls.length, 5);
  assert.match(call(stub.calls, 0).url, /personas\/agent%2Fone\/revisions$/);
  assert.match(call(stub.calls, 1).url, /personas\/agent%2Fone\/state$/);
  assert.match(call(stub.calls, 2).url, /personas\/agent%2Fone\/evolution-proposals$/);
  assert.match(
    call(stub.calls, 3).url,
    /personas\/agent%2Fone\/evolution-proposals\/proposal%2Fone:approve$/,
  );
  assert.match(call(stub.calls, 4).url, /personas\/agent%2Fone:rollback$/);
  for (const [index, expected] of ["persona-1", "persona-2", "persona-3", "persona-4", "persona-5"].entries()) {
    assert.equal(call(stub.calls, index).headers["Idempotency-Key"], expected);
  }
  assert.equal(call(stub.calls, 3).body?.reason, "reviewed");
});

test("plugin negotiation and Entity Profile use public wire fields", async () => {
  const stub = stubFetch([
    { api_version: "v1", schema_version: 23, capabilities: ["profile.v1"] },
    { subject_id: "entity/id" },
  ]);
  try {
    const client = new AsyncIrisMemoryClient("http://mock.local");
    await client.negotiate(["v1"], { requiredCapabilities: ["profile.v1"] });
    await client.getEntityProfile("entity/id", { agentId: "agent", spaceId: "space" });
  } finally {
    stub.restore();
  }
  assert.deepEqual(call(stub.calls, 0).body, {
    api_versions: ["v1"], required_capabilities: ["profile.v1"],
  });
  assert.match(call(stub.calls, 1).url, /entity%2Fid\/profile\?agent_id=agent&space_id=space/);
});


test("observation context and explicit summaries send scope, cursor and idempotency", async () => {
  const raw = { messages: [], summaries: [], source_watermark: "5", next_cursor: null, has_more: false, partial: false, summaries_partial: false };
  const accepted = { batch_id: "batch", job_id: "job", observation_ids: ["obs"], status: "pending" };
  const stub = stubFetch([raw, accepted]);
  try {
    const client = new AsyncIrisMemoryClient("http://localhost:8765");
    const scope = { agent_id: "agent", space_id: "space" };
    assert.deepEqual(await client.observationContext({ scope, cursor: "cursor", limit: 20 }), raw);
    assert.deepEqual(await client.summarizeObservations({ scope }, { idempotencyKey: "summary" }), accepted);
    assert.equal(stub.calls[0]?.url, "http://localhost:8765/v1/observations:context");
    assert.deepEqual(stub.calls[0]?.body, { scope, cursor: "cursor", limit: 20 });
    assert.equal(stub.calls[1]?.headers["Idempotency-Key"], "summary");
    assert.equal(stub.calls[1]?.url, "http://localhost:8765/v1/observations:summarize");
  } finally { stub.restore(); }
});
