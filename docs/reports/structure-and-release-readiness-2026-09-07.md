# Iris Memory Core 项目组织与发布就绪度评估

> 状态：原评估已完成；2026-09-07 后续核实与修复见 §6。§1–5 保留原审计基线，不代表修复后的当前状态。
> 日期：2026-09-07
> 评估对象：`iris-memory-core` Core 0.13.0 / Schema 20 / Contract 1.10.0 / Console Contract 1.1.0
> 门禁实测基线：`d5c0cb8e63ea519ae2804af29c390069019f19db` 的工作区（当时未提交的文档、`.gitignore`/`.gitattributes` 与 `application/` → `hosts/` 改名已于同日提交为 `f81ea18` / `980cc6b` / `49d871c`；`src/` 与 `tests/` 内容与 `d5c0cb8` 一致）
> 补充复测基线：`49d871c` + 未提交的性能测试修复（见 §2.3）
> 环境：macOS 26.6.2 ARM64、Python 3.12.13、SQLite 3.50.4、Node v26.8.1、uv 0.12.9、Google Chrome 152.0.7977.82
> 边界：所有门禁均带 `development_sqlite_override=true`；本报告不构成生产隔离、安全、Soak 或灾备验收

---

## 摘要

**已有部分的工程质量高，但发布范围不完整。** 这两件事需要分开评价。

除一项负载敏感的性能门禁外，`make ci` 的全部环节在本机实测通过：11,680 个测试通过、覆盖率 85.24%、mypy strict 344 文件、20 个真实浏览器用例、全新构建 + 隔离安装验证均通过。分层边界、打包边界、契约漂移、公共接口快照都有机器门禁强制，不是靠约定。

但按项目自身的追踪：Phase 13 的 27 个功能面中有 8 个仍标注「未发布」且前端为 mock；Phase 14 的六条发布门禁全部未关闭；认知 Provider 目前只有确定性桩实现。**当前不满足稳定发布标准**，项目文档对此的表述是准确的。

---

## 1. 文件组织

### 1.1 已验证成立的部分

以下不是印象，是逐条核对过的：

| 结论 | 核对方式 |
| --- | --- |
| 分层方向真实成立 | 扫描全量导入图：`domain/` 对外零依赖（`tools/check_import_boundaries.py` 强制）；`storage/` 反向依赖 `application/ports`（17 处）；`api/app.py` 对 `storage`/`indexing` 的 6 + 2 处引用位于组合根，属正常装配 |
| 打包边界干净 | 实际构建：wheel 803 KB / 188 成员；sdist 顶层仅 `src`、`migrations`、`schemas`、`contracts`、`README.md`、`LICENSE`、`pyproject.toml`；无 tests、docs、web、hosts、`.uv-cache`、`.venv` 泄漏 |
| 运行时资源无遗漏 | 源码中 4 处 `runtime_resource()` 调用与 `pyproject.toml` 的 3 条 `force-include` 规则完全对应 |
| 文档职责有划分且有门禁 | `docs/README.md` 的分工表 + `check_docs.py` 校验根目录/docs/sdk/hosts/console 的链接与锚点 |
| 生成物有漂移门禁 | 契约生成、兼容基线、公共接口快照（含已安装 wheel）全部机器校验，实测通过 |

规模基线：`src/**/*.py` 79,652 行 / 160 文件；`tests/**` 约 63.6k 行 / 162 个测试文件；`tools/**` 9,566 行；仓库跟踪 1,320 个文件，其中 794 个（60%）在 `schemas/` 下且全部为生成物。

### 1.2 优化建议

按建议优先级排序。每条都附了核对依据。

---

#### 建议 1：统一契约编写范式（价值最高）

当前 `/v1` 与 `/console/v1` 使用两套互相矛盾的模型：

| | 源文件 | 生成器 |
| --- | --- | --- |
| `/v1` 业务 | `contracts/source/contracts.json` **2,333 字节** | `tools/generate_contracts.py` **5,745 行 / 228,745 字节** |
| `/console/v1` | `contracts/source/console.json` **622,022 字节**（声明式） | `tools/generate_console_contracts.py` **111 行** |

