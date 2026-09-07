# ADR-0023：安装资源与 Console Canonical ID

状态：Accepted，2026-09-06；落实 Phase 14.0-C 与 14.1 的既定范围。

## 决定

Core wheel 显式包含 Migration、两套 OpenAPI 和业务 capability 真源，通过内部包资源解析器读取。sdist 使用白名单，只包含重建 Core 所需文件；独立 SDK、前端、仓库根 application 宿主目录与开发缓存不进入产物。缺失或空 Migration 目录必须失败，不能报告 Schema 0 迁移成功。源码开发允许从经过布局校验的 editable checkout 读取同一真源，安装物不按当前工作目录寻找资源。

Console 的 ResourceView.id、ResourceRef.resource_id 与 ReferenceView.id 是 1–128 字符的不透明 Canonical 标识符，与现有详情路由参数一致。保持现有 UUID ID，同时允许 Reflection/Candidate 的确定性 ID；不重写 Canonical 主键或建立第二套 ID 映射。认证密钥、Session、请求 ID 等 UUIDv7 约束不变。客户端 URL 编码 ID，不能将它作为路径、表名或对象名执行。权限校验与绑定查询仍在服务端执行。

Console 契约补丁版本升至 1.0.1；补充确定性 ID、空 ID 和超长 ID Fixture。业务 `/v1` 契约不受本决定影响。

## 受控初始化

为安装后的服务新增离线 `init` CLI，事务内初始化租户、Agent/Persona、local Space、application 凭据，可显式登记和确认新的 actor Binding、设置 Surface 模式。操作要求可信 OS 身份；不开放匿名 HTTP 初始化或通用对象访问。凭据文件独占创建、0600、拒绝覆盖/符号链接，标准输出只含 ID。命令不是恢复流程，不用于重新绑定已有身份。

## 验证与回退

使用安装包的迁移/契约加载与真实网络 Smoke 验证资源，使用原 Reflection 读面回归及契约 Fixture 验证 ID。无需数据库 Migration。回退 Console Reader 不能恢复错误的 UUID-only 假设；旧客户端应保留 ID 字符串，不自行解析 UUID。

## 边界

资源打包不增加公共进程内 façade；内部模块可导入不代表受支持 API，也不构成同进程安全沙箱。OS 身份、文件权限与独立服务部署的隔离验收仍属于 Phase 14.2/14.3。此决定不表示 Phase 14 发布门禁已通过。
