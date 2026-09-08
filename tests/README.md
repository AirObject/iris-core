# Test navigation

2026-09-08 新实施项：[Observation 全量上下文与批量总结](../docs/development/observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

Tests are organized by execution layer, then by subject. New filenames should
name behavior or an invariant; phase and review identifiers belong in docstrings
and historical reports.

| Location | Coverage |
| --- | --- |
| `unit/` | Domain rules and engineering tools |
| `contract/` | JSON Schema, HTTP and SDK contracts |
| `integration/console/` | Console authentication, commands, resource HTTP and operations |
| `integration/memory/` | Memory, State, Focus, notes, tasks, events and evidence |
| `integration/recall/` | FTS, vector, graph/profile projections, retrieval and publication |
| `integration/cognition/` | Persona and reflection pipelines |
| `integration/runtime/` | Provisioning, ingestion, workers, scheduling and Surface fencing |
| `integration/migrations/` | Database schema upgrades |
| `integration/recovery/` | Packaging, backup, restore and deletion resurrection |
| `fault/` | Fault injection |
| `performance/` | Wall-clock latency budgets without coverage instrumentation |

For example, Focus promotion is in
[integration/memory/test_focus_promotion.py](integration/memory/test_focus_promotion.py).
[The old-to-new path map](path-migrations.json) resolves paths used in historical
phase reports. Test identifiers and assertions are retained across that relocation.
Cross-module fixture imports remain supported; moving fixtures into their own
modules can be done separately without relabeling integration tests as unit tests.

```sh
make test-affected TESTS="tests/integration/memory/test_focus_promotion.py"
make test
make test-performance
```

`make test` runs functional tests with branch coverage and the 80% floor.
`make test-performance` runs every latency test sequentially without coverage;
`make ci` requires both. Preserve wall-clock timing, sample counts and budgets.
Do not use CPU time, automatic retries, or relaxed budgets to mask contention.
Avoid other heavy work during local latency measurement. Shared CI hardware can
still vary; production latency acceptance requires the fixed hardware and load
profile specified by Phase 14, including its separate Soak campaign.