`contracts.json` 里只有 `api_version`、`capabilities`、`contract_version`、`error_codes`、`package_version`、`schema_version` 六个键——**没有任何路径或 Schema 定义**。业务侧实际的 86 个路径 / 92 个操作全部以 Python dict 字面量硬编码在生成器中。

后果：`README.md` 写的「Edit `contracts/source/contracts.json` for `/v1`」对不上实际操作——新增业务端点必须编辑那个 5,745 行的 Python 文件。

建议：将 `/v1` 收敛到 Console 已验证可行的声明式模型。这一项同时消除全仓最大的源文件和 README 的错误指引。

---

#### 建议 2：拆分 `application/ports.py`

| 指标 | 数值 |
| --- | --- |
| 行数 | 2,758 |
| Protocol / 类 | 39 |
| 方法 | 522 |
| 被引用的源文件 | 76 / 160 |
| 其中 `Transaction` 单个协议 | 285 行、75 个方法 |

任何端口签名变更都会触发大范围重新类型检查。`application/console/ports.py`（109 行）已经示范了按上下文切分的可行做法，建议按有界上下文拆为 `ports/memory.py`、`ports/persona.py`、`ports/recall.py` 等。

---

#### 建议 3：测试按主题而非构建历史组织

| 现象 | 数量 |
| --- | --- |
| 命名为 `test_phaseN_*` 的测试文件 | 56 / 162 |
| 命名含 `review_roundN` / `regressions` | 13 |
| 平铺在 `tests/integration/` 单一目录 | 131 / 162 |

迁移测试同时存在 4 种命名法：`test_migrations_phase2.py`、`test_phase5_migration.py`、`test_state_deletion_migration.py`、`test_migrations.py`。

对一个准备对外发布的包，「Focus 提升的覆盖在哪个文件」无法从文件名回答。建议在功能面清空后、RC 冻结前作为独立工作包处理，避免与功能切片的验收证据混在一起。

> 备注：工作区已有一份独立的《测试分层与 pytest 耗时诊断报告》（`docs/reports/test-layering-analysis-2026-09-07.md`，未跟踪），其结论是「存在错层，但迁移到 unit 不是主要提速手段」。本条关注的是**可维护性/可检索性**，与该报告的**执行耗时**议题是两个不同的问题，建议分别处理。

---

#### 建议 4：为散文中的数字增加断言门禁

`check_docs.py` 校验链接与锚点，但不校验数字，因此计数会静默漂移：

| 位置 | 文档写的 | 从生成物实测 |
| --- | --- | --- |
| `web/console/INTEGRATION_MATRIX.md`（标注 2026-09-07） | 97 个路径、111 个 HTTP 操作 | **125 个路径、152 个操作** |
| `docs/reports/phase-14-verification.md` | 91 个业务操作 | **92 个操作**（86 路径） |

建议增加一个几十行的工具，从 `schemas/openapi/*.json` 读取实际计数并断言文档中的对应数字，纳入 `make lint`。

---

#### 建议 5：大模块（低优先级，属设计选择）

23 个源文件超 1,000 行，52 个超 500 行：

| 文件 | 行数 |
| --- | ---: |
| `application/recall.py` | 3,783 |
| `application/tasks.py` | 3,338 |
| `application/ports.py` | 2,758 |
| `storage/memory.py` | 2,464 |
| `storage/backup.py` | 2,074 |
| `storage/plans.py` | 2,053 |
| `api/app.py` | 1,963 |

架构基线明确写了「`domain/` 与 `application/` 的聚合模块使用平铺结构」，因此这是有意的设计选择，不是失控。但 `recall.py` 以 3,783 行容纳 25 个类，已经超过平铺结构还能带来收益的规模。

---

#### 建议 6：清理两个空壳包

- `src/iris_memory_core/security/__init__.py`：仅一行 docstring，包内无任何模块
- `src/iris_memory_core/coordinator/__init__.py`：仅从 `application.surface` 转出 2 个名字

两者的 docstring 都说明是「边界标记」，属于有意为之，但对新读者呈现为遗留脚手架。建议或补实内容，或在架构文档中明确标注其占位性质。

---

#### 建议 7：补齐发布标准文件

