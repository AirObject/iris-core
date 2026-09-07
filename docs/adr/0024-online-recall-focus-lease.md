# ADR-0024：Recall 与 Focus 的在线 Lease 门禁

状态：Accepted，2026-09-06；落实 Phase 14.0-E 与 ADR-0010 的既定 Required Surface 约束。

## 决定

公共 `/v1` 的 Recall、Focus 创建及显式激活/状态转换携带可选 `lease_id`、`lease_epoch`。Core HTTP 装配必须注入 SurfaceCoordinatorService；Required 模式要求当前租约、Epoch 与认证 app_instance 一致。OFF 保持原行为，Advisory 使用既有协调器判定；内部维护 Worker 不通过这些在线 HTTP 入口。

Scope/Privacy 检查先于租约判定，防止将越权资源作为租约状态探针。首次执行在 Canonical 写事务内再次校验；幂等重放也先校验当前租约。Recall 在读取缓存前和发布响应记录的事务内校验，防止长查询期间失去租约仍发布结果。Lease Proof 不参与业务请求指纹，合法新租约可以重试同一业务请求。

`/v1` 契约升至 1.10.0，增加可选字段而不删除既有操作；继续使用既定 `lease_expired`、`lease_fenced` 错误。Python SDK 的 Focus transition 增加可选关键字；TS 的 Recall 与 Focus transition 增加可选字段。旧客户端在 OFF/Advisory 模式保持兼容；Required 模式本就要求持有并提交有效 Proof。

## 验证与回退

真实 HTTP 测试覆盖缺失 Proof、过期、旧 Epoch、非 Holder、合法 Holder 和重放；事务测试覆盖校验后抢占的竞争窗口。无数据库 Migration。回滚业务二进制不能把缺少门禁的旧版本用于 Required 部署；如需回退，先停写并确认受支持版本，不能静默降级 Surface 模式。
