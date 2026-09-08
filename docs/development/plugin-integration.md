# iris_memory v4 接入指南与缺口回填

2026-09-08 新实施项：[Observation 全量上下文与批量总结](observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

本轮基于 2026-09-08 评审清单实现 Core 侧接缝。清单中的工作指令按评审输入处理；没有修改插件仓库，也没有关闭其他任务的 W 工作包。当前源码开发候选为 Core 0.16.0、Python/TypeScript SDK 0.12.0、业务契约 1.13.0、Schema 25；**尚未发布到注册表**。使用本轮构建的 wheel 与摘要识别产物，正式发布仍属发布工作。

## 两种入口

- 本地：`iris_memory_core.embedded.EmbeddedMemory`，无服务端口，直接复用应用服务。
- 远程：独立 `iris_memory_sdk.AsyncIrisMemoryClient`，连接独立 Core 服务；不把 SDK 源码或 Core 源码复制进插件。
- 业务 JSON 请求/结果由业务 OpenAPI 冻结。`execute()` 的 operationId 使用 [方法清单](public-api.md) 对应操作；只有具体实现可调用，HTTP 健康、SSE、Console 和未授权管理操作不因存在名称而可用。本地健康与生命周期使用 `diagnostics()`。

本地安装 Core wheel 基础依赖即可；启用向量安装该 wheel 的 `[vector]`，服务部署安装 `[server]`，Console 部署安装 `[console]`，完整服务通常安装 `[server,vector,console]`。SDK wheel 仅依赖 httpx 及其传递依赖。Python 要求 ≥3.12；本地实例当前使用 Unix flock，macOS 已实测，Linux 待宿主验证，Windows 尚未支持；SQLite 使用现有正式 allowlist。`allow_local_sqlite=True` 仅供开发验证，诊断报告实际 SQLite 版本，不能当作宿主生产兼容承诺。

## 宿主生命周期

```python
from pathlib import Path
from iris_memory_core.embedded import EmbeddedConfig, EmbeddedMemory, LocalBootstrap

# import / 构造不会迁移、联网或创建后台工作。
memory = EmbeddedMemory(EmbeddedConfig(
    Path(plugin_data_dir) / "core",
    bootstrap=LocalBootstrap(
        app_instance_id="iris-plugin",
        manage_identities=True,  # 宿主确认可以注册的用户身份
        manage_persona=True,    # 仅授予人格内容镜像与状态操作
        manage_agents=True,    # 多人格需要时开启
    ),
    background=False,
))
scope = await memory.start()
actor = await memory.register_actor(
    "astrbot-platform-name", stable_external_user_id,
    realm="default", display_name="User", idempotency_key=stable_actor_key,
)
# 多人格：保存插件 persona_id -> Core Agent/Space 映射。
other_scope = await memory.provision_agent("Persona B", key="plugin-persona-b")
# 常用方法：observe_batch / remember_claim / recall / correct_claim / forget_memory。
# 更多业务使用 execute("createTask", body, idempotency_key=...) 等公开入口。
await memory.aclose()
```

`start()` 返回初始 tenant_id、agent_id、space_id；重复启动返回相同注册。停止后同实例可重启。更换初始 tenant/application/name/surface_mode 会与已保存目录身份冲突，应使用对应原配置；不得随意丢弃注册文件来重置映射。`provision_agent` 最多保存 128 个注册，键与显示名称不匹配会拒绝；重启后全部已注册 Agent/Space 恢复。多人格共用一个实例，不重复加载大索引。远程多人格仍消费运维预配置 Agent；SDK 没有通用 Agent 创建方法。

每次连接后可再次调用 `register_actor`，已有有效绑定直接返回，并恢复本实例的实体读取授权；失效绑定不会自动恢复。

单实例业务执行器并发为 1，待处理上限默认 32。后台默认关闭，可显式 `run_pending()` 处理一个有界批次，或 `background=True` 每 0.25 秒处理一批。未启用模型时不会调用确定性桩；索引尚未建成会在 Recall 中降级，明确记忆的 canonical route 仍可使用。声明管理索引需要 `manage_indexes=True`，然后通过 `execute("rebuildIndex", {"reason": ...}, path_parameters={"kind": "vector"}, idempotency_key=...)` 排队，由相同 Worker 处理。

`aclose()` 停止接收、取消本实例 Provider 调用并等待在途工作。默认关闭 deadline 为 5 秒；超时抛 `EmbeddedError(code="deadline_exceeded")`，保持 `closing` 和目录锁，待工作结束后再次关闭。不要因此启动第二个实例。SQLite 写操作进入执行器后，外层 `CancelledError` 不能证明没有提交；保留原业务幂等键对账。`diagnostics()` 提供状态、在途数量、请求 ID、最近 64 次完成/失败结果、后台错误码及 Provider 在途数；不输出内容、密钥或私有句柄。诊断不调用模型。

目录锁只协调 Embedded 的合作实例。不得同时让独立 serve/worker 使用同一目录，不得在宿主关闭其事件循环后才关闭 Core。同步注入 Provider 必须遵守其 deadline；不能强杀 Python 线程，失控 Provider 会造成可诊断的关闭超时。

## Provider 接入与成本

`iris_memory_core.embedded_providers` 公开原有 `EmbeddingProvider`、`ExtractionProvider`、`SummarizationProvider`、`VectorSpaceConfig`，没有 AstrBot 类型。对异步宿主使用适配器：

```python
from iris_memory_core.embedded_providers import (
    AsyncEmbeddingAdapter, AsyncCognitiveAdapter, CognitiveProviderLimits, VectorSpaceConfig,
)

async def embed(texts):
    # 必须返回数量一致、维度正确、L2 归一化的有限向量。
    return await host_embedding_client.embed(texts)

embedding = AsyncEmbeddingAdapter(
    VectorSpaceConfig(model="provider/model@revision", dimension=1024), embed,
    timeout_seconds=5,
)
# extract / summarize 接收 observations 及 prompt_version/schema_version/timeout_seconds。
# 产物沿用现有认知 Provider 的候选 Schema；只能产生候选，由现有提交流程校验。
cognitive = AsyncCognitiveAdapter(
    model_id="provider/model@revision", extract=extract_candidates, summarize=summarize_episode,
    limits=CognitiveProviderLimits(max_retries=0, concurrency=1, daily_budget_microunits=100_000),
)
memory = EmbeddedMemory(config, embedding=embedding, cognitive=cognitive)
```

异步回调在宿主事件循环运行；应用服务在自己的执行器等待，不阻塞 AstrBot 循环。适配器拒绝无限并行，按调用 deadline 取消，隐藏模型异常中的敏感文本。关闭不会调用宿主 Provider 的 close。Embedding 空间包含模型/版本、维度、metric、normalization、template、builder；变更需要新代索引，旧空间不混用。真实服务端配置继续使用 W05/W06 的部署文件、秘密引用和 Provider 治理；远程不回调 AstrBot、不复制宿主密钥。

认知适配器默认不重试、并发 1；可显式设置超时、QPS、重试、熔断和日预算，复用持久治理。没有认知 Provider 时不声明已配置的摘要/提炼能力。页面查询和健康检查不触发模型。模型超时/取消与候选提交各自处理，来源失效、删除、权限和修订校验不被适配器绕过。

## 人格镜像与权限

插件保留人格主记录、编辑、草稿、开关和绑定。Core 保存已发布镜像。插件分别保存插件 revision、Core revision 和 Core content_hash；以 expected_revision 发布，成功后回读核对，不能把两个 revision 当成同一个计数器。

合法映射可把插件的完整文本与来源标识放在 `core.identity` 对象，例如：

```json
{
  "expected_revision": 1,
  "core": {"identity": {"plugin_persona_id": "persona-a", "plugin_revision": 7,
                        "text": "You are Iris."}},
  "traits": {}, "narrative": {}, "reason": "plugin_publish"
}
```

复用 `publishPersonaRevision` / SDK `publish_persona_revision`，以及 current/history/rollback。顶层字段必须是 Core 已定义字段，不能增加 `core.plugin`。每层 canonical JSON ≤32768 UTF-8 字节；每个字符串 ≤4096 字符，列表≤128项，嵌套≤8层。超过单字符串长度时可把完整文本按顺序切成 `core.identity.text_chunks` 字符串列表，回读拼接必须与原文完全相等；超过总层大小明确拒绝，不能悄悄截断。Core content_hash 覆盖三层规范化内容；插件可另存原文 hash，不替代 Core hash。

远程由可信运维初始化时显式添加 `init --persona-mirror`。产生的是具有 `persona.mirror.v1` 的 scoped application Bearer，允许该 Agent 内内容发布/回滚，不允许修改 Policy 或 Console；普通 application Token 仍拒绝发布。已有安装由受权凭据管理配置相同 capability。不要把 Console 登录密钥当作 `/v1` Bearer。

镜像 CAS 冲突时回读并报告差异，不覆盖外部编辑。回滚是新增 Core revision。保留默认演进锁与当前 Policy，不为了镜像关闭 Ready/Policy。镜像不可用时插件仍可编辑人格；依赖精确 Core 镜像版本的 Recall 注入应暂停，并按公开重验能力验证旧 Recall。

## SDK 热停与错误

```python
async with AsyncIrisMemoryClient(
    core_url, bearer_token=token, timeout_seconds=5,
    max_in_flight=4, max_pending=32,
) as client:
    await client.negotiate(required_capabilities=("recall.v1", "persona.mirror.v1"))
    profile = await client.get_entity_profile(entity_id, agent_id=agent_id, space_id=space_id)
```

连接池复用连接，默认 8 个在途请求、64 个已接纳请求；超出后立即拒绝。deadline 包含排队和整个调用；socket 超时为 `socket_timeout`，整次超时为 `deadline_exceeded`。外部协程取消仍为 `CancelledError`，底层异步 HTTP 工作随之取消。`aclose(timeout_seconds=5)` 停止接纳、取消并等待本实例请求；传入 `http_client=` 时该客户端归调用方所有，不被关闭。SDK 不自动重试。

业务错误保留 `IrisMemoryApiError.envelope` 和 request_id。传输错误为公开 `iris_memory_sdk.client.IrisMemoryTransportError`，带 code、request_id、retryable、result_unknown。写入超时/断连可能已提交，`result_unknown=True` 不等于可直接重试；只有具备幂等保证的操作才用原键重试/对账。被外层取消的写入同样按结果未知处理。授权错误、校验错误与业务冲突继续使用原有业务错误码。

## 评审清单回填

| 项目 | 处置 / 本轮交付 | 验收与剩余边界 |
| --- | --- | --- |
| G-01 | 接受：公共 Embedded 模块，共享 BusinessRuntime / DTO | 无端口记忆闭环、权限/CAS/遗忘测试；ADR-0053 |
| G-02 | 接受：显式启停、单执行器、有界接纳/关闭、OS目录锁、持久注册 | 50 次启停、失败清理、跨进程互斥、在途工作保留；不承诺强杀失控线程 |
| G-03 | 接受：复用 ports，公开异步桥接与借用资源语义 | Fake Embedding 实际建索引/查询、宿主循环与取消；真实 AstrBot 适配由插件验收 |
| G-04 | 采用现有 W05：配置/秘密/建索引/激活/空间切换实现 | 真实外部模型与生产运行门禁仍依原 W05 报告，不以 Fake 关闭 |
| G-05 | 采用 W06，并补宿主认知注入与保守成本配置 | 固定标注样本质量、真实 Token/耗时由原 W06/实际模型环境验收 |
| G-06 | 接受：基础/server/vector/console 依赖拆分、后台默认关闭、调用上限 | 安装后无重型模块加载与生命周期趋势；大规模、重建峰值和宿主总量尚未冻结 |
| G-07 | 接受：独立 SDK 原生异步传输、关闭、错误、Entity Profile、required_capabilities | 真实 TCP 慢响应、断连、取消、借用连接及并发测试；SSE 未新增 |
| G-08 | 接受：有限 persona.mirror 授权、完整文本合法映射、CAS/回滚说明 | 本地多人格注册；远程预配 Agent，真实插件版本映射仍由插件实现 |
| G-09 | 接受：双入口真实 TCP 等价测试、wheel 消费脚本 | 本轮安装物以摘要冻结；正式宿主 Python/SQLite 组合仍需逐项认证 |
| G-10 | 按需：本地诊断/受权重建、既有 SDK 管理方法 | 没有自动开放 Console，没有宣称迁移/导入页面已实现 |
| G-11 | 暂缓本轮关闭：沿 W16～W20 发布/长稳/恢复工作 | 不把本次接口补齐认作生产就绪 |

本轮实现责任：Core/SDK 接缝；插件负责人继续负责 AstrBot Provider 适配、配置/Pages、人格主记录、平台身份与实际发送。目标为当前开发候选产物，正式发行版本由发布工作冻结。

验收入口：`tests/integration/runtime/test_embedded.py`、`test_embedded_http_parity.py`、`test_sdk_async_lifecycle.py`；安装消费命令 `python -I tools/smoke_installed_embedded.py --allow-local-sqlite`（在安装 wheel 的隔离 Python 下运行）。结果与产物摘要见 [接入验收报告](../reports/plugin-integration.md)。

## 接入群聊背景与实时上下文

无需增加环境缓存数据库。将实际获授权收到的背景消息传给 `observe_batch`，设置 `context_kind="background"`，可同时提供 `source_thread_id` 和 `reply_to_source_event_id`。消息入库不调用认知模型。直接交互沿用缺省 `interaction`，Recent 优先保留这些直接交互。

在外部 Agent 需要本轮上下文时调用 `observation_context`，读取原文、处理状态及已发布摘要，根据分页标志继续读取。要在宿主内自动批量总结，除配置 `AsyncCognitiveAdapter` 外还需要 `EmbeddedConfig(auto_summary_enabled=True, background=True)`，或显式调用 `run_pending()`。自动总结开关默认关闭；只配模型不会产生后台总结费用。也可用 `summarize_observations` 显式排队一次。

背景原文默认保存 30 天，摘要明确引用的消息继续保留，未引用闲聊仍按期清理；`background_retention_days=0` 关闭这一项自动清理。普通保留与主动遗忘遵循不同规则。完整调用示例、响应字段、触发阈值和成本说明见 [Observation 上下文](observation-context.md)。