| 项 | 状态 |
| --- | --- |
| `CHANGELOG.md` | **缺失** |
| `SECURITY.md` | **缺失** |
| `pyproject.toml` 的 `classifiers` | **缺失**（已从构建出的 wheel METADATA 确认） |
| `pyproject.toml` 的 `keywords` | **缺失** |
| `pyproject.toml` 的 `[project.urls]` | **缺失** |
| PyPI 发布工作流 | **缺失**（`.github/workflows/` 下只有 `ci.yml`） |
| `README.md` / `LICENSE` / `CONTRIBUTING.md` / `.editorconfig` | 齐备 |

`sdk/python/pyproject.toml` 同样缺少 `authors`、`classifiers` 与 `urls`。

---

#### 建议 8：零碎项

- `web/console` 版本号为 `0.1.0`，与 Core `0.13.0` 不同步（该包 `private: true`，不发布，但属于发布范围的一部分）
- `hosts/` 下仅有两个 README，为暂缓的 Phase 11/12 占位
- 本地存在游离的 `sdk/typescript/.uv-cache/`（已被 gitignore，仅为本机残留）
- 文档语言分工：对外文档（根 README、CONTRIBUTING、两个 SDK README）为英文，内部文档（架构基线、Console 设计、阶段计划、验证报告、**安装运维指南**）为中文。其中 `docs/operations/core-installation.md` 是英文 README 直接指向的安装指引，对非中文使用者会形成断点

---

## 2. 门禁实测结果

以下为本机实际执行结果，非引用历史报告。

### 2.1 结果总表

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 格式 | `ruff format --check .` | ✅ 347 files |
| Lint | `ruff check .` | ✅ |
| 导入边界 | `python -m tools.check_import_boundaries` | ✅ |
| 文档链接/锚点 | `python -m tools.check_docs` | ✅ |
| 类型 | `mypy` | ✅ 344 source files |
| TS SDK 类型 | `npm run typecheck --prefix sdk/typescript` | ✅ |
| 契约漂移 | `python -m tools.generate_contracts --check` | ✅ |
| 契约兼容 | `python -m tools.check_compatibility` | ✅ |
| 公共接口快照 | `python -m tools.check_public_api` | ✅ |
| **Python 测试** | `pytest` | ⚠️ **11,680 passed / 1 failed**，1134.55s（18:54），覆盖率 **85.24%**（阈值 80%） |
| TS SDK 测试 | `npm test --prefix sdk/typescript` | ✅ 19 tests |
| Console 检查 | `npm run check --prefix web/console` | ✅ 29 tests + 生产构建 |
| Console 真实浏览器 | `npm run test:browser --prefix web/console` | ✅ **20 passed**（1.3m） |
| 打包与隔离安装 | `python -m tools.check_packages` | ✅ 退出 0 |

打包检查回执：`core_version 0.13.0`、`schema_version 20`、`sdk_version 0.11.1`、`surface_mode required`、`public_sdk_smoke passed`、`worker_once passed`、`private_endpoint_rejections 4`、`serve_exit_code -15`、`shutdown_seconds 0.255`。

### 2.2 唯一失败项

`tests/performance/test_recall_latency.py::test_structured_recall_p95_under_50ms`

```
AssertionError: [5.23, 4.67, 4.48, 49.66, 50.47, 58.93, ...]
assert 59.192415967117995 <= 50.0
```

追查得到两个**互相独立**的结论：

**(a) 该门禁负载敏感，本次失败不是性能回归。**

| 条件 | 结果 |
| --- | --- |
| 全量套件内（带 coverage） | ❌ p95 = 59.19 ms |
| 紧随其后单独运行整文件（`--no-cov`） | ❌ p95 = 53.47 ms |
| 机器空闲后单测 ×3 | ✅ 全部通过 |
| 机器空闲后整文件 ×3 | ✅ 全部通过 |
| 机器空闲后整个 `tests/performance` 目录（11 项） | ✅ 全部通过 |

空闲状态下实测 p95 约 24 ms，距 50 ms 预算有一倍余量。这与项目自身报告中记录的 Hybrid Recall 门禁间歇失败属同一类现象：**门禁在全量 CI 条件下不稳定**。一个在完整运行中会非确定性失败的门禁，无法为 RC 提供可信的绿色证据。此项仍然开放。

**(b) 采样方法缺陷（现已修复）。**

