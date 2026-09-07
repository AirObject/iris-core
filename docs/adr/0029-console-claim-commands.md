# ADR-0029：Claim 管理创建与证据化纠正

状态：Accepted，2026-09-07。实现 [Console 资源矩阵](../design/console-backend.md#51-资源矩阵) 的 Claim 管理能力，沿用 [ADR-0025](0025-console-command-authorization.md) 的真实 CommandActor 与当前授权边界。

## 领域写入

Console 创建必须明确选择具体主体或 Agent 自身，提供谓词、JSON 值和至少一条有效 Evidence。可接受现有领域支持的类别、文本、评分、有效时间及隐私标签；不能传入租户、origin、extractor_version 或 source_authority。新主张固定为 user_statement，表示操作者提交的陈述。Evidence 只接受已支持的资源种类、ID、可选固定修订、关系和片段，不允许自造高权威来源。

人工输入的 Claim 使用显式隐私标签，纠正保持其原有隐私信封；本入口不从 Evidence 自动复制正文。Evidence 的可见性、Scope、有效状态、当前修订和删除状态仍按所属领域检查。自动 Focus 提升的隐私继承继续遵守 ADR-0027。

ClaimService 的 create_for_command/correct_for_command 验证管理身份，复用原有创建、去重、修订、Evidence、双时间戳、CAS、水位、审计和 Outbox 事务。Required Surface 仅对该授权管理分支分流，普通 remember/correct 的 Lease 门禁保留。去重命中实际 Claim 后再次检查当前可见性与可写性，不能借创建请求给隐藏的已有主张添加 Evidence。

## 纠正与撤回

`claims/{id}:correct` 必须带 expected_revision。supersede 修改值或文本，并按现有领域规则使用 explicit_correction 权威；dispute 标记争议并保留原内容；retract 撤回并运行既有证据失效级联。前两者必须提供有效 Evidence，撤回可省略。主体、谓词、隐私、历史修订和来源身份不能用该动作随意覆盖。

所有模式追加修订，保留旧内容与双时间边界。管理适配层把已授权的 Claim ID 加入内部命令收据，不改变原业务纠正服务结果形状。幂等重放重新检查当前操作者、目标和请求证据；来源或结果已删除时不返回旧成功。响应读取固定 Claim 修订，但 Evidence 关联仍使用领域现有的当前集合语义，不把它声明为历史 Evidence 快照。

事务故障必须同时回滚修订、前驱结束时间、指针、证据、审计、水位和 Outbox。跨空间、只读、未显式授权的 Restricted 访问不得写入。服务端审计使用真实 console/key 操作者，不伪造 admin 或平台身份。

## 契约与界面

Console 1.1.0 开发候选新增创建与纠正两个 POST 操作及五个 Schema，发布创建表单和 correct 动作描述，继续无通用 Claim PATCH。浏览器切换到 dispute/retract 时隐藏并省略内容修改字段，防止预填旧文本意外进入非内容动作。动作可用性由当前授权与状态决定。

业务 `/v1`、Python/TS SDK、CLI 及 Core 0.13.0 / Schema 15 不变。此切片不代替其余管理模块或 Phase 14 生产发布门禁。验证见 [Phase 14 报告](../reports/phase-14-verification.md)。
