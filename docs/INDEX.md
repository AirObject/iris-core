# 文档总入口

先读[当前任务](work/CURRENT_TASK.md)和[已验收状态](work/STATUS.md)，核对仓库基线；再按下表只读本任务涉及的正文。

## 现行正文与冻结参考

2026-09-09用户明确调整文档权威关系：三份根目录原文移入[reference](reference/README.md)，成为冻结的原始参考版本，仅供设计和审核追溯，不能直接作为当前实现或验收约束，也不再修改。

现行维护关系如下；这是文档组织迁移，不改变已批准行为，不批准建议、示例或日志草案，不授权业务实现。

| 唯一维护位置 | 内容及边界 |
| --- | --- |
| `product/` | 产品行为、信息对象、业务边界及产品验收条件 |
| `modules/` | 各模块职责、所有权、端口及相关契约导航 |
| `architecture/` | 共享工程约束、事务与恢复、技术契约、候选及工程验收条件；配置和日志详细契约仍在其现有文件唯一维护 |
| [CODING_STANDARDS.md](CODING_STANDARDS.md) | 完整现行代码规范；涉及实现时必读 |
| [CURRENT_TASK](work/CURRENT_TASK.md)／[STATUS](work/STATUS.md) | 当前执行状态／已验收里程碑；均不替代正文中的批准状态 |
| [reference](reference/README.md)及[整理历史](work/ORGANIZATION_REPORT.md) | 仅作历史参考，不属于日常必读或现行规则来源 |

每项规则只修改其所属正文，其他位置用链接引用；不维护“原文＋视图”两套正文。现行文件保留迁移前已确定、已批准、建议、示例、待批准的区别；正文尚未形成的决定仍须用户明确批准。参考版本中的内容不能因被引用而恢复为要求；发现遗漏时提出定点建议，不直接按旧版实现。

现行正文互相补充；真实冲突须说明双方位置并停止受影响部分，不能按文件名、时间或代码现状自行覆盖。契约只写批准状态，工作记录负责实现／验收／提交进度。已有`section-*`、`source-line-*`及语义锚点保留兼容；数字用于文档追踪，`source-line-*`不代表当前文件行号，不进入实现。

## 按任务阅读

涉及实现时先读[代码规范](CODING_STANDARDS.md)，再选择一行。链接到其他主题仅表示依赖，不要求递归通读。

| 任务 | 主要正文 | 必要边界 |
| --- | --- | --- |
| 范围与模块协作 | [产品概览](product/overview.md)、[模块所有权](architecture/ownership.md) | [待定产品决定](product/decisions-and-delivery.md)、[工程候选及顺序](architecture/implementation-options.md) |
| 接入 | [入口模块](modules/ingress.md)、[输入与媒体](product/input-and-media.md) | [持久化与事务](architecture/persistence-and-transactions.md) |
| 模式、缓存与学习批次 | [运行模块](modules/runtime.md)、[缓存模块](modules/buffers.md)、[批次规则](product/batches-and-learning.md) | [梦境门控](product/dream.md)、[事务与恢复](architecture/persistence-and-transactions.md) |
| 配置 | [配置模块](modules/configuration.md)、[配置正文](architecture/configuration.md) | 已批准[注册表](architecture/configuration.md#configuration-registry-contract)、[解析与快照](architecture/configuration.md#configuration-resolution-contract)；[激活事务](architecture/persistence-and-transactions.md#t12)仅在任务触及时读 |
| 日志 | [日志模块](modules/logging.md)、[日志正文](architecture/logging.md) | [待批准运行诊断草案](architecture/logging.md#runtime-diagnostics-contract)、[事务审计](architecture/persistence-and-transactions.md#t13) |
| Provider | [Provider模块](modules/provider.md)、[Provider正文](architecture/provider.md) | [调用事务](architecture/persistence-and-transactions.md#t10)、[上下文与成本](architecture/context-and-cost.md) |
| 媒体与正式记忆 | [媒体模块](modules/media.md)、[记忆模块](modules/memory.md)、[来源与关系](product/provenance-and-memory.md) | [输入与媒体](product/input-and-media.md)、[生命周期](product/lifecycle.md)、[事务](architecture/persistence-and-transactions.md) |
| 认知加工 | [认知模块](modules/cognition.md)、[批次学习](product/batches-and-learning.md) | [上下文与成本](architecture/context-and-cost.md)、[学习事务](architecture/persistence-and-transactions.md#t03) |
| 召回、当前状态与目标 | [检索模块](modules/retrieval.md)、[当前状态模块](modules/state.md)、[目标模块](modules/goals.md) | [召回规则](product/retrieval.md)、[当前状态](product/current-state.md)、[目标行为](product/goals.md)、[时限与接口](architecture/request-paths.md) |
| 自我、persona与梦境 | [自我模块](modules/self-model.md)、[梦境模块](modules/dream.md) | [自我与persona](product/self-and-persona.md)、[梦境规则](product/dream.md) |
| Web与权限 | [管理模块](modules/management.md)、[管理与审计行为](product/operations-and-management.md) | 按页面选择日志、Provider、配置的受控端口 |
| 存储、部署与选型 | [部署候选](architecture/deployment-candidates.md)、[基础设施](architecture/ownership.md#i01) | [事务恢复](architecture/persistence-and-transactions.md)、[实施顺序与决定](architecture/implementation-options.md) |
| 验收与技术资料 | [产品验收](product/acceptance.md)、[工程验收](architecture/acceptance.md) | [技术参考](architecture/references.md)；外部资料不自动批准工程方案 |

## 工作记录

[CURRENT_TASK](work/CURRENT_TASK.md)只保留当前目标、授权、契约链接、阻塞、最近验证及停止点；使用[简短模板](work/TASK_TEMPLATE.md)更新，不追加历史全文。[STATUS](work/STATUS.md)仅记录已验收里程碑及版本证据。已提交历史通过提交号和原路径读取；未提交且独有的信息须保留。[ORGANIZATION_REPORT](work/ORGANIZATION_REPORT.md)保持历史用途，不持续重算全文哈希或覆盖映射。