原 `_measure()` 的 `request_id` 为 `perf-req-<topic>-<sample>`，每次调用从 0 编号；测试先以 3 个样本预热，再以 40 个样本测量，导致**测量批次的前 3 个 ID 与预热完全重合，走幂等重放路径**。实测对比：

| 调用 | 耗时 |
| --- | --- |
| 全新 ID（ids 0–2） | 24.6 / 23.6 / 23.5 ms |
| 重放同一 ID（ids 0–2） | 2.36 / 2.32 / 2.25 ms |
| 全新 ID（ids 100–139，40 样本） | p50 23.59 ms、p95 24.67 ms、max 24.75 ms |

即前 3 个测量样本比真实 Recall 快约 10 倍，而文件顶部的度量方法 docstring 写的是「第一次未缓存的调用作为预热被丢弃」，与代码行为不符。40 样本下 p95 的判定仍成立，但报出的 p50 是错的。

> **此项已在工作区修复**（截至 2026-09-07 尚未提交）：预热改用 `perf-warmup-` 前缀，两项测试均已修正，docstring 已更正，并对其余六个性能文件做了同类审计（均无此问题）。修复后本机复测：Structured p50 22.60 ms / **p95 24.78 ms** / max 30.43 ms；FTS p50 25.08 ms / **p95 26.41 ms** / max 26.62 ms，`2 passed in 3.62s`。

### 2.3 一项 CI 配置风险

`.github/workflows/ci.yml` 设置 `timeout-minutes: 30`，而该 job 需要完成：`make bootstrap` + `npx playwright install --with-deps chromium` + 完整 `make ci`。

本机（Apple Silicon）实测仅 pytest 一项就耗时 **18:54**，加上 Console 浏览器 1.3 分钟、生产构建与全新构建 + 隔离安装。`ubuntu-latest` runner 的 CPU 性能低于本机，**该额度大概率不足**。工作流注释中引用的 `436.09 s` 是 Phase 8 于 2026-09-03、8,128 个测试时的历史测量，当前已增至 11,681 个 case。

### 2.4 本地复现说明

Console 浏览器门禁首次运行时 20 项全部失败，原因为 Playwright 期望的 `chromium_headless_shell-1187` 未安装（本机缓存为 1208 / 1234），非产品缺陷。改用项目已支持的环境变量后 20 项全部通过：

```bash
CONSOLE_BROWSER_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  npm run test:browser --prefix web/console
```

---

## 3. 发布就绪度

### 3.1 结论

**当前不满足稳定发布包标准。** 项目文档对此的表述是准确的，本评估未发现被高估的完成度。

### 3.2 差距清单

**（1）Phase 13 功能范围未闭合**

`web/console/INTEGRATION_MATRIX.md` 的 27 个功能面中，8 个在契约/后端列标注「未发布」，前端仍为 mock：

| 功能面 | 说明 |
| --- | --- |
| 统计八个面 | 完全未发布 |
| 导出/下载 | 完全未发布 |
| 导入 | 完全未发布（依赖导出） |
| Provider | 完全未发布 |
| Settings | 完全未发布 |
| 保留/Hold 管理 | 完全未发布 |
| Reflection/Candidate/投影视图 | Canonical 只读已接线，管理动作与投影路由未发布 |
| Operation/运维/审计 | `memory_forget` Operation 已整合，其余未发布 |

**（2）Phase 14 六条发布门禁全部未关闭**

| 需求 ID | 剩余工作 |
| --- | --- |
| P14-BASELINE-01 / P14-PUBLIC-API-01 | Phase 13 其余资源写入/Forget、统计、导入导出、Provider、Settings、Operation 全链；逐方法权限与真实进程消费矩阵；同一最终 RC 的完整 CI |
| P14-DEPLOY-01 / P14-ISOLATION-01 | 生产 Python/SQLite 镜像、Compose、真实 Provider 或明确关闭、指标、SSE 多进程与客户端 OS 身份/文件隔离 |
| P14-SECURITY-01 | 供应链扫描/SBOM、静态加密、凭据/密钥轮换与恢复拒绝矩阵 |
| P14-PERF-01 | 固定生产硬件与预算后测量容量、24h Soak、每进程边界 20 次 SIGKILL |
| P14-RECOVERY-01 | 3 轮生产规模升级/恢复/回退，删除账本与撤销权限不复活，RPO/RTO |
| P14-RELEASE-01 | 双向追踪、不可变 RC/Release Manifest、发布评审及注册表安装回读 |

