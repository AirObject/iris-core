# 代码组织建议与设计冻结点

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：目录树和阶段表均是原文建议，没有对应实现。目录示例中的模块编号、三段简称和说明留在设计阅读材料中，不能机械复制进源码、测试、日志或运行时prompt。

来源：[原文 L1022–L1094](../../companion_memory_module_design_provider_logging_config.md#section-13)；[原文 L1198–L1236](../../companion_memory_module_design_provider_logging_config.md#section-15)。行号对应整理时的哈希基线。

按关联工作联合阅读：[代码规范](../../CODING_STANDARDS.md)；[本轮实际状态](../work/STATUS.md)；[待批准的小任务](../work/CURRENT_TASK.md)；[候选部署](deployment-candidates.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

<a id="section-13"></a>

<a id="source-line-1024"></a>

## 13. 代码组织建议

```text
repository/
  docs/
    requirements.md
    architecture.md
    modules.md
    api-contracts.md
    config-reference.md      由Schema生成，不手写重复默认值
    observability.md         日志等级/统计口径/保留边界
    adr/
  src/companion_memory/
    main.py                  只调用bootstrap，不在各模块重复初始化
    application/             跨模块用例编排、事务与运行上下文
    modules/
      ingress/
      runtime/
      buffers/
      media/
      cognition/
      memory/
      self_model/
      retrieval/
      state/
      goals/
      dream/
      management/            Web/管理用例，不保存配置或用量副本
      provider/              M13：唯一模型出口与计量
        ports.py
        service.py
        adapters/            chat_completions/responses/anthropic/embedding/rerank
        admission.py         账户/角色限流、预算预留、deadline
        ledger.py            request/attempt/usage/cost及统计
      logging_service/       M14：避免命名logging.py遮蔽标准库
        ports.py
        service.py
        sinks/               console/file/web
        redaction.py
        audit.py
      configuration/         M15：唯一配置模型与生效流程
        schema.py            参数注册、类型、单位、唯一默认值
        service.py
        snapshots.py
        activation.py
        secrets.py           只管理引用和接口，不泄漏明文
    infrastructure/
      storage/               SQLite仓储/UoW/迁移/备份实现
      indexes/               词法/向量引擎实现
      files/                 blob发布与GC
    shared/                  小型ID、时间、错误及通用接口
    bootstrap/               最小启动配置、应急日志、正式模块注入
  web/                       初始化与管理界面源码，构建后静态交付
  tests/
    unit/
    contracts/
    integration/
    recovery/
    load/
    evaluation/
    architecture/            检查模型出口、日志handler与配置旁路
  deploy/
    Dockerfile
    compose.yaml
  migrations/
```

该目录以Python为优先实现候选展示，不提前固定HTTP框架、ORM、模型SDK或向量库。每个模块可以先由少量文件组成；不为凑分层给每个模块创建几十个空目录。

业务规则与Repository接口可在模块内组织，实现放基础设施；模块之间通过公开端口或应用编排协作。架构测试限制跨模块访问私有实现。外部平台专用薄兼容层可以在宿主插件或独立适配项目实现，不把所有聊天框架打包进核心容器。


图示／代码框中的文档标记定位：[M13](../modules/provider.md#contract)、[M14](../modules/logging.md#contract)、[M15](../modules/configuration.md#contract)。这些标记仅用于本文阅读，不能进入未来实现。

<a id="section-15"></a>

<a id="source-line-1200"></a>

## 15. 实施顺序与设计冻结点

<a id="source-line-1202"></a>

### 15.1 开发顺序

| 阶段 | 交付范围 | 通过后再进入 |
| --- | --- | --- |
| 0 | [M15](../modules/configuration.md#contract)类型化Schema/快照、[M14](../modules/logging.md#contract)控制台+文件/事务审计、[M13](../modules/provider.md#contract)统一假Provider与调用账本、[I01](ownership.md#i01)基础 | 所有后续模块从开始就没有请求/日志/配置旁路 |
| 1 | [M01](../modules/ingress.md#contract)/[M02](../modules/runtime.md#contract)/[M03](../modules/buffers.md#contract)：持久接收、三段、三终态、幂等、重启；Web日志与模式只读页面 | 基础状态不会丢失、误删或重放 |
| 2 | [M04](../modules/media.md#contract)/[M06](../modules/memory.md#contract)：hash媒体、来源、对象关系、引用与统一事务 | 正式认知与来源/文件一致，审计可解释 |
| 3 | [M08](../modules/retrieval.md#contract)/[M09](../modules/state.md#contract)/[M10](../modules/goals.md#contract)本地路径：一秒查询、反馈、当前状态、目标注入及Provider统计页面 | 无生成LLM的查询边界与诊断开销可验证 |
| 4 | [M13](../modules/provider.md#contract)真实协议/能力/预算/完整usage，[M05](../modules/cognition.md#contract)结构化学习，异步embedding；[M15](../modules/configuration.md#contract)安全热发布 | 请求与成本可观察，错误/未知不漏报，配置切换不破坏旧任务 |
| 5 | [M07](../modules/self-model.md#contract)/[M11](../modules/dream.md#contract)自我、梦境、persona发布与恢复；验证配置下梦境生效及专注门控 | 梦境不破坏数据或成为配置/请求绕过通道 |
| 6 | [M12](../modules/management.md#contract)完整管理，[M14](../modules/logging.md#contract)日志历史/导出/背压，[M15](../modules/configuration.md#contract)迁移与回退，备份和综合压力测试 | 冻结首期发布与剩余容量边界 |

Web只读运行状态应在早期随骨架提供，不能等到最后才有办法排查梦境卡住。[第6阶段](implementation-options.md#source-line-1202)补齐完整界面，不表示早期完全没有管理观察能力。

<a id="source-line-1216"></a>

### 15.2 需要形成ADR的决定

草稿已给出方向，以下是正式实现前应批准的少量工程决定，而不是重新讨论产品主体：

| 决策 | 草稿建议 |
| --- | --- |
| 部署形态 | 单容器模块化单体、单调度领导者、持久卷、Web静态交付 |
| 权威存储 | SQLite/WAL/FULL优先验证；正式批准基于写延迟、备份和大规模读取测试 |
| 一秒与语义质量 | 不强依赖在线生成LLM；查询embedding短截止、明确词法降级；拒绝/错误与成功分别统计 |
| 本地恢复 | 单操作ID与原子提交；候选已保存则只重试提交，远程结果未知采用显式恢复决策 |
| 检索实现 | FTS5中文适配 + 本地向量索引接口；具体ANN后端经内存/删除/重建/过滤测试确定 |
| 独立Provider | [M13](../modules/provider.md#contract)唯一模型出口；能力与协议分离；逻辑请求/attempt/usage独立持久化；只有一层有限远程尝试 |
| 独立日志 | [M14](../modules/logging.md#contract)统一标准等级及三sink；运行诊断、事务审计与Provider账本分开；异步诊断有界背压 |
| 独立配置 | [M15](../modules/configuration.md#contract)唯一Schema/默认值/版本真相；不可变快照与分阶段激活；专注期只读；迁移/重启不伪装热改 |
| 专注切换 | 入梦先收尾，在梦期间只暂存，梦境发布后恢复业务，各入口独立回流 |
| 原始媒体 | 字节hash内容寻址、独立出现记录、阶段发布、引用驱动GC |

仍需具体数值的项目包括部署CPU/内存/磁盘预算、正常峰值、S1/S2/S3与token预算、反馈有效期、目标去重字段合并、提醒路由、时间格式、梦境中断管理员操作。它们应在对应模块开始正式编码前固定，不妨碍先验证输入/事务核心。

本草稿不把“预计记忆不超过输入”变成强制输出限制，不把“成本低”变成逐条重要性LLM预检，不把“支持API”变成所有provider功能等价，也不把“事务恢复”变成无限重复远程调用。
