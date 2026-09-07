import assert from "node:assert/strict";
import test from "node:test";

import { AsyncIrisMemoryClient, parseEventStream } from "../src/index.js";

test("parseEventStream retains cursor and forward fields", () => {
  const events = parseEventStream(
    'id: 7\nevent: persona.revised.v1\ndata: {"event_id":"event-1","event_type":"persona.revised.v1","occurred_at":"2026-09-05T00:00:00Z","source_watermark":12,"resource_refs":[],"future":true}\n\n',
  );
  assert.equal(events.length, 1);
  assert.equal(events[0]?.cursor, "7");
  assert.equal(events[0]?.event_type, "persona.revised.v1");
});

test("adapter-facing reads propagate AbortSignal and authentication", async () => {
  const controller = new AbortController();
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  const fetchStub: typeof fetch = async (input, init) => {
    calls.push({ input: String(input), ...(init === undefined ? {} : { init }) });
    return new Response(
      JSON.stringify({ api_version: "v1", schema_version: 11, capabilities: ["recall.v1"] }),
      { status: 200 },
    );
  };
  const client = new AsyncIrisMemoryClient("http://core.invalid/", {
    bearerToken: "token",
    fetch: fetchStub,
  });
  await client.capabilities({ signal: controller.signal });
  assert.equal(calls[0]?.input, "http://core.invalid/v1/capabilities");
  assert.equal(calls[0]?.init?.signal, controller.signal);
  assert.equal(new Headers(calls[0]?.init?.headers).get("authorization"), "Bearer token");
});

test("events sends Last-Event-ID and parses a finite SSE poll", async () => {
  let headers = new Headers();
  const client = new AsyncIrisMemoryClient("http://core.invalid", {
    fetch: async (_input, init) => {
      headers = new Headers(init?.headers);
      return new Response(": keep-alive\n\n", {
        headers: { "content-type": "text/event-stream" },
      });
    },
  });
  assert.deepEqual(await client.events({ after: "9" }), []);
  assert.equal(headers.get("last-event-id"), "9");
  assert.equal(headers.get("x-iris-after-event-id"), null);
  assert.deepEqual(await client.events({ after: "9", afterEventId: "saved-event-9" }), []);
  assert.equal(headers.get("x-iris-after-event-id"), "saved-event-9");
});

test("events rejects invalid saved checkpoints before transport", async () => {
  let calls = 0;
  const client = new AsyncIrisMemoryClient("http://core.invalid", {
    fetch: async () => { calls++; return new Response(": keep-alive\n\n"); },
  });
  for (const options of [
    { afterEventId: "event" }, { after: "0", afterEventId: "event" },
    { after: "01", afterEventId: "event" }, { after: "9223372036854775808", afterEventId: "event" },
    { after: "9", afterEventId: "\n" }, { after: "9", afterEventId: "a".repeat(513) },
  ]) await assert.rejects(() => client.events(options), /event checkpoint/);
  assert.equal(calls, 0);
});

test("events rejects an HTTP error instead of treating it as an empty poll", async () => {
  for (const status of [410, 503]) {
    const client = new AsyncIrisMemoryClient("http://core.invalid", {
      fetch: async () => new Response('{"error":"unavailable"}', { status }),
    });
    await assert.rejects(() => client.events(), new RegExp(`event stream failed with ${status}`));
  }
});
