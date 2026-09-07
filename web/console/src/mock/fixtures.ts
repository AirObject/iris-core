/** Development/test-only descriptors. These are NOT published backend contracts. */
import type {
  Action,
  Adapter,
  Field,
  Metric,
  Registry,
  Resource,
  ResourceType,
} from "../api/design";
export const reasons = [
  "operator_request",
  "data_correction",
  "privacy_request",
];
const text = (key: string, label: string, required = false): Field => ({
  key,
  label,
  type: "text",
  required,
});
const json = (key: string, label: string, required = false): Field => ({
  key,
  label,
  type: "json",
  required,
});
export const action = (
  id: string,
  label: string,
  fields: Field[] = [],
  permission = "memory.write",
  suffix?: string,
): Action => ({
  id,
  label,
  permission,
  fields,
  reason_codes: reasons,
  method: id === "update" ? "PATCH" : "POST",
  suffix,
  high_risk: [
    "forget",
    "publish",
    "rollback",
    "confirm",
    "revoke",
    "release",
    "tombstone",
    "redirect",
  ].includes(id),
});
const evidence = json("evidence", "可见有效 Evidence", true);
const scope: Field = {
  key: "agent_id",
  label: "Scope · Agent",
  type: "lookup",
  lookup: "agents",
  required: true,
};
const base = [
  text("title", "标题", true),
  text("body", "正文", true),
  scope,
  json("privacy_labels", "隐私标签"),
  json("source_refs", "来源引用"),
];
const transition = action("transition", "状态转换", [
  {
    key: "target_status",
    label: "目标状态",
    type: "enum",
    options: ["active", "archived", "cancelled", "completed", "dismissed"],
    required: true,
  },
  evidence,
]);
const forget = action("forget", "预览 Forget", [], "memory.forget");
const specs: [string, string, string, Action[], boolean?, boolean?][] = [
  [
    "observations",
    "Observation",
    "原始事件只允许 annotate 创建关联更正记录。",
    [
      action("annotate", "补充更正说明", [
        text("body", "更正说明", true),
        evidence,
      ]),
      forget,
    ],
    false,
    true,
  ],
  [
    "states",
    "State",
    "仅允许 user 来源的 namespace；过期使用专属动作。",
    [
      action("update", "更正 State", [
        text("value", "状态值", true),
        { key: "ttl_us", label: "TTL", type: "duration_us" },
      ]),
      action("expire", "使 State 过期"),
      forget,
    ],
  ],
  [
    "focus-items",
    "Focus",
    "关注项遵守状态机与衰减语义；dismissed 不等于擦除。",
    [
      action("update", "更新关注项", [text("body", "内容", true)]),
      action("activate", "激活关注"),
      transition,
      forget,
    ],
  ],
  [
    "notes",
    "Note",
    "便签修改与置顶、归档、snooze、晋升遵循领域规则。",
    [action("update", "修改 Note", base), transition, forget],
  ],
  [
    "tasks",
    "Task",
    "完成需 Evidence；cancel 是取消计划，承诺未履行保持保护。",
    [action("update", "修改任务", base), transition, forget],
  ],
  [
    "claims",
    "Claim",
    "correct 创建新 Revision，旧事实和证据保留历史。",
    [
      action("correct", "更正 Claim", [
        text("body", "更正事实", true),
        evidence,
      ]),
      forget,
    ],
  ],
  [
    "episodes",
    "Episode",
    "摘要、边界与来源更正产生 Revision。",
    [
      action("update", "更正摘要与边界", [
        text("summary", "摘要", true),
        text("from", "边界起点"),
        text("to", "边界终点"),
        evidence,
      ]),
      transition,
      forget,
    ],
  ],
  [
    "relations",
    "Relation",
    "两端、方向、双时态和 Evidence 由服务端校验。",
    [
      action("correct", "更正 Relation", [
        json("endpoints", "可见端点", true),
        text("direction", "方向", true),
        evidence,
      ]),
      transition,
      forget,
    ],
  ],
  [
    "artifacts",
    "Artifact",
    "内容寻址对象不能原地替换；新建后更正引用者。",
    [forget],
    false,
    true,
  ],
  [
    "entities",
    "Entity",
    "显式身份属性与受控 redirect，不按同名合并。",
    [
      action(
        "attributes",
        "添加属性",
        [json("attributes", "属性", true)],
        "memory.write",
        "/attributes",
      ),
      action("redirect", "受控 redirect", [
        {
          key: "target_id",
          label: "目标已存在 Entity",
          type: "lookup",
          lookup: "entities",
          required: true,
        },
        evidence,
      ]),
      action("tombstone", "Entity Tombstone"),
    ],
  ],
  [
    "identities",
    "Identity",
    "发生时身份历史保留；绑定通过明确确认。",
    [],
    true,
  ],
  [
    "bindings",
    "Binding",
    "只允许显式确认与撤销。",
    [
      action("confirm", "确认身份绑定", [evidence]),
      action("revoke", "撤销绑定"),
    ],
  ],
  [
    "cognitive-events",
    "CognitiveEvent",
    "投递状态只读，管理端只允许 dismiss，不伪造 ACK。",
    [action("dismiss", "终止待投递事件")],
    false,
    true,
  ],
  [
    "reflections",
    "Reflection",
    "流水线输入版本与结果只读；可请求 dry-run / replay。",
    [action("dry-run", "请求 dry-run"), action("replay", "请求 replay")],
  ],
  [
    "candidates",
    "候选",
    "审核不会直接把输出设为已采纳事实。",
    [
      action("review", "审核候选", [
        {
          key: "decision",
          label: "决定",
          type: "enum",
          options: ["accept_as_candidate", "reject"],
          required: true,
        },
        evidence,
      ]),
    ],
  ],
  ...["recent-context", "fts", "vector", "profile", "graph"].map(
    (s): [string, string, string, Action[], boolean] => [
      s,
      `${s} 投影`,
      "只读派生索引。修改请前往 Canonical 来源。",
      [],
      true,
    ],
  ),
];
export const resourceTypes: ResourceType[] = specs.map(
  ([collection, label, description, actions, read_only, append_only]) => ({
    collection,
    resource_type:
      (
        {
          "focus-items": "focus_item",
          "cognitive-events": "cognitive_event",
          entities: "entity",
          identities: "identity",
          "recent-context": "recent_context",
        } as Record<string, string>
      )[collection] ??
      (collection.endsWith("s") ? collection.slice(0, -1) : collection),
    label,
    description,
    actions,
    read_only,
    append_only,
    permission: "memory.read",
    list_columns: ["title", "status"].map((key) => ({ key, label: key, type: "string" })),
    create_schema: read_only ? null : {},
    update_schema: read_only ? null : {},
    supports: { history: true, references: true, forget: !read_only },
    filters: [
      {
        key: "status",
        label: "状态",
        type: "enum",
        options: ["active", "draft", "archived"],
      },
      { key: "agent_id", label: "Agent", type: "lookup", lookup: "agents" },
    ],
    sorts: [
      { key: "created_at_desc", label: "创建时间", direction: "desc" },
      { key: "created_at_asc", label: "创建时间", direction: "asc" },
    ],
    ...(!read_only &&
    ![
      "cognitive-events",
      "reflections",
      "candidates",
      "identities",
      "bindings",
      "entities",
    ].includes(collection)
      ? {
          create: action(
            "create",
            `新建 ${label}`,
            collection === "observations"
              ? [text("body", "当前真实人工提交", true), scope]
              : collection === "claims" || collection === "relations"
                ? [...base, evidence]
                : base,
          ),
        }
      : {}),
  }),
);
export const makeResource = (
  type: ResourceType,
  id: string,
  title: string,
): Resource => ({
  id,
  resource_type: type.resource_type,
  revision: type.append_only ? undefined : 1,
  version_token: type.append_only ? "opaque-version-1" : undefined,
  status: "active",
  fields: {
    title,
    body: "这是一条模拟运营记录。<script>不会执行</script>\n数据来自显式启用的开发模拟环境。",
  },
  scope: { agent_id: "agent-1", space_id: null },
  privacy_labels: ["internal"],
  source_refs: [],
  created_at: "2026-09-06T08:00:00.123456Z",
  updated_at: "2026-09-06T08:00:00.123456Z",
  available_actions: [
    ...type.actions.map((a) => a.id),
    ...(type.collection === "tasks" ? ["create"] : []),
  ],
  blocked_actions: [],
  ...(type.read_only ? { source: { collection: "notes", id: "notes-1" } } : {}),
});
export const metrics: Metric[] = [
  "overview",
  "timeseries",
  "pipeline",
  "projections",
  "recall",
  "providers",
  "storage",
  "security",
].flatMap((panel) => [
  {
    metric_id: `${panel}.count`,
    panel,
    label: `${panel} · 可见记录数`,
    unit: "count",
    granularity: ["hour", "day", "week"],
    group_by: ["agent_id"],
    filters: [],
    permission: "stats.read",
    description: "模拟：当前授权集合的计数；无不可见分组。",
  },
  {
    metric_id: `${panel}.latency`,
    panel,
    label: `${panel} · 延迟`,
    unit: "microseconds",
    granularity: ["hour", "day"],
    group_by: [],
    filters: [],
    permission: "stats.read",
    description: "历史未采集时为 null，不补零。",
  },
]);
export const adapters: Adapter[] = [
  {
    id: "openai-compatible",
    label: "OpenAI compatible · 服务端连接",
    reason_codes: reasons,
    fields: [
      {
        key: "endpoint",
        label: "HTTPS Endpoint",
        type: "string",
        required: true,
      },
      { key: "model", label: "Model", type: "string", required: true },
      { key: "dimension", label: "Dimension", type: "integer", required: true },
      {
        key: "secret_mode",
        label: "密钥承载",
        type: "enum",
        options: ["secret_ref", "sealed"],
        required: true,
      },
      {
        key: "secret_ref",
        label: "Secret 引用",
        type: "string",
        description: "只填 env:NAME 或 file:/绝对引用",
      },
      { key: "sealed", label: "一次性密钥", type: "secret" },
      { key: "timeout_us", label: "超时", type: "duration_us" },
    ],
  },
];
export const settingsRegistry: Registry = {
  reason_codes: reasons,
  settings: [
    {
      key: "worker.batch_size",
      label: "Worker 批次上限",
      type: "integer",
      group: "worker",
      scope: ["tenant", "agent"],
      source: "default",
      risk: "medium",
      apply_mode: "worker",
      permission: "settings.write",
      default: 100,
      minimum: "1",
      maximum: "1000",
    },
    {
      key: "console.upload_limit_bytes",
      label: "上传限额",
      type: "bytes",
      group: "console",
      scope: ["tenant"],
      source: "default",
      risk: "high",
      apply_mode: "restart",
      permission: "settings.write",
      default: "52428800",
      minimum: "1",
      maximum: "1073741824",
    },
    {
      key: "recall.deadline_us",
      label: "Recall 预算",
      type: "duration_us",
      unit: "微秒",
      group: "recall",
      scope: ["tenant", "agent"],
      source: "default",
      risk: "medium",
      apply_mode: "online",
      permission: "settings.write",
      default: "250000",
      minimum: "1000",
      maximum: "1000000",
    },
  ],
};
