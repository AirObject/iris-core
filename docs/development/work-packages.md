# 工作包执行队列

2026-09-07 项目负责人决定：停止“根据构建文档实现 Phase 14”整体 goal。后续依照完成情况核查编写了[构建指导](next-build-guide.md)，本页只维护队列、状态与交接；具体范围、依赖和验收统一在指导文档。更新队列不自动启动下一项。Phase 14 尚未完成，也未进入稳定发布验收；Phase 11/12 保持 Deferred。

## 已保存成果

- 历史实现检查点为 `eda18d2`；最新核查代码基线为 `e2a6bbd`，Core 0.13.0 / Schema 20。此前 11680 passed / 1 failed 已有后续通过记录，不能继续标作最新失败。[工程复验](../reports/structure-and-release-readiness-2026-09-07.md#62-本次验证)记录功能 11676 项、性能 11 项分段通过，覆盖率 85.28%；[本轮核查](../reports/phase-completion-audit-2026-09-07.md)另有定向回归，未重跑全仓覆盖率。开发门禁通过仍不代表稳定发布完成。
- 未合入 Draft 原型：`codex/phase14-persona-drafts-checkpoint`，提交 `e639a2d`，基于上述主目录检查点保存 22 个差异文件。包含独立 Draft 版本、Schema 21 原型、授权命令与删除恢复账本，以及未完成的上下文/API。此前 22 个定向测试通过；最后的上下文/API 编辑尚未复验，路由未注册、请求契约/UI/Hold 审查/完整 Schema 21 回归未完成，ADR 仍为 Proposed。该分支用于续作，不能直接作为已验收版本合入。

## 当前执行授权

2026-09-08 最新用户指令进一步覆盖串行和逐包单独提交要求：按本指导完成剩余 W 至 W20，无依赖工作允许并行；关联的多个 W 可以合并提交，但按可审查行为拆分，避免大提交。依赖、原验收、收尾 make ci、报告与状态真实性仍保留；某包的外部验收缺失不阻止无依赖包开发，也不代表该包完成。当前 W05 本地收尾，W06/W08 已整合待完整 CI；W07/W09/W10 在独立副本开发；共享契约、迁移编号和最终集成统一协调。W18/W19 满足冻结输入和资源隔离后可并行运行，24h Soak 等原门槛不缩减。Phase 11/12 Deferred。

2026-09-08 最新补充：已按用户要求删除定时 automation `w01-w20`；继续现有 goal，以后台任务实际完成结果触发收尾和下一包，不设置定时阶段门、不执行 clock sleep。

2026-09-07 用户指令覆盖此前“完成后停止、下一包另行下达”的规则：使用 goal 严格按 W01→W20 串行推进，每包全部验收、收尾 `make ci`、报告/队列更新并单独提交后立即进入下一包，无需逐包确认。长任务后台运行，在[持久检查点](serial-execution-state.json)保存会话/进程、日志和实际结果；后台验收未完成不得关闭或跳包。Phase 11/12 保持 Deferred。下文历史下达描述以本段及最新补充授权为准。

2026-09-08 本轮补充授权：生产平台/硬件/数据规模、性能与 RPO/RTO 预算、真实 Provider 与凭据、发布操作人与 GitHub/PyPI 配置全部标为待定，**暂不实现生产验收**。继续独立本地功能与开发门禁；W15–W20 的生产验收保持未通过，W18/W19 正式批次不启动。W20 已提前完成独立的 Core/SDK 发布选择修正（`a088c5f`），仅属准备工作，见[报告](../reports/w20-release-delivery.md)。

## 下达与收尾规则

每次只下达一个 W 工作包，写明其对应原阶段/需求 ID、交付范围、必要依赖和验收清单。W 编号仅用于后续执行，不是新 Phase。完成后记录提交与实测结果并更新状态，结束该目标；相邻功能、新发现问题和发布演练归入具名后续工作包，不自动扩大当前目标。

开发中运行受影响测试及必要的类型、契约、真实浏览器或迁移检查；Python 子集使用 `make test-affected TESTS="具体测试路径或 node ID"`。每工作包收尾执行一次 `make ci`，保留原有覆盖率与性能阈值。失败后定位并复测受影响项；仅在修正或明确的集成疑点需要时再次全量，不因未通过就自动循环执行无变化候选。检查点提交不等于验收通过，未关闭的失败必须明示。

## 交互式工作包队列

W01–W04 已完成全部验收并各自独立提交。W05 本地收尾；W06/W08 已整合等待完整 CI，W07 继续独立副本实施，W09/W10 依已交付接缝开始开发。不得跳过[指导中的必需依赖](next-build-guide.md#3-建议顺序与依赖)，开发进展和最终验收分开记录。

| 工作包 | 原范围 | 状态/交接 |
| --- | --- | --- |
| [W01 HTTP Recall 接线与能力声明](next-build-guide.md#w01-http-recall-接线与能力声明) | Phase 7/8/10，14.0-D/F | Completed；[W01 报告](../reports/w01-http-recall-assembly.md)，完整 `ci-002` 通过：11705 项功能测试、85.50% 覆盖率、20 项真实浏览器与独立安装 Recall；Commit `0959259` |
| [W02 Graph 原量化门禁](next-build-guide.md#w02-graph-原量化门禁) | Phase 8，14.0-D | Completed；[W02 报告](../reports/w02-graph-quantitative-gates.md)，456 项定向回归、11966 项功能/85.52% 覆盖率、完整 CI/浏览器/安装通过；Commit `4c6277b` |
| [W03 Persona 原量化门禁](next-build-guide.md#w03-persona-原量化门禁) | Phase 9，14.0-D | Completed；[W03 报告](../reports/w03-persona-quantitative-gates.md)，2800 Policy 案例、50 客户端与三次时钟轨迹、14768 项功能/85.55% 覆盖率、完整 CI 通过；Commit `460f7dd` |
| [W04 Operation 扩展与可信备份流程](next-build-guide.md#w04-operation-扩展与可信备份流程) | 13.11 最小前置 | Completed；[W04 报告](../reports/w04-operations-trusted-backup.md)，14795 项功能/85.58% 覆盖率、21 项真实浏览器及完整 CI/安装通过；Commit `d4998ed` |
| [W05 Embedding Provider 全链路](next-build-guide.md#w05-embedding-provider-全链路) | 13.9，14.2 | In progress；[W05 报告](../reports/w05-embedding-provider.md)，Embedding 配置/Worker/API 全链路 |
| [W06 认知 Provider 运行接线](next-build-guide.md#w06-认知-provider-运行接线) | Phase 10，14.0-D/14.2 | In progress；已整合生产适配器与治理接线，完整 CI 待运行；[报告](../reports/w06-cognitive-provider.md)保留真实模型待验收 |
| [W07 统计与运行观测](next-build-guide.md#w07-统计与运行观测) | 13.6 | In progress；独立副本，持久统计/授权桶/回填和真实页面 |
| [W08 Persona Draft](next-build-guide.md#w08-persona-draft) | 13.4 | In progress；已整合 Schema23 草稿生命周期/HTTP/UI；[报告](../reports/w08-persona-draft.md)，完整 CI 待运行 |
| [W09 Reflection/Candidate 管理](next-build-guide.md#w09-reflectioncandidate-管理) | 13.4 原队列漏项 | In progress；W06 独立副本续作 dry-run/replay/候选审核；真实模型验收仍待条件 |
| [W10 Retention/Hold 与清理状态](next-build-guide.md#w10-retentionhold-与清理状态) | 13.5 | In progress；基于 W08 草稿实现开发策略/Hold/分页扫描；依赖最终验收仍须完成 |
| [W11 业务导出与授权下载](next-build-guide.md#w11-业务导出与授权下载) | 13.7 | Planned |
| [W12 手动导入闭环](next-build-guide.md#w12-手动导入闭环) | 13.8 | Planned |
| [W13 Settings 与实际生效](next-build-guide.md#w13-settings-与实际生效) | 13.10 | Planned |
| [W14 其余运维与部署前读面](next-build-guide.md#w14-其余运维与部署前读面) | 13.11 | Planned |
| [W15 兼容、产物与验收输入冻结](next-build-guide.md#w15-兼容产物与验收输入冻结) | 14.0/14.1 剩余闭环 | Planned |
| [W16 生产装配与进程验证](next-build-guide.md#w16-生产装配与进程验证) | 14.2 | Planned |
| [W17 安全、密钥与供应链](next-build-guide.md#w17-安全密钥与供应链) | 14.3 | Planned |
| [W20 发布验收与交付](next-build-guide.md#w20-发布验收与交付) | 14.6 | Preparation in progress；独立发布选择已提交；完整功能/CI和生产验收未完成，生产输入按用户要求待定 |

## 14.4 / 14.5 独立后台批次

两项对应 W18/W19，均为 Planned/待排期，当前未创建定时任务或启动长跑。目标环境、冻结候选及启动窗口尚未确定；先完成对应运行脚本与前置能力，再单独下达执行。具体场景和硬门禁分别见 [W18](next-build-guide.md#w18-性能容量soak-与进程故障)、[W19](next-build-guide.md#w19-恢复升级与回退)。

| 批次 | 输入与前置条件 | 固定执行范围与停止条件 | 归档产物 |
| --- | --- | --- | --- |
| 14.4 性能/容量/Soak/故障 | 冻结 Commit/镜像摘要、目标硬件、数据规模与预先确定的预算 | 一轮基线/容量/故障矩阵及至少 24h 的混合 Soak；启动时固定总时限，到期结束，失败即记录，不自动无限重试或修改候选 | 数据集与参数、逐阶段日志、延迟/容量/资源曲线、故障证据、通过/失败报告 |
| 14.5 恢复/升级/回退 | 已验证备份、删除/授权账本与密钥恢复方案、隔离环境、冻结候选 | 3 轮安装→Seed→升级→备份→恢复→回退；启动时固定每轮及总时限，失败保留现场并结束批次 | 备份与候选摘要、完整性/权限/不复活检查、RPO/RTO、运行手册与逐轮结果 |

未来后台任务仅在完成、失败或需要处理时通知；运行期间不自动扩展范围，不让交互式 goal 等待 24h。性能和恢复阈值仍遵守原阶段文档，拆分执行不代表取消发布要求。
