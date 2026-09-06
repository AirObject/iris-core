# Phase 0 Verification Report

> 归档证据：以下版本、测试数量、耗时与覆盖率是本阶段执行时的历史快照，未在本次文档整理中重跑；不能作为当前发布已通过的证明。当前状态见[阶段索引](../development/README.md)，发布重验见[Phase 14](../development/phase-14-hardening-release.md)。
> 后续闭环：HTTP/进程入口已由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；旧报告中的应用层/mock 范围只描述当时环境。

> Result: Passed  
> Date: 2026-08-29  
> Implementation commit: `a3a3e1d5dba70dbb3b2a385fd443a2ca8cf6c557`

## Environment

| Component | Version |
| --- | --- |
| macOS architecture | Apple Silicon (`arm64`) |
| Python | 3.12.13 |
| SQLite | 3.50.4 |
| uv | 0.11.29 |
| Node.js | 26.5.0 |
| npm | 11.17.0 |

The repository requires Python 3.12+ and Node.js 22+. GitHub Actions fixes Python 3.12, Node.js 24, and uv 0.11.29.

## Reproducible gate

The following commands passed in the workspace and in a new `/tmp` copy with `.git`, `.venv`, `.uv-cache`, `node_modules`, build output, and test caches excluded before bootstrap:

```bash
make bootstrap
make ci
```

Results:

- Ruff format check: 33 files formatted.
- Ruff lint: passed.
- Domain import boundary: passed; the negative fixture proves an illegal `fastapi` import is detected.
- Documentation structure and local links: passed.
- Mypy strict check: 32 source files, zero issues.
- TypeScript strict check: passed.
- Generated OpenAPI/JSON Schema drift check: passed.
- v1 compatibility snapshot check: passed.
- Python tests: 19 passed.
- Python production/SDK coverage: 81.23%, threshold 80%.
- TypeScript SDK shared Fixture test: 1 passed.
- npm audit during locked bootstrap: 0 vulnerabilities.

## Contract and migration evidence

- API version: `v1`
- Contract version: `1.0.0`
- Package version: `0.1.0`
- Schema version: `1`
- Contract source SHA-256: `895f7ea20576309a4c81ae731ed160a2a42e64b8b9f11586fb530222766ec6d2`
- Migration: `0001_phase0_metadata.sql`
- Migration SHA-256: `ec236f7e3f5691119fed78f5ef4d0ffe9983bb117936e68d733cfee63342a861`

Migration tests cover empty-database upgrade, idempotent repeat, invalid filename, duplicate version, failed transaction, and applied-file checksum tampering.

## Accepted decisions

ADR-0001 through ADR-0008 are Accepted. The ADR index records no open conflict blocking Phase 1.

## Known limitations

- Phase 0 intentionally exposes only health, capability, negotiation, stable error, SDK, and migration infrastructure; no business-domain table or endpoint is implemented.
- The checked-in GitHub Actions workflow has not run on the remote service because the local commits have not been pushed. The same `make bootstrap && make ci` workflow passed in a dependency-clean local copy.
- Container, production topology, and host adapters remain in their planned phases.

## 原阶段验收目标

下列门槛从已归档阶段计划移入，保留未被实测证明的要求。它们是当时的验收目标，不能从本报告 Passed/Completed 标签推断逐项均已完成；是否达到须与前文的样本、测试与限制核对。尚未闭合项由 Phase 14 的发布矩阵承接。

- Python 3.12+ 与 Node.js 22+ 的干净环境均能复现；CI 固定 Python 3.12。
- 测试覆盖率不低于 80%，Format、Lint、Type Check 和全部测试零错误。
- 契约连续生成两次字节一致；兼容基线、Python Fixture 和 TypeScript Fixture 结果一致。
- Migration 至少覆盖空库升级、重复运行、未知文件名和已应用文件篡改四类场景。