**（3）认知 Provider 只有确定性桩**

`src/iris_memory_core/runtime.py` 默认接线 `DeterministicCognitiveProvider` 与 `DeterministicEmbeddingProvider`。`providers/cognitive.py` 中不存在真实 LLM 实现——抽取、摘要、对账、Persona 演进目前均无真实能力。Embedding 侧存在 `HttpEmbeddingProvider`（OpenAI 兼容端点），但默认未启用。

README 对此有诚实披露：「passing local tests does not establish production retrieval or cognitive quality」。这不是隐藏缺陷，但意味着以「cognitive memory service」发布时，其核心认知能力尚未接通。

**（4）生产 SQLite Allowlist 从未满足**

包括本次在内的所有验证均带 `development_sqlite_override=true`。生产运行时 allowlist 尚未有任何通过证据。

**（5）性能门禁不稳定**

见 §2.2(a)。在解决之前，无法为任何 RC 产出可信的完整绿色 CI。

**（6）CI 超时风险**

见 §2.3。

### 3.3 建议推进顺序

1. **先修 CI 超时与性能门禁稳定性。** 这是前置项——在完整 CI 能稳定跑绿之前，后续任何工作包的验收证据都不可信。性能门禁需要一个明确决策：固定测量主机，或改用与负载无关的度量口径。
2. **补 `CHANGELOG.md` 与 PyPI 元数据。** 成本低，且是发布的硬性前置。
3. **按 `docs/development/work-packages.md` 已排好的队列推进功能面**，从 13.6 统计开始。
4. **内部重构（建议 1–3、6）安排在功能面清空之后、RC 冻结之前的独立工作包**，避免与功能切片的验收证据互相污染。
5. **14.4 / 14.5 后台批次**（Soak、灾备）按现有计划在冻结候选确定后单独下达。

---

## 4. 复现命令

```bash
# 快速门禁
uv run ruff format --check .
uv run ruff check .
uv run python -m tools.check_import_boundaries
uv run python -m tools.check_docs
uv run mypy
uv run python -m tools.generate_contracts --check
uv run python -m tools.check_compatibility
uv run python -m tools.check_public_api

# 完整 Python 回归（本机 18:54）
uv run pytest

# 前端与 SDK
npm run typecheck --prefix sdk/typescript && npm test --prefix sdk/typescript
npm run check --prefix web/console
CONSOLE_BROWSER_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  npm run test:browser --prefix web/console

# 全新构建 + 隔离安装
uv run python -m tools.check_packages

# 计数核对（用于建议 4）
python3 -c "
import json
for name in ('openapi', 'console'):
    d = json.load(open(f'schemas/openapi/{name}.json'))
    p = d['paths']
    ops = sum(1 for v in p.values() for m in v if m.lower() in ('get','post','put','patch','delete'))
    print(f'{name}: paths={len(p)} operations={ops}')
"
```

---

## 5. 本报告未覆盖的范围

- 未做生产环境部署、OS 身份隔离或容器验证
- 未做供应链扫描、SBOM 生成或密钥轮换演练
- 未做 24h Soak、容量测量或 SIGKILL 故障矩阵
- 未做升级/恢复/回退演练，未测量 RPO/RTO
- 未逐方法核对权限矩阵
- 未评估召回质量或认知输出质量（当前 Provider 为确定性桩，此类评估无意义）
- 未在非 macOS/非 ARM64 平台验证
- §2.1 的门禁结果对应 `d5c0cb8` 的源码状态；此后工作区已包含未提交的性能测试修复


---

## 6. 后续逐项核实与修复（2026-09-07）

本次按项目负责人确认的范围执行：**修复已实现部分的问题与工程缺陷，未开发功能另列清单**。开始时已有的本报告、测试分层报告、Phase 14 报告修改及 Recall 预热 ID 修复均保留。没有实施新功能、数据库迁移、真实 Provider 接线、生产部署或上传注册表。

### 6.1 核实结论与处理

