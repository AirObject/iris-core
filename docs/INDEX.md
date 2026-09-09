# 文档总入口

先查看 [当前任务及批准状态](work/CURRENT_TASK.md)、[实际完成情况](work/STATUS.md)，再检查当前目录、分支和工作区差异。按下表读取本次工作涉及的主题与契约；点击章节或验收行可直接定位，无需默认通读全部材料。

## 权威原文与维护关系

| 权威材料 | 实际文件 | 职责 |
| --- | --- | --- |
| 独立陪伴角色记忆与认知系统设计 | [系统原文](../companion_memory_cognition_system_design_integrated.md) | 产品行为、业务边界、逻辑对象及产品验收 |
| 独立陪伴角色记忆与认知系统：技术架构与模块设计草稿 | [架构原文](../companion_memory_module_design_provider_logging_config.md) | 工程约束、模块所有权、事务与恢复、实施候选及工程验收 |
| 代码规范：实现自描述与文档边界 | [完整代码规范](../CODING_STANDARDS.md) | 命名、文件独立可读性、注释、文档与运行时边界及审查要求 |

三份原文相互补充，不按文件名、修改时间或篇幅决定覆盖关系。产品材料未选语言或存储，不否定架构材料补充的工程约束；候选仍待批准，不能反向改变产品行为。代码规范约束实现表达，文档内的模块和验收编号可以继续用于追踪。

