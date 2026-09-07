# ADR-0035：Console 受控实体重定向与关联链授权

状态：Accepted，2026-09-07。继续 [ADR-0034](0034-console-identity-registry.md)，实现 `POST /console/v1/memory/entities/{id}:redirect`；实体属性和 tombstone 仍分别实施。

## 显式管理命令

请求仅携带 expected_revision、target_id 与 operator_request 原因。实体详情提供明确目标选择和不可撤销提示，不能按名字、导入内容或模型推断自动合并。静态 entity.redirect 使用 memory.write，源与直接目标都必须在当前租户、可见且可写；向下可读的 Grant 不因此获得全局写权。

IdentityService 把原 redirect 事务提取为共享方法，原公开入口继续要求管理员；Console 使用 CommandActor 和实际操作者审计，不伪造 admin。插入 redirect、CAS 更新源实体状态、追加历史与审计、发布 graph.apply 在同一管理幂等事务中提交。已存在出向 redirect 的实体不能重新改向；不提供自动撤销或改写历史接口。

原始 Binding、Relation 等引用 ID 保留。发生时身份按原时间读取，当前身份沿重定向链解析。Graph 的既有规则排除 redirected 节点，重新派生会移除其旧边，不把历史关系改写成目标实体的关系。

## 深度、容量与授权

既有链上限为 16 条边。Console 沿目标链读取有限条记录，并检查受影响源实体的前驱深度，防止在长链尾部新增边使更早实体超限。前驱递归最多读取 5001 行，超过 5000 个受影响节点返回暂不可用；同时受 150 ms 和 200 万 SQLite 步数预算限制。预算不修改连接的 query_only 状态，在全部路径清除进度处理器；失败不提交边、修订、审计或成功回执。

管理入口把已检查的有限映射传给共享事务方法，避免加载整个租户的 redirect_map。原公开业务入口保持既有行为，本切片不宣称已经重构所有业务遍历或完成生产容量验收。

Console Entity 读面增加 redirect_entity_id；重定向前的历史修订显示空目标，不向历史回填新指针。每次读取，包括缓存命令结果及历史，均以当前出向链作为必要授权依赖。任何中间实体或终点被删除、隐藏或形成非法循环时，源对象的当前、历史与缓存结果都拒绝披露。指针不绕过关联实体授权，完整历史语义仍由 Canonical 保留。

## 验证与兼容

新增一个 Console 操作、一个请求 Schema、六个夹具和 redirect 动作枚举。Core 0.13.0、Schema 15、业务 HTTP、SDK、CLI 保持不变。验证覆盖 CAS、严格字段、两端权限、当前关联链删除、16 层边界、5000 个前驱预算、提交故障回滚、稳定重放、真实图投影、隔离恢复和浏览器操作。结果及失败修正见 [Phase 14 报告](../reports/phase-14-verification.md)。实体属性、tombstone/Forget 与生产门禁仍待完成。
