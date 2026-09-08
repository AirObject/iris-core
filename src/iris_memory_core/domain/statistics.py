"""Finite, low-sensitivity statistics definitions shared by projection and readers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HOUR_US = 3_600_000_000
DAY_US = 24 * HOUR_US
WEEK_US = 7 * DAY_US
GRANULARITIES = {"hour": HOUR_US, "day": DAY_US, "week": WEEK_US}
# Upper-inclusive logarithmic bins, followed by an unbounded overflow bin.
LATENCY_BOUNDS_US = tuple(1_000 * 2**power for power in range(17))
MEMORY_COLLECTIONS = (
    "observations",
    "states",
    "focus-items",
    "notes",
    "tasks",
    "claims",
    "episodes",
    "relations",
    "artifacts",
    "cognitive-events",
    "candidates",
    "persona-proposals",
)
DIMENSIONS = ("agent_id", "space_group_id", "space_id", "session_id")


def bucket_start(value: int, granularity: str) -> int:
    width = GRANULARITIES[granularity]
    # Monday 00:00 UTC; the Unix epoch was Thursday.
    offset = 4 * DAY_US if granularity == "week" else 0
    return ((value - offset) // width) * width + offset


@dataclass(frozen=True, slots=True)
class MetricSpec:
    metric_id: str
    panel: str
    label: str
    unit: str = "count"
    source: str = "rollup"
    group_by: tuple[str, ...] = DIMENSIONS
    description: str = "当前 Grant 可见集合;不返回正文或不可见余数。"
    system: bool = False
    approximate: bool = False

    def as_dict(self, coverage_from: str | None) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "panel": self.panel,
            "label": self.label,
            "unit": self.unit,
            "value_type": "decimal",
            "source": self.source,
            "granularity": list(GRANULARITIES) if self.source == "rollup" else [],
            "group_by": list(self.group_by),
            "filters": [{"key": key, "label": key, "type": "string"} for key in DIMENSIONS],
            "permission": "stats.read",
            "required_permissions": ["stats.read"] + (["system.read"] if self.system else []),
            "coverage_from": coverage_from,
            "description": self.description,
        }


METRICS = (
    MetricSpec(
        "memory.present",
        "overview",
        "现存记忆",
        group_by=(*DIMENSIONS, "resource_type", "privacy_label", "source_authority", "decision"),
    ),
    MetricSpec(
        "memory.created",
        "timeseries",
        "可见记忆新增",
        group_by=(*DIMENSIONS, "resource_type", "privacy_label", "source_authority", "decision"),
    ),
    MetricSpec(
        "memory.created_24h",
        "overview",
        "近 24 小时可见新增",
        group_by=(*DIMENSIONS, "resource_type", "privacy_label", "source_authority", "decision"),
    ),
    MetricSpec(
        "memory.created_7d",
        "overview",
        "近 7 天可见新增",
        group_by=(*DIMENSIONS, "resource_type", "privacy_label", "source_authority", "decision"),
    ),
    MetricSpec(
        "memory.tombstones",
        "overview",
        "可见删除标记",
        group_by=(*DIMENSIONS, "resource_type", "privacy_label", "source_authority", "decision"),
    ),
    *(
        MetricSpec(f"active.{name}", "overview", label)
        for name, label in (
            ("agents", "活跃 Agent"),
            ("spaces", "活跃 Space"),
            ("sessions", "活跃 Session"),
        )
    ),
    MetricSpec(
        "pending.items", "overview", "待处理事项", group_by=("resource_type", "status", *DIMENSIONS)
    ),
    MetricSpec("recall.requests", "recall", "Recall 请求量"),
    MetricSpec("recall.candidates", "recall", "路由候选贡献", group_by=("route", *DIMENSIONS)),
    MetricSpec(
        "recall.degraded", "recall", "Recall 降级", group_by=("reason_code", "route", *DIMENSIONS)
    ),
    MetricSpec("recall.budget_truncated", "recall", "预算截断率", unit="ratio"),
    MetricSpec("recall.host_selected", "recall", "宿主选中候选"),
    MetricSpec("recall.model_visible", "recall", "模型可见候选"),
    *(
        MetricSpec(
            f"recall.latency_p{q}",
            "recall",
            f"Recall p{q} 耗时",
            unit="microseconds",
            approximate=True,
            description="与 RecallTrace 同一单调时钟区间;固定对数直方图近似,历史不可补算。",
        )
        for q in (50, 95, 99)
    ),
    MetricSpec(
        "pipeline.jobs",
        "pipeline",
        "Outbox 任务",
        source="live",
        system=True,
        group_by=("job_kind", "status", *DIMENSIONS),
    ),
    MetricSpec(
        "pipeline.oldest_pending_us",
        "pipeline",
        "最老待处理任务年龄",
        unit="microseconds",
        source="live",
        system=True,
    ),
    MetricSpec(
        "pipeline.dlq", "pipeline", "死信任务", source="live", system=True, group_by=("job_kind",)
    ),
    MetricSpec(
        "pipeline.schedule_lag_us",
        "pipeline",
        "调度 tick 落后",
        unit="microseconds",
        source="live",
        system=True,
        group_by=("job_kind",),
    ),
    MetricSpec(
        "pipeline.leases",
        "pipeline",
        "当前 Worker 租约",
        source="live",
        system=True,
        group_by=("status",),
    ),
    MetricSpec(
        "pipeline.heartbeat_us",
        "pipeline",
        "Worker 心跳距今",
        source="live",
        unit="microseconds",
        system=True,
    ),
    *(
        MetricSpec(
            f"projection.{name}",
            "projections",
            label,
            source="live",
            system=True,
            group_by=("projection",),
        )
        for name, label in (
            ("documents", "投影文档数"),
            ("source_watermark", "投影源水位"),
            ("tombstone_watermark", "投影删除水位"),
            ("lag", "投影落后 Canonical"),
        )
    ),
    MetricSpec(
        "provider.calls",
        "providers",
        "Provider 调用",
        system=True,
        group_by=("provider_kind", "outcome"),
    ),
    MetricSpec(
        "provider.cost",
        "providers",
        "Provider 成本",
        unit="microunits",
        system=True,
        group_by=("provider_kind", "outcome"),
    ),
    MetricSpec(
        "provider.circuit",
        "providers",
        "Provider 熔断",
        source="live",
        system=True,
        group_by=("provider_kind",),
    ),
    MetricSpec(
        "provider.budget",
        "providers",
        "Provider 预算使用",
        source="live",
        system=True,
        group_by=("provider_kind",),
    ),
    MetricSpec(
        "provider.rate_limited",
        "providers",
        "Provider 限流次数",
        system=True,
        group_by=("provider_kind",),
    ),
    MetricSpec(
        "storage.rows", "storage", "可见 Canonical 行数", group_by=("resource_type", *DIMENSIONS)
    ),
    MetricSpec(
        "storage.estimated_bytes",
        "storage",
        "可见 Canonical 估算字节",
        unit="bytes",
        approximate=True,
        group_by=("resource_type", *DIMENSIONS),
    ),
    *(
        MetricSpec(
            f"storage.{name}",
            "storage",
            label,
            unit="bytes",
            source="live",
            system=True,
            group_by=(),
            description="此服务实例的文件占用;不是租户或 Grant 可见数据的字节数。",
        )
        for name, label in (
            ("database_bytes", "实例数据库文件"),
            ("wal_bytes", "实例 WAL 文件"),
            ("free_bytes", "实例 SQLite 空闲页"),
            ("staging_bytes", "实例传输暂存"),
            ("vector_bytes", "实例向量文件"),
        )
    ),
    MetricSpec(
        "security.keys", "security", "可见运营密钥", source="live", group_by=("status", "template")
    ),
    MetricSpec(
        "security.expiring_keys",
        "security",
        "30 天内到期运营密钥",
        source="live",
        group_by=("template",),
    ),
    MetricSpec("security.sessions", "security", "可见活动会话", source="live", group_by=()),
    MetricSpec(
        "security.login_failures",
        "security",
        "可见密钥登录失败",
        source="live",
        group_by=(),
        description="保留审计中当前Grant可见密钥的失败次数;未知密钥无法归属而不计入。",
    ),
    MetricSpec(
        "security.lockouts",
        "security",
        "可见密钥锁定拒绝",
        source="live",
        group_by=(),
        description="覆盖起点之后保留审计中的可见密钥限流拒绝次数。",
    ),
    MetricSpec("security.audit", "security", "可见审计事件", group_by=("action",)),
    MetricSpec("security.holds", "security", "可见 Legal Hold", source="live", group_by=()),
    MetricSpec("security.retention", "security", "可见保留策略", source="live", group_by=()),
)
BY_METRIC = {metric.metric_id: metric for metric in METRICS}
PANELS = frozenset({metric.panel for metric in METRICS})


def histogram(duration_us: int) -> list[int]:
    result = [0] * (len(LATENCY_BOUNDS_US) + 1)
    index = next(
        (i for i, bound in enumerate(LATENCY_BOUNDS_US) if duration_us <= bound),
        len(LATENCY_BOUNDS_US),
    )
    result[index] = 1
    return result