`product/`、`architecture/` 和 `modules/` 是原文的阅读视图，**原文仍是权威依据**，不是第二套可以独立修改的需求。后续修改规则时，先更新对应原文，再同步受影响的阅读视图与相对链接。[逐节覆盖映射](work/ORGANIZATION_REPORT.md#coverage)仅保留一次性整理历史，不要求每个代码任务重做逐节哈希、全文还原或全量覆盖映射；历史校验结果不代表当前文件版本。不得只修改拆分正文，任其与原文分叉。每份视图注明原文章节及整理时行号；验收表保留完整行，导航不能代替详细规则。

完整代码规范已有独立表达，直接链接原文，不另复制。它禁止实现反向依赖规划编号和文档，不禁止文档互相引用。原文中的代码树、prompt和字段草图仍属于设计示意，不能连同编号机械复制进运行资源。

## 按任务选择阅读路线

涉及实现的任务统一先读 [完整代码规范](../CODING_STANDARDS.md)，然后选择一行。模块入口含职责、非职责、所有权、公开能力、配置、验收与待定项。下列“追加阅读”只在任务触及该边界时读取；不递归把所有相关链接都当成必读。

| 要做的事 | 该任务的主要阅读 | 涉及关联边界时追加 |
| --- | --- | --- |
| 理解产品范围或确定新任务边界 | [角色、术语与对象](product/overview.md#section-01)、[模块所有权](architecture/ownership.md)、[当前状态](work/STATUS.md) | [产品未定契约](product/decisions-and-delivery.md#section-22)、[工程冻结点](architecture/implementation-options.md#source-line-1216) |
| 接入原始事件与入口授权 | [接入模块](modules/ingress.md)、[原始语境与管理分离](product/input-and-media.md#section-04)、[接收事务](architecture/persistence-and-transactions.md#t01) | 附件读 [媒体模块](modules/media.md)；专注期读 [门控表](product/dream.md#source-line-780) |
| 缓存批次、成功或失败终结 | [缓存模块](modules/buffers.md)、[队列与三终态完整规则](product/batches-and-learning.md#section-06)、[来源与所有权](product/provenance-and-memory.md#section-08)、[冻结到终结事务](architecture/persistence-and-transactions.md#section-07) | 媒体引用读 [清理边界](product/input-and-media.md#source-line-187)；改窗口读 [下批生效](architecture/configuration.md#source-line-878)；回流读 [移交](product/dream.md#source-line-815) |
| Provider能力或调用控制 | [Provider模块](modules/provider.md)、[能力／归因／准入／账本／恢复完整设计](architecture/provider.md)、[发送与结算事务](architecture/persistence-and-transactions.md#t10)、[配置快照与激活](architecture/configuration.md#source-line-878) | 构建输入读 [上下文与成本](architecture/context-and-cost.md)；发诊断读 [日志隔离](architecture/logging.md#source-line-739)；供应商事实定位 [原参考资料](architecture/references.md) |
| 日志、审计或输出 | [日志模块](modules/logging.md)、[等级、三端输出、脱敏、背压完整设计](architecture/logging.md)、[事务审计](architecture/persistence-and-transactions.md#t13)、[配置准备与发布](architecture/configuration.md#source-line-895) | Web读 [管理权限](modules/management.md)；计费关联读 [Provider统计口径](architecture/provider.md#source-line-618) |
| 配置注册表、快照或热修改 | [配置模块](modules/configuration.md)、[注册表／快照／生效边界／权限完整设计](architecture/configuration.md)；本次只读切片见[当前有效契约](architecture/configuration.md#configuration-registry-contract)及[批准范围](architecture/configuration.md#configuration-registry-decisions) | [配置热修改事务](architecture/persistence-and-transactions.md#t12)仅用于区分后续承诺；按消费者选一个模块；改业务数值读 [参数性质表](product/decisions-and-delivery.md#section-23)；迁移读 [检索空间](modules/retrieval.md) |
| 内部学习与候选校验 | [认知模块](modules/cognition.md)、[学习及目标范围](product/batches-and-learning.md)、[来源](product/provenance-and-memory.md)、[上下文与成本](architecture/context-and-cost.md)、[成功提交流程](architecture/persistence-and-transactions.md#source-line-501) | 模型错误读 [Provider错误与准入](architecture/provider.md#source-line-565)；正式写入读 [记忆模块](modules/memory.md) |
| 正式记忆、来源、关系或生命周期 | [记忆模块](modules/memory.md)、[来源与关系](product/provenance-and-memory.md)、[双指标／反馈／遗忘／删除](product/lifecycle.md)、[使用事务](architecture/persistence-and-transactions.md#t07) | 对外读取读 [入口隔离](product/retrieval.md)；依赖影响读 [梦境来源修复](product/dream.md#source-line-845) |
| 回复准备、召回或索引 | [检索模块](modules/retrieval.md)、[分区返回与缓存隔离](product/retrieval.md)、[时限及接口草图](architecture/request-paths.md)、[索引恢复](architecture/persistence-and-transactions.md#source-line-526) | 反馈读 [生命周期](product/lifecycle.md#source-line-519)；模型增强读 [Provider预算](architecture/provider.md#source-line-654) |
| 媒体保存、理解或清理 | [媒体模块](modules/media.md)、[媒体完整规则](product/input-and-media.md#section-05)、[持久化承诺](architecture/persistence-and-transactions.md#section-04) | 移交引用读 [缓存事务](architecture/persistence-and-transactions.md#t06)；理解调用读 [Provider](architecture/provider.md) |
| 当前状态及时间 | [状态模块](modules/state.md)、[外部权威的现在](product/current-state.md)、[时间待定契约](product/decisions-and-delivery.md#source-line-1160) | 回复分区读 [外部获取](product/retrieval.md)；写入门控读 [梦境能力表](product/dream.md#source-line-780) |
| 目标、去重或提醒 | [目标模块](modules/goals.md)、[多目标与提醒](product/goals.md)、[时序待定项](product/decisions-and-delivery.md#source-line-1154) | 修改持久状态读 [管理事务](architecture/persistence-and-transactions.md#t08)；模型判断读 [Provider](modules/provider.md) |
| persona或自我视图 | [自我模块](modules/self-model.md)、[稳定persona](product/self-and-persona.md)、[梦境发布](product/dream.md#source-line-762) | 配置读 [下梦境生效](architecture/configuration.md#source-line-878)；候选提交读 [发布事务](architecture/persistence-and-transactions.md#t09) |
| 模式、梦境、重启或回流 | 按职责选 [运行模块](modules/runtime.md)、[梦境模块](modules/dream.md) 或 [缓存模块](modules/buffers.md)；读 [梦境完整规则](product/dream.md)、[事务与恢复](architecture/persistence-and-transactions.md)、[切换待定项](product/decisions-and-delivery.md#source-line-1184) | 改模型发出门控读 [Provider发送核验](architecture/provider.md#source-line-575)；改管理入口读 [Web模块](modules/management.md) |
| Web管理与权限 | [管理模块](modules/management.md)、[产品管理面板](product/operations-and-management.md#section-20)、[专注下各能力](architecture/request-paths.md#source-line-980) | 只按实际页面选 [Provider](modules/provider.md)、[日志](modules/logging.md) 或 [配置](modules/configuration.md) 的受控接口 |
| 获授权后的持久化／部署／选型工作 | [工程约束与候选](architecture/deployment-candidates.md)、[持久化基础设施及依赖](architecture/ownership.md#i01)、[恢复契约](architecture/persistence-and-transactions.md)、[冻结点](architecture/implementation-options.md#source-line-1216) | [运行支持基础设施](architecture/ownership.md#i02)、[工程验收](architecture/acceptance.md)、[参考资料](architecture/references.md)；本轮不开展此类验证 |
| 修改设计或核对整理完整性 | 对应权威原文、受影响的阅读视图；追溯整理历史时查[历史映射与待确认记录](work/ORGANIZATION_REPORT.md) | [产品完整验收表](product/acceptance.md)、[工程完整验收表](architecture/acceptance.md) 按受影响行核对 |

## 当前工作与异常处理

- [CURRENT_TASK.md](work/CURRENT_TASK.md)：记录当前任务；配置参数定义与只读注册表为“契约已批准，待实现”，业务代码尚未开始，等待下一次代码实现授权。
- [STATUS.md](work/STATUS.md)：在切片完成节点简短更新实际完成情况及限制。
- [TASK_TEMPLATE.md](work/TASK_TEMPLATE.md)：后续小任务模板。
- [ORGANIZATION_REPORT.md](work/ORGANIZATION_REPORT.md)：一次性整理的历史记录，保留当时的哈希、逐节归属与校验结果，不作为后续代码任务的持续维护清单。

遇到真实冲突或无法解释的缺失时，在当前任务中写明双方原文位置、原始措辞、影响范围和所需决定。停止受影响的操作，继续不受影响的工作；不能由导航摘要、某个示例或较新文件自行批准行为变化。没有实现、验证环境或执行结果时，明确记录“未建立／未执行”，不得据设计描述标记功能完成。
