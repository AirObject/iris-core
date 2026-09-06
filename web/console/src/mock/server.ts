/** Stateful in-memory transport; imported only by explicit Vite dev mock mode or tests. */
import type { Transport } from "../api/client";
import type {
  Action,
  Bootstrap,
  Fields,
  ImportReport,
  ImportView,
  Operation,
  Preview,
  Resource,
  ProviderView,
  Session,
  SettingsView,
  Value,
} from "../api/design";
import {
  action,
  adapters,
  makeResource,
  metrics,
  reasons,
  resourceTypes,
  settingsRegistry,
} from "./fixtures";
import sessionFixture from "../../../../schemas/fixtures/console/valid/SessionView.json" with { type: "json" };
import keyFixture from "../../../../schemas/fixtures/console/valid/KeyView.json" with { type: "json" };
export interface MockOptions {
  readOnly?: boolean;
  maintenance?: boolean;
  permissions?: Bootstrap["permissions"];
  scenario?:
    | "csrf"
    | "reauth"
    | "conflict"
    | "preview_stale"
    | "partial"
    | "blocked"
    | "provider_dimension";
}
export function createMockTransport(options: MockOptions = {}): Transport {
  let authenticated = false;
  let csrf = "mock-csrf";
  let sequence = 0;
  let injected = false;
  let settingsRevision = 1;
  const values: Fields = Object.fromEntries(
    settingsRegistry.settings.map((s) => [s.key, s.default]),
  );
  const operations = new Map<string, Operation>();
  const imports = new Map<string, ImportView>();
  const reports = new Map<string, ImportReport>();
  const previews = new Map<string, Preview>();
  const previewSets = new Map<string, Resource[]>();
  const managed = new Map<string, Resource[]>();
  const histories = new Map<string, Resource[]>();
  const configs = new Map<string, ProviderView>();
  const cache = new Map<
    string,
    { body: string; status: number; data: unknown }
  >();
  const resources = new Map(
    resourceTypes.map((t) => [
      t.collection,
      [
        makeResource(t, `${t.collection}-1`, `${t.label} · 项目上下文`),
        makeResource(t, `${t.collection}-2`, `${t.label} · 待复核记录`),
      ],
    ]),
  );
  let keys = [structuredClone(keyFixture)];
  const permissions: Bootstrap["permissions"] = options.permissions ?? [
    "memory.read",
    "memory.write",
    "memory.forget",
    "memory.history",
    "persona.publish",
    "keys.manage",
    "service_keys.manage",
    "stats.read",
    "system.read",
    "system.write",
    "imports.write",
    "exports.write",
    "exports.read_all",
    "providers.manage",
    "settings.read",
    "settings.write",
    "indexes.rebuild",
    "audit.read",
    "retention.manage",
  ];
  const now = () => new Date().toISOString().replace("Z", "000Z");
  const future = (ms = 1800000) =>
    new Date(Date.now() + ms).toISOString().replace("Z", "000Z");
  const session = (): Session =>
    ({
      ...sessionFixture,
      csrf_token: csrf,
      permissions,
      grants: { ...sessionFixture.grants, permissions } as Session["grants"],
      session: {
        id: "mock-session",
        expires_at: future(43200000),
        idle_expires_at: future(),
        reauth_until: future(),
      },
    }) as Session;
  const reply = (
    data: unknown,
    status = 200,
    extra: Record<string, unknown> = {},
  ) =>
    new Response(
      status === 204
        ? null
        : JSON.stringify({
            data:
              data && typeof data === "object" && "items" in data
                ? data.items
                : data,
            meta: {
              ...(data && typeof data === "object" && "items" in data
                ? { descriptor: { ...data, items: undefined } }
                : {}),
              request_id: `mock-request-${++sequence}`,
              contract_version: "1.0.0",
              as_of: now(),
              page: { has_more: false, next_cursor: null, limit: 50 },
              warnings: ["mock_data"],
              ...extra,
            },
          }),
      {
        status,
        headers: {
          "Content-Type": "application/json",
          "X-Request-ID": `mock-request-${sequence}`,
        },
      },
    );
  const fail = (
    status: number,
    code: string,
    kind: string,
    details: Record<string, unknown> = {},
  ) =>
    new Response(
      JSON.stringify({
        error: {
          code,
          message: "模拟错误",
          retryable: false,
          details: { kind, ...details },
        },
        request_id: `mock-request-${++sequence}`,
      }),
      { status, headers: { "Content-Type": "application/json" } },
    );
  const operation = (kind: string, status: Operation["status"] = "queued") => {
    const op: Operation = {
      id: `operation-${++sequence}`,
      kind,
      status,
      phase: "等待 Worker",
      progress: {
        processed: status === "cancelled_partial" ? "100" : "0",
        total: "250",
        unit: "records",
      },
      cancellable: !["completed", "cancelled_partial"].includes(status),
      result_ref: null,
      problems_count: status === "blocked" ? 1 : 0,
      blocked_reason:
        status === "blocked"
          ? "backup_unavailable：等待已校验的本地备份"
          : null,
      available_actions: ["cancel"],
      reason_codes: reasons,
    };
    operations.set(op.id, op);
    return op;
  };
  const settings = (): SettingsView => ({
    settings_revision: settingsRevision,
    values: { ...values },
    sources: {},
    instances: [
      {
        id: "worker-1",
        applied_revision: settingsRevision - 1,
        status: "pending_restart",
      },
    ],
    status: settingsRevision > 1 ? "pending_restart" : "effective",
    side_effects: { pending_restart: settingsRevision > 1 },
    available_actions: ["update", "reset", "rollback"],
    blocked_actions: [],
  });
  return async (url, init, progress) => {
    if (init.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const parsed = new URL(url, "http://localhost");
    const path = parsed.pathname.replace("/console/v1", "");
    const method = init.method ?? "GET";
    const headers = new Headers(init.headers);
    const body =
      typeof init.body === "string"
        ? (JSON.parse(init.body) as Record<string, unknown>)
        : {};
    if (path === "/auth/login") {
      authenticated = true;
      return reply(session());
    }
    if (!authenticated)
      return fail(401, "access_denied", "authentication_required");
    if (path === "/auth/session") return reply(session());
    if (path === "/auth/logout") {
      authenticated = false;
      return reply(null, 204);
    }
    if (path === "/auth/refresh") {
      csrf = `mock-csrf-${sequence}`;
      return reply(session());
    }
    if (path === "/auth/reauth") return reply({ reauth_until: future() });
    if (method !== "GET" && headers.get("X-IMC-CSRF") !== csrf)
      return fail(403, "access_denied", "csrf_failed");
    if (
      method !== "GET" &&
      !injected &&
      ["csrf", "reauth", "conflict", "preview_stale"].includes(
        options.scenario ?? "",
      )
    ) {
      const scenario = options.scenario;
      if (scenario !== "preview_stale" || path.endsWith(":forget")) {
        injected = true;
        if (scenario === "csrf")
          return fail(403, "access_denied", "csrf_failed");
        if (scenario === "reauth")
          return fail(403, "access_denied", "reauth_required");
        if (scenario === "conflict")
          return fail(409, "revision_mismatch", "revision_conflict");
        if (scenario === "preview_stale")
          return fail(409, "conflict", "preview_stale");
      }
    }
    const cacheKey = headers.get("Idempotency-Key");
    const fingerprint = `${method}:${path}:${typeof init.body === "string" ? init.body : "binary"}`;
    if (cacheKey && cache.has(cacheKey)) {
      const cached = cache.get(cacheKey)!;
      if (cached.body !== fingerprint)
        return fail(409, "idempotency_key_reused", "");
      const replay =
        cached.data &&
        typeof cached.data === "object" &&
        "secret_available" in cached.data
          ? { ...cached.data, secret: undefined, secret_available: false }
          : cached.data;
      return reply(replay, cached.status);
    }
    const write = (data: unknown, status = 200) => {
      if (cacheKey)
        cache.set(cacheKey, {
          body: fingerprint,
          data: structuredClone(data),
          status,
        });
      return reply(data, status);
    };
    if (path === "/bootstrap")
      return reply({
        contract_version: "1.0.0",
        permissions,
        modules: [
          "memory",
          "stats",
          "exports",
          "imports",
          "personas",
          "keys",
          "service_credentials",
          "providers",
          "settings",
          "retention",
          "operations",
          "system",
          "audit",
        ],
        read_only: options.readOnly ?? false,
        maintenance: options.maintenance ?? false,
        upload_limits: {
          file_bytes: "52428800",
          records: "100000",
          record_bytes: "262144",
          json_depth: 20,
        },
        display_timezone: "UTC",
        pending_restart: false,
        import_in_progress: false,
      });
    if (path.startsWith("/lookups/"))
      return reply([
        { id: "agent-1", label: "Iris · 主工作区" },
        { id: "entity-1", label: "已确认对象" },
      ]);
    if (path === "/memory/resource-types") return reply(resourceTypes);
    if (
      path.endsWith(":forget-preview") ||
      path.endsWith(":compensate-preview")
    ) {
      const selector = body.selector as
        | { collection: string; filters?: Fields }
        | undefined;
      const selected = selector
        ? (resources.get(selector.collection) ?? []).filter(
            (r) =>
              !selector.filters?.status || r.status === selector.filters.status,
          )
        : (
            (body.targets as { id: string; resource_type: string }[]) ?? []
          ).flatMap((t) =>
            (
              resources.get(
                resourceTypes.find((r) => r.resource_type === t.resource_type)
                  ?.collection ?? t.resource_type,
              ) ?? []
            ).filter((r) => r.id === t.id),
          );
      const preview: Preview = {
        preview_id: `preview-${++sequence}`,
        preview_hash: `server-preview-hash-${sequence}`,
        expires_at: future(600000),
        targets: [
          {
            index: 0,
            status: "allowed",
            revision: 1,
            protected: false,
            held: false,
          },
        ],
        impacts: {
          canonical: "将写入 Tombstone",
          cleanup: ["fts", "vector", "profile", "graph", "blob"],
          fixed_set: true,
        },
        can_commit: true,
        reason_codes: reasons,
      };
      if (path.endsWith(":forget-preview")) {
        preview.targets = selected.map((r, index) => ({
          index,
          status: r.blocked_actions.some((a) => a.action === "forget")
            ? "protected"
            : "allowed",
          revision: r.revision ?? null,
          version_token: r.version_token ?? null,
          protected: r.blocked_actions.some((a) => a.action === "forget"),
          held: false,
        }));
        preview.can_commit =
          selected.length > 0 &&
          !selected.some((r) =>
            r.blocked_actions.some((a) => a.action === "forget"),
          );
      }
      previewSets.set(preview.preview_id, structuredClone(selected));
      previews.set(preview.preview_id, preview);
      return write(preview);
    }
    if (path.endsWith(":forget") || path.endsWith(":compensate")) {
      const p = previews.get(String(body.preview_id));
      if (!p || p.preview_hash !== body.preview_hash)
        return fail(409, "conflict", "preview_stale");
      if (!p.can_commit) return fail(409, "protected_resource", "");
      const selected = previewSets.get(p.preview_id) ?? [];
      for (const old of selected) {
        const latest = (
          resources.get(
            resourceTypes.find((r) => r.resource_type === old.resource_type)
              ?.collection ?? old.resource_type,
          ) ?? []
        ).find((r) => r.id === old.id);
        if (
          !latest ||
          latest.revision !== old.revision ||
          latest.version_token !== old.version_token
        )
          return fail(409, "conflict", "preview_stale");
      }
      for (const old of selected)
        resources.set(
          resourceTypes.find((r) => r.resource_type === old.resource_type)
            ?.collection ?? old.resource_type,
          (
            resources.get(
              resourceTypes.find((r) => r.resource_type === old.resource_type)
                ?.collection ?? old.resource_type,
            ) ?? []
          ).filter((r) => r.id !== old.id),
        );
      previews.delete(p.preview_id);
      previewSets.delete(p.preview_id);
      return write(
        {
          canonical_status: "committed",
          cleanup_status: "queued",
          operation: operation("forget_cleanup"),
        },
        202,
      );
    }
    if (path.startsWith("/memory/")) {
      const parts = path.split("/");
      const collection = parts[2] ?? "";
      const type = resourceTypes.find((t) => t.collection === collection);
      if (!type) return fail(404, "not_found", "");
      const list = resources.get(collection)!;
      const idAction = parts[3]?.split(":");
      const id = idAction?.[0];
      const row = list.find((r) => r.id === id);
      if (parts[4] === "history" || parts[4] === "references")
        return reply(
          row
            ? parts[4] === "history"
              ? (histories.get(row.id) ?? [row])
              : (row.source_refs ?? [])
            : [],
        );
      if (
        parts[4] &&
        ["steps", "dependencies", "triggers"].includes(parts[4])
      ) {
        if (method === "GET")
          return reply({
            items: [
              {
                ...makeResource(type, `${parts[4]}-1`, "任务子对象"),
                available_actions: ["transition", "disable", "remove"],
              },
            ],
            create: action("create", `添加 ${parts[4]}`, [
              {
                key: "body",
                label: "内容或调度说明",
                type: "text",
                required: true,
              },
              { key: "evidence", label: "完成证据", type: "json" },
            ]),
            actions: [
              action("transition", "完成步骤", [
                {
                  key: "evidence",
                  label: "完成 Evidence",
                  type: "json",
                  required: true,
                },
              ]),
              action("disable", "禁用触发器"),
              action("remove", "移除依赖"),
            ],
          });
        return write({});
      }
      if (method === "GET" && id && !row) return fail(404, "not_found", "");
      if (method === "GET")
        return reply(
          id
            ? row
            : list.filter(
                (r) =>
                  !parsed.searchParams.get("status") ||
                  r.status === parsed.searchParams.get("status"),
              ),
        );
      if (!id) {
        const next = makeResource(
          type,
          `${collection}-${++sequence}`,
          String((body.fields as Fields)?.title ?? "新记录"),
        );
        next.fields = { ...next.fields, ...(body.fields as Fields) };
        list.push(next);
        return write(next, 201);
      }
      if (!row) return fail(404, "not_found", "");
      if (
        body.expected_revision !== undefined &&
        body.expected_revision !== row.revision
      )
        return fail(409, "revision_mismatch", "revision_conflict");
      histories.set(row.id, [
        ...(histories.get(row.id) ?? []),
        structuredClone(row),
      ]);
      if (idAction?.[1] === "annotate") {
        const note = makeResource(
          resourceTypes.find((t) => t.collection === "notes")!,
          `annotation-${++sequence}`,
          "Observation 更正说明",
        );
        note.fields = { ...note.fields, ...(body.fields as Fields) };
        resources.get("notes")!.push(note);
        row.source_refs = [
          ...(row.source_refs ?? []),
          { id: note.id, resource_type: "note" },
        ];
        return write({ original: row, annotation: note }, 201);
      }
      if (body.target_status) row.status = String(body.target_status);
      if (idAction?.[1] === "expire") row.status = "expired";
      if (idAction?.[1] === "activate") row.status = "active";
      if (idAction?.[1] === "dismiss") row.status = "dismissed";
      row.fields = { ...row.fields, ...(body.fields as Fields) };
      row.revision = (row.revision ?? 0) + 1;
      return write(row);
    }
    if (path === "/stats/metrics") return reply(metrics);
    if (path.startsWith("/stats/")) {
      const panel = path.split("/")[2];
      return reply(
        metrics
          .filter((m) => m.panel === panel)
          .map((m, i) => ({
            id: m.metric_id,
            metric_id: m.metric_id,
            value: i === 0 ? "900719925474099312345" : null,
            approximate: i > 0,
          })),
        200,
        {
          source: "rollup",
          computed_at: now(),
          coverage_from: "2026-09-06T00:00:00.123456Z",
          stale: true,
          warnings: ["mock_data", "rollup_lagging", "no_historical_coverage"],
        },
      );
    }
    if (path === "/exports") {
      if (method === "POST")
        return write({ operation: operation("export") }, 202);
      return reply([
        {
          id: "export-1",
          status: "completed",
          format: "imc-data/v1",
          rows: "1",
          bytes: "512",
          sha256: "a".repeat(64),
          expires_at: future(),
          downloadable: true,
          available_actions: ["delete"],
          blocked_actions: [],
        },
      ]);
    }
    if (path.startsWith("/exports/") && method !== "GET")
      return write({ operation: operation("export_cleanup") }, 202);
    if (path.endsWith("/download")) {
      const line =
        JSON.stringify({
          kind: "record",
          source_id: "note-1",
          resource_type: "note",
          payload: { body: "模拟导出往返数据" },
        }) + "\n";
      const hash = [
        ...new Uint8Array(
          await crypto.subtle.digest("SHA-256", new TextEncoder().encode(line)),
        ),
      ]
        .map((n) => n.toString(16).padStart(2, "0"))
        .join("");
      return new Response(
        JSON.stringify({
          kind: "header",
          format: "imc-data",
          format_version: 1,
          dataset_id: "mock-dataset",
          mode: "data",
          resources: ["note"],
        }) +
          "\n" +
          line +
          JSON.stringify({
            kind: "trailer",
            record_count: "1",
            records_sha256: hash,
          }) +
          "\n",
        {
          headers: {
            "Content-Type": "application/x-ndjson",
            "Content-Disposition": "attachment; filename=iris.jsonl",
          },
        },
      );
    }
    if (path === "/imports/formats")
      return reply([
        {
          id: "imc-data/v1",
          label: "IMC JSONL",
          description: "带 header / record / trailer 的纯数据 JSONL",
          extensions: [".jsonl"],
          media_types: ["application/x-ndjson"],
          limits: {
            file_bytes: "52428800",
            records: "100000",
            record_bytes: "262144",
            json_depth: 20,
          },
          mapping_fields: [
            {
              key: "source_namespace",
              label: "来源命名空间",
              type: "string",
              required: true,
            },
            {
              key: "agent_id",
              label: "目标已有 Agent",
              type: "lookup",
              lookup: "agents",
              required: true,
            },
            {
              key: "entity_id",
              label: "显式身份映射",
              type: "lookup",
              lookup: "entities",
            },
          ],
          reason_codes: reasons,
        },
        {
          id: "manual-records/v1",
          label: "人工记录 JSON / JSONL / CSV",
          description: "固定数据列，不接受执行配置或投影。",
          extensions: [".json", ".jsonl", ".csv"],
          media_types: ["application/json", "application/x-ndjson", "text/csv"],
          limits: {
            file_bytes: "52428800",
            records: "100000",
            record_bytes: "262144",
            json_depth: 20,
          },
          mapping_fields: [
            {
              key: "source_namespace",
              label: "来源命名空间",
              type: "string",
              required: true,
            },
            {
              key: "agent_id",
              label: "目标已有 Agent",
              type: "lookup",
              lookup: "agents",
              required: true,
            },
            { key: "columns", label: "源列到允许目标字段", type: "json" },
          ],
          reason_codes: reasons,
        },
      ]);
    if (path === "/imports" && method === "POST") {
      const view: ImportView = {
        id: `import-${++sequence}`,
        revision: 1,
        status: "awaiting_upload",
        format_id: String(body.format_id),
        checkpoint_version: "0",
        available_actions: ["cancel", "resume", "compensate-preview"],
        blocked_actions: [],
        mapping: {},
      };
      imports.set(view.id, view);
      return write(view, 201);
    }
    if (path.startsWith("/imports/")) {
      const id = path.split("/")[2]?.split(":")[0] ?? "";
      const item = imports.get(id);
      if (!item) return fail(404, "not_found", "");
      if (path.endsWith("/file")) {
        progress?.(512, 512);
        item.status = "uploaded";
        item.revision++;
        return write(item);
      }
      if (path.endsWith("/mapping")) {
        if (body.expected_revision !== item.revision)
          return fail(409, "revision_mismatch", "revision_conflict");
        item.mapping = body.mapping as Fields;
        item.revision++;
        reports.delete(id);
        return write(item);
      }
      if (path.endsWith(":validate")) {
        const report: ImportReport = {
          report_id: `report-${++sequence}`,
          report_hash: `server-report-hash-${sequence}`,
          expires_at: future(),
          can_commit: true,
          counts: {
            total: "250",
            created: "0",
            duplicate: "0",
            quarantined: "5",
            skipped: "0",
            failed: "0",
            remaining: "245",
          },
          warnings: [
            "Persona → 待审 Proposal",
            "Task 触发器 disabled",
            "无证据事实隔离",
          ],
          decisions: [{ kind: "quarantined", count: "5" }],
        };
        reports.set(id, report);
        item.status = "validated";
        item.report_hash = report.report_hash;
        return write(
          { operation: operation("import_validate", "completed") },
          202,
        );
      }
      if (path.endsWith("/report"))
        return reports.has(id)
          ? reply(reports.get(id))
          : fail(409, "conflict", "report_stale");
      if (path.endsWith("/problems"))
        return reply([
          {
            id: "problem-1",
            code: "quarantined",
            field: "evidence",
            reason: "缺少可见证据，保留候选",
          },
        ]);
      if (path.includes("/records")) {
        if (method === "POST") {
          reports.delete(id);
          return write({});
        }
        return reply({
          items: [
            {
              ...makeResource(
                resourceTypes[0]!,
                `record-1`,
                "隔离候选：待审核",
              ),
              available_actions: ["review"],
            },
          ],
          actions: [
            action(
              "review",
              "审核此记录",
              [
                {
                  key: "decision",
                  label: "决定",
                  type: "enum",
                  options: ["accept_as_candidate", "reject", "resolve_mapping"],
                  required: true,
                },
              ],
              "imports.write",
            ),
          ],
        });
      }
      if (path.endsWith(":commit")) {
        const report = reports.get(id);
        if (
          !report ||
          body.report_id !== report.report_id ||
          body.report_hash !== report.report_hash
        )
          return fail(409, "conflict", "report_stale");
        return write(
          {
            operation: operation(
              "import_commit",
              options.scenario === "partial"
                ? "cancelled_partial"
                : options.scenario === "blocked"
                  ? "blocked"
                  : "queued",
            ),
          },
          202,
        );
      }
      if (path.endsWith(":cancel") || path.endsWith(":resume"))
        return write(
          {
            operation: operation(
              "import_commit",
              path.endsWith(":cancel") ? "cancelled_partial" : "running",
            ),
          },
          202,
        );
      return reply(item);
    }
    if (path === "/providers/embedding/adapters") return reply(adapters);
    if (path === "/providers/embedding")
      return reply({
        configs: [...configs.values()],
        active:
          [...configs.values()].find((c) => c.status === "active") ?? null,
      });
    if (path === "/providers/embedding/rebuilds")
      return reply(
        [...operations.values()].filter((o) => o.kind === "vector_rebuild"),
      );
    if (path.startsWith("/providers/embedding/configs")) {
      const id = path.split("/")[4]?.split(":")[0];
      let config = id ? configs.get(id) : undefined;
      if (path.endsWith(":activate")) {
        if (
          !config?.probe?.ok ||
          Date.parse(config.probe.expires_at) <= Date.now() ||
          config.probe.dimension_observed !== config.fields.dimension
        )
          return fail(409, "conflict", "probe_required");
        if (body.rebuild_ack !== config.rebuild_plan_hash)
          return fail(409, "conflict", "config_changed");
        config.status = "active";
        return write({ operation: operation("vector_rebuild") }, 202);
      }
      if (path.endsWith(":test") && config) {
        config.probe = {
          ok: true,
          expires_at: future(),
          dimension_observed:
            options.scenario === "provider_dimension"
              ? 999
              : Number(config.fields.dimension),
        };
        config.status = "probed";
        config.side_effects = {
          rebuild: true,
          estimated_records: "9007199254740993",
          worker_required: true,
          old_generation_serves: true,
        };
        config.rebuild_plan_hash = `server-rebuild-${config.revision}`;
        return write(config);
      }
      if (path.endsWith(":discard") && config) {
        configs.delete(config.id);
        return write({});
      }
      if (config && body.expected_revision !== config.revision)
        return fail(409, "revision_mismatch", "revision_conflict");
      const fields = { ...body } as Fields;
      delete fields.sealed;
      delete fields.expected_revision;
      delete fields.reason_code;
      config = {
        id: id ?? `config-${++sequence}`,
        revision: (config?.revision ?? 0) + 1,
        status: "draft",
        fields,
        available_actions: [
          "update",
          "test",
          "activate",
          "discard",
          "rollback",
        ],
        blocked_actions: [],
        side_effects: {},
        generation: "generation-2",
        serving_generation: "generation-1",
        secret_hint: body.sealed ? "••••1234" : undefined,
      };
      configs.set(config.id, config);
      return write(config, id ? 200 : 201);
    }
    if (path === "/providers/embedding:rollback")
      return write({ operation: operation("vector_rebuild") }, 202);
    if (path === "/settings/registry") return reply(settingsRegistry);
    if (path === "/settings/history") return reply([{ revision: 1, values }]);
    if (path === "/settings" && method === "GET") return reply(settings());
    if (path === "/settings:validate")
      return write({
        valid: true,
        differences: body.changes,
        side_effects: {
          pending_restart:
            "console.upload_limit_bytes" in
            ((body.changes ??
              (body.intent === "reset"
                ? Object.fromEntries(
                    (body.keys as string[]).map((k) => [
                      k,
                      settingsRegistry.settings.find((s) => s.key === k)
                        ?.default ?? null,
                    ]),
                  )
                : { "console.upload_limit_bytes": "52428800" })) as Fields),
        },
        high_risk_keys: Object.keys(
          (body.changes ??
            (body.intent === "reset"
              ? Object.fromEntries(
                  (body.keys as string[]).map((k) => [
                    k,
                    settingsRegistry.settings.find((s) => s.key === k)
                      ?.default ?? null,
                  ]),
                )
              : { "console.upload_limit_bytes": "52428800" })) as Fields,
        ).filter(
          (k) =>
            settingsRegistry.settings.find((s) => s.key === k)?.risk === "high",
        ),
      });
    if (path.startsWith("/settings")) {
      if (body.expected_revision !== settingsRevision)
        return fail(409, "revision_mismatch", "revision_conflict");
      Object.assign(values, body.changes);
      settingsRevision++;
      return write(settings());
    }
    if (path.startsWith("/operations/")) {
      const id = path.split("/")[2]?.split(":")[0] ?? "";
      const op = operations.get(id);
      if (!op) return fail(404, "not_found", "");
      if (path.endsWith("/problems"))
        return reply([{ code: "backup_unavailable", reason: "等待备份" }]);
      if (method === "POST") {
        op.status = "cancelled_partial";
        op.cancellable = false;
        return write(op);
      }
      if (op.status === "queued") {
        op.status = "running";
        op.phase = "处理批次";
      } else if (op.status === "running") {
        op.status = "completed";
        op.progress = { processed: "250", total: "250", unit: "records" };
        op.cancellable = false;
      }
      return reply(op);
    }
    if (path === "/auth/sessions")
      return reply([
        {
          id: "mock-session",
          is_current: true,
          created_at: now(),
          expires_at: future(),
        },
      ]);
    if (path.startsWith("/auth/sessions/")) return write(null, 204);
    if (path === "/keys" || path === "/service-credentials") {
      if (method === "GET") return reply(keys);
      const key = {
        ...keyFixture,
        id: `key-${++sequence}`,
        label: String(body.label),
        expires_at: String(body.expires_at),
      };
      keys.push(key);
      return write(
        {
          key,
          secret: "MOCK-ONE-TIME-KEY-NOT-A-CREDENTIAL",
          secret_available: true,
        },
        201,
      );
    }
    if (path.startsWith("/keys/") || path.startsWith("/service-credentials/")) {
      const id = path.split("/")[2]?.split(":")[0];
      const key = keys.find((k) => k.id === id);
      if (!key) return fail(404, "not_found", "");
      if (path.endsWith(":rotate"))
        return write(
          {
            key: {
              ...key,
              status: "pending_confirmation",
              confirmation_expires_at: future(600000),
            },
            secret: "MOCK-SUCCESSOR-NOT-A-CREDENTIAL",
            secret_available: true,
          },
          201,
        );
      if (path.endsWith(":revoke")) {
        if (keys.length === 1)
          return fail(409, "conflict", "last_owner_protected");
        keys = keys.filter((k) => k.id !== id);
      } else Object.assign(key, body, { revision: key.revision + 1 });
      return write(key);
    }
    const generic = /^\/(personas|retention|system|audit|operations)/.test(
      path,
    );
    if (generic) {
      if (method !== "GET") {
        const base = [...managed.keys()]
          .sort((a, b) => b.length - a.length)
          .find((p) => path.startsWith(p));
        if (base) {
          const rows = managed.get(base)!;
          const target = rows.find((r) => path.includes(r.id)) ?? rows[0];
          if (
            target &&
            body.expected_revision !== undefined &&
            body.expected_revision !== target.revision
          )
            return fail(409, "revision_mismatch", "revision_conflict");
          if (target) {
            histories.set(base, [
              ...(histories.get(base) ?? []),
              structuredClone(target),
            ]);
            target.revision = (target.revision ?? 0) + 1;
            target.fields = {
              ...target.fields,
              ...(body.fields as Fields),
              last_action: path.split(":")[1] ?? method,
            };
            target.status = path.endsWith(":release")
              ? "released"
              : path.endsWith(":approve")
                ? "approved"
                : path.endsWith(":reject")
                  ? "rejected"
                  : path.endsWith(":discard")
                    ? "discarded"
                    : "updated";
          }
          if (path.endsWith("/proposals"))
            rows.push({
              ...makeResource(
                resourceTypes[3]!,
                `proposal-${++sequence}`,
                "新建 Persona Proposal",
              ),
              fields: body.fields as Fields,
              status: "needs_review",
              available_actions: ["approve", "reject"],
            });
        }
        return path.startsWith("/system/")
          ? write({ operation: operation(path.split("/")[2] ?? "system") }, 202)
          : write({ status: "saved" });
      }
      if (path.endsWith("/history"))
        return reply(histories.get(path.replace("/history", "")) ?? []);
      if (path.endsWith("/proposals"))
        return reply([
          {
            id: "history-1",
            revision: 1,
            status: "published",
            body: "模拟已发布人格历史",
          },
        ]);
      let actions: Action[] = [];
      let create: Action | undefined;
      if (path.startsWith("/personas/")) {
        actions = [
          action(
            "publish",
            "发布 Revision",
            [
              { key: "body", label: "人格内容", type: "text", required: true },
              {
                key: "evidence",
                label: "Evidence",
                type: "json",
                required: true,
              },
            ],
            "persona.publish",
            "/revisions",
          ),
          action(
            "approve",
            "审批 Proposal",
            [],
            "persona.publish",
            "/proposals/{id}:approve",
          ),
          action(
            "reject",
            "拒绝 Proposal",
            [],
            "persona.publish",
            "/proposals/{id}:reject",
          ),
          action(
            "rollback",
            "回滚为新 Revision",
            [
              {
                key: "target_revision",
                label: "目标修订",
                type: "integer",
                required: true,
              },
            ],
            "persona.publish",
            ":rollback",
          ),
          action(
            "state",
            "修改 Persona State",
            [{ key: "state", label: "State", type: "json", required: true }],
            "memory.write",
            "/state",
          ),
          action("clear", "清除过期 State", [], "memory.write", "/state:clear"),
          action(
            "discard",
            "丢弃未发布草稿",
            [],
            "memory.write",
            "/drafts/{id}:discard",
          ),
        ];
        actions.find((a) => a.id === "state")!.method = "PATCH";
        create = action(
          "create",
          "创建 Proposal",
          [
            {
              key: "body",
              label: "Proposal 内容",
              type: "text",
              required: true,
            },
          ],
          "memory.write",
          "/proposals",
        );
      }
      if (path.includes("/retention/")) {
        actions = [
          action(
            "release",
            "释放 Legal Hold",
            [],
            "retention.manage",
            "/{id}:release",
          ),
        ];
        create = action(
          "create",
          path.endsWith("policies") ? "更新保留策略" : "创建 Legal Hold",
          [
            { key: "scope", label: "授权作用域", type: "json", required: true },
            { key: "duration_us", label: "保留期限", type: "duration_us" },
          ],
          "retention.manage",
        );
      }
      if (path === "/retention/policies") {
        actions = [
          {
            ...action(
              "update",
              "修改保留策略",
              [
                {
                  key: "duration_us",
                  label: "最短保留期",
                  type: "duration_us",
                  required: true,
                },
                {
                  key: "scope",
                  label: "授权范围",
                  type: "json",
                  required: true,
                },
              ],
              "retention.manage",
              "/{id}",
            ),
            method: "PUT",
            description: "缩短保留期仅计划安全扫描；不能同步删除内容。",
          },
        ];
        create = undefined;
      }
      if (path.includes("/system/")) {
        const kind = path.split("/")[2];
        actions =
          kind === "workers"
            ? []
            : [
                action(
                  kind === "jobs"
                    ? "retry"
                    : kind === "schedules"
                      ? "run"
                      : "rebuild",
                  kind === "jobs"
                    ? "重试 DLQ 任务"
                    : kind === "schedules"
                      ? "立即调度"
                      : "重建派生索引",
                  [],
                  kind === "indexes" ? "indexes.rebuild" : "system.write",
                ),
              ];
        if (kind === "backups") {
          actions = [];
          create = action("create", "触发并校验备份", [], "system.write");
        }
      }
      const row = makeResource(resourceTypes[3]!, "managed-1", "当前管理对象");
      row.available_actions = actions.map((a) => a.id);
      row.fields = {
        title: path.includes("jobs") ? "DLQ 任务：等待重试" : "当前管理对象",
        revision: 1,
        policy: "Evidence required",
        status: "current",
      };
      if (path.startsWith("/personas/")) {
        row.available_actions = ["state", "clear", "rollback"];
        if (!managed.has(path))
          managed.set(path, [
            row,
            {
              ...structuredClone(row),
              id: "proposal-1",
              status: "needs_review",
              fields: {
                title: "待审 Proposal",
                body: "待审人格草稿",
                policy: "Evidence required",
              },
              available_actions: ["approve", "reject"],
            },
            {
              ...structuredClone(row),
              id: "draft-1",
              status: "draft",
              fields: { title: "未发布 Revision 草稿" },
              available_actions: ["publish", "discard"],
            },
          ]);
      }
      if (!managed.has(path)) managed.set(path, [row]);
      const items =
        path === "/operations"
          ? [...operations.values()].map((op) => ({
              ...row,
              id: op.id,
              status: op.status,
              fields: { operation: op as unknown as Value },
            }))
          : managed.get(path)!;
      return reply({
        title: "授权管理视图",
        description:
          "开发模拟注册表 · 实际 wire descriptor 尚待业务切片发布确认",
        items,
        actions,
        create,
        reason_codes: reasons,
      });
    }
    return fail(404, "not_found", "");
  };
}