| 原审计项 | 核实结论 | 本次处理 |
| --- | --- | --- |
| 建议 1：契约真源不完整 | 存在：业务生成器承载路径和 Schema，README 指引不完整 | 86 个路径、86 个 Schema 迁入业务 JSON 真源，`schema_files` 声明独立输出；生成器缩至 128 行。新增声明式变更、源不被修改及输出路径拒绝测试。两套 OpenAPI 和业务 JSON Schema 保持原字节，仅 version-manifest 的源摘要变化。见 [编写指南](../../contracts/README.md) |
| 建议 2：集中端口定义 | 存在维护成本，但“大范围类型重检查”是推断，不能单由引用数量证明耗时 | 39 个类按上下文拆为 18 个模块；应用与适配器导入具体模块，`application/ports/__init__.py` 保留兼容导出。Transaction 仍表达一个事务可用的完整资源面，不改变事务语义 |
| 建议 3：测试按构建历史组织 | 存在检索问题，属于组织改进 | 迁移 143 个测试/辅助文件；integration 按七个主题分组，阶段/审阅轮次文件名替换为主题或不变量名称。保留测试函数、参数、断言和历史 docstring；用例收集前后经路径转换完全一致。见 [测试导航](../../tests/README.md) 与 [旧路径映射](../../tests/path-migrations.json) |
| 建议 4：数字漂移 | Console 当前矩阵确实过期；Phase 14 的 91 不能直接认定错误 | `git show 692de12b:schemas/openapi/openapi.json` 实测 85 路径/91 操作，属于真实历史口径。保留历史数字，新增当前标记；矩阵更新到 125/152。`tools.check_contract_counts` 纳入 lint，校验当前计数与标记完整性，并覆盖 HEAD/参数项及缺失/重复/错误数字负例 |
| 建议 5：大模块 | 行数成立，“已超过收益规模”是设计判断，不是已证明的行为缺陷 | 本轮已拆最大生成器和端口集中模块。其他大模块继续按现有平铺基线维护，不因行数单独切割业务逻辑；后续按实际职责耦合立重构工作包 |
| 建议 6：空壳包 | 两者是有意边界，并非缺失业务实现 | 在架构 §33.1 明确 security 占位、实际安全实现位置及 coordinator 兼容导出职责 |
| 建议 7：发布文件/元数据 | 存在 | 增加 CHANGELOG、SECURITY，补 Core/SDK authors、classifiers、keywords、URLs；新增手动构建/发布工作流及[配置说明](../operations/publishing.md)。默认不上传，外部 Trusted Publisher、环境保护和正式验收仍未配置或完成 |
| 建议 8：Console 版本 | 独立 private 包不要求与 Core 同号 | 在 Console README 明确包版本、契约版本与运行时版本职责，保留 0.1.0 |
| 建议 8：hosts 占位 | 不属于本次发布范围，报告已说明暂缓，不是待修缺陷 | 保留说明及 Phase 11/12 延期状态 |
| 建议 8：嵌套缓存 | 本机存在，约 12 KB，已被忽略 | 清除 `sdk/typescript/.uv-cache/`；不改变仓库资源 |
| 建议 8：安装语言断点 | 存在 | 增加完整[英文安装指南](../operations/core-installation.en.md)，中英文互链，根 README 改指英文入口 |
| §2.2(b)：预热重放 | 已在起始工作区修复 | 保留独立 warm-up 前缀及说明，复验全部性能测试。重放会污染分布，但原文“p50 是错的”应理解为采样口径错误，不能推出任意这组样本的 p50 数值一定发生变化 |
| §2.2(a)、§3.2(5)：性能不稳定 | 原始失败与空闲复测支持负载敏感，但不足以证明“绝非性能回归” | `make test-performance` 独立、串行、无 coverage；`make test` 保留功能覆盖率 80% 门槛，`make ci` 同时要求两者。延迟计时、样本和预算不变。消除插桩干扰，不声称共享 runner 或生产性能已稳定 |
| §2.3、§3.2(6)：CI 时限 | 30 分钟缺乏余量，Ubuntu 必然慢于本机/大概率超时未经实测 | 时限增到 60 分钟并更新依据；仍需 GitHub runner 实际证据 |

补充限定：`check_import_boundaries` 机器门禁直接检查的是 domain 的依赖边界，不能把原报告中的手工全量导入核对表述成所有层都已有同等机器约束。

