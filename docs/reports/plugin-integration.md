# 插件接入补齐验收报告（2026-09-08）

本轮对应插件评审 G-01～G-09 的本地/远程接缝；实现与逐项回填见 [接入指南](../development/plugin-integration.md)，范围决策见 [ADR-0053](../adr/0053-embedded-plugin-integration.md)。没有修改插件仓库，未关闭既有 W 工作包。

## 已验证结果

- 本地公共入口完成观察、明确记忆、幂等重放、Recall、纠正、遗忘与重启持久化，全程无监听端口。
- 本地专项测试覆盖 50 次生命周期、同进程/跨进程目录互斥、启动各阶段失败、启动取消、关闭 deadline、Required Lease、Provider 所在事件循环/取消/共享资源，以及多人格注册与重启。13 项通过。
- SDK 原生异步传输覆盖真实 TCP 慢请求、断开、外层取消、关闭、连接借用与并发上限，新增 Profile/能力协商 wire 检查通过。
- Embedded 与 SDK→真实 TCP→Core 使用同一数据库上的业务规则，验证记忆读取、Recall、权限、人格镜像/CAS/回滚与遗忘重启。独立的 `persona.mirror.v1` 授权通过，普通 application 凭据发布失败。
- TypeScript 20 项测试通过，新增 Profile 与 requiredCapabilities wire 测试。
- Core 基础安装、SDK-only 安装、Core `[server,vector,console]` 安装在三个独立环境通过。服务安装消费验证 Required Lease、独立 Worker，以及真实 HTTP Graph/Vector 命中和四类重建；确定性 Embedding 不算真实模型质量验证。
- 源码静态类型、格式/lint、领域导入边界、契约生成/兼容/计数、公开接口快照通过。

完整约 1.5 万项测试曾启动但未完成，不能据此声称全量回归或覆盖率门禁已通过。首次受影响集合为 1161 项通过、1 项失败；失败是新测试中 committed/occurred 时间分别取样导致先后倒置，已修正为同一取样。最终受影响集合结果记录在 `evidence/plugin-integration/verification.json`。

## 安装与容量证据

[基础安装消费记录](evidence/plugin-integration/embedded-wheel.json) 使用 Python 3.12.13 / SQLite 3.50.4 / macOS arm64，显式使用开发 SQLite 例外。当前 Embedded 使用 Unix flock；macOS 已实测，Linux 可按相同脚本验证，Windows 尚未支持/认证。正式 SQLite allowlist 没有被放宽。

所附一次合成单条记忆、无模型、关闭后台的隔离运行：冷启动约 486 ms；50 次重启约 10～17 ms。累计进程峰值 RSS 六个采样点均为 65,961,984 字节（约 62.9 MiB），不代表 Core 净增量或保留堆。另一次完整重装复跑冷启动约 2.1 秒、累计峰值约 64.5 MiB，说明这些采样也受环境影响。退出后检查实例在途工作为 0；该趋势与资源归属检查一起构成有限生命周期证据，不能外推到大索引、重建峰值、真实宿主或长期运行。

基础 Core 仅安装 jsonschema 及其依赖，import/构造/启动均未加载 FastAPI、Uvicorn、NumPy、FAISS。SDK-only 环境没有 Core、向量引擎或 Web 框架。构建 wheel/sdist 后按实际归档检查资源与依赖；摘要见 [发行物清单](evidence/plugin-integration/artifacts.json)，Core 归档内容检查见 [分发报告](evidence/plugin-integration/distribution.json)。

基础源码已有 Schema 24 迁移/运行时与业务能力声明 23 不一致，本轮同步业务 source/version manifest 和安装 smoke 断言到 24。业务契约为 1.12.0；Core/SDK 包仍带原开发版本标记，尚未进行新版本发行，必须按本报告 wheel 摘要识别。

## 重现

```sh
uv run python -m pytest tests/contract tests/integration/runtime \
  tests/integration/cognition/test_persona.py \
  tests/integration/cognition/test_persona_http_concurrency.py \
  tests/integration/cognition/test_cognitive_deployment.py \
  tests/integration/cognition/test_reflection_pipeline.py \
  tests/integration/console/test_console_persona_http.py \
  tests/integration/console/test_console_persona_policy_http.py \
  tests/integration/console/test_console_plane.py sdk/python/tests --no-cov -q
npm test --prefix sdk/typescript
uv run python -m tools.check_public_api
uv run python -m tools.check_installation CORE_WHEEL SDK_WHEEL --offline --allow-local-sqlite
```

`check_installation` 现在持续验证三个独立依赖环境，并用 `python -I` 脱离源码路径消费。安装测试需要本机临时端口；Embedded smoke 本身不需要端口。`--offline` 需要已经准备好的依赖缓存。

## 尚未关闭的验收

真实 AstrBot 生命周期/Provider 适配、真实模型标注质量/Token 成本、代表规模峰值与宿主整体内存、生产 SQLite 目标矩阵、长稳/备份恢复/升级回退和正式注册表发布，均未被本轮合成测试代替。沿 W05/W06、W15～W20 各自发布门禁继续验收；插件 Pages、配置和自身人格编辑无须等待所有 Console 功能。