### 6.2 本次验证

性能阶段：`pytest tests/performance --no-cov -s`，**11 passed（44.87s）**。Structured p50 **25.01 ms** / p95 **29.44 ms**；FTS p50 **31.82 ms** / p95 **32.67 ms**；原预算分别为 50/100 ms。性能实测仍来自开发 macOS/SQLite 环境，不构成固定生产主机验收。

测试路径迁移前后，包含当时新增的四个计数门禁用例，共 **11,685 个收集节点**经旧路径映射逐项一致；随后增加两个契约生成负例/变更用例。没有通过改层、删除参数化案例或减少断言降低回归范围。

首轮功能回归在沙箱内因回环端口绑定被拒而出现 54 个 SDK mock 服务夹具错误，主动中止（521 passed）；不是业务断言失败。获准启动回环服务后重跑完整功能套件。重排路径时另修正一个多行 `__file__.parents` 的仓库根定位，并在重跑前完成该修正。

其余门禁通过：Ruff 格式/检查 **367 文件**、mypy **364 文件**、文档/计数/导入边界、契约生成/兼容、公共接口快照；TS SDK **19 tests**，Console **29 tests**、生产构建与 **20 browser tests（1.3m）**。全新 Core sdist→wheel、独立 SDK 构建与隔离安装通过；安装物公共接口、Required SDK Smoke、Worker 和四个私有路径拒绝通过，SIGTERM 排空约 **0.127s**。CI 与发布工作流 YAML 解析通过；没有在 GitHub/PyPI 实际运行发布。

另外直接比较 AST，39 个端口类的定义/方法体/签名全部保持一致；其他生产源码变更仅为端口导入路径。完整功能回归 `pytest --ignore=tests/performance -q` **11,676 passed（849.74s）**，覆盖率 **85.28%**，保留 80% 门槛。与独立性能阶段合计 **11,687 项通过**。功能回归期间只完成最后两份契约测试的纯路径改名；测试逻辑未改，最终路径及使用其夹具的 Required Surface 套件定向复验 **101 passed（10.90s）**，最终 mypy **364 文件通过**。最终收集再次确认全部原有节点可映射、无丢失，并包含新增六个工程门禁用例。

验证按完整 CI 的各目标分段执行（性能、完整功能覆盖率、其余 Make 门禁），没有把尚未执行的 GitHub CI、生产性能或发布工作流记作通过。

### 6.3 单独保留的功能与生产验收清单

以下确认存在，但按本次明确范围不作为已修复；继续由[单一工作包队列](../development/work-packages.md)承接：

| 范围 | 尚需完成 |
| --- | --- |
| Phase 13 | 13.6 八个统计面；13.7 导出/下载；13.8 导入；13.9 Provider 配置与真实运行接线；13.10 Settings；13.5 Retention/Hold；Reflection/Candidate 与投影管理；13.11 其余 Operation/运维/审计。13.4 Persona Draft 仍为独立待审原型 |
| 认知能力 | 当前运行时仍默认 DeterministicCognitiveProvider / DeterministicEmbeddingProvider；真实模型能力、质量评估或生产明确关闭策略均未闭合 |
| 14.2 装配与隔离 | 生产 Python/SQLite allowlist 证据、镜像/Compose、独立 OS 身份和文件隔离、SSE 多进程/客户端场景 |
| 14.3 安全与供应链 | 扫描/SBOM、静态加密、凭据/密钥轮换、恢复拒绝矩阵；新增 SECURITY 不等于完成安全验收 |
| 14.4 性能批次 | 固定生产硬件/规模/预算、容量、24h Soak 和每进程边界 20 次 SIGKILL；本轮无插桩性能阶段不替代这些要求 |
| 14.5 恢复批次 | 三轮生产规模升级/恢复/回退、删除与撤权不复活、RPO/RTO |
| 14.6 发布 | 逐方法权限与真实消费矩阵、同一冻结 RC 的完整 CI、双向追踪、不可变 Manifest/评审、PyPI 外部配置与注册表安装回读 |

**稳定发布结论保持不变：尚未就绪。** 本次处理的是可确认的仓库工程问题，没有将未开发功能或待执行的生产门禁改记为完成。
