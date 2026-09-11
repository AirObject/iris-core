# 当前任务

**正式记忆、完整来源与媒体持久化闭环：本轮完成通知与宿主关闭两项定点修复完成，待监督技术复核。** 沿用用户对五组推荐、所属补充及整阶段实现、自查、测试和范围内修复的授权；尚无提交授权，不自行宣布验收通过。

## 基线与完成范围

仓库 `/Users/cassia/Local/Code/iris_memory_core`，`main`，HEAD `6a41859fdfc8c0f80305e792221e19fdab2a9fa2`。交接162份变更、空暂存区及375份文件指纹 `9b1ad68ee2bcbd94f3c4902f7dd75622ea894ef2079b27a51cc75c4a04571cd6` 核对一致，保留全部已有改动。当前164份变更：79份源码、68份测试／夹具、17份文档；本轮增量仅6份：存储服务、内容宿主、媒体服务、两份关闭回归测试及本文件。

依据[主契约](../architecture/formal-memory-source-media.md#ports-permissions)及[存储生命周期](../architecture/persistence-and-transactions.md#persistence-foundation-lifecycle)完成：

- 残留连接通知登记、原工作结束公布及清理消费使用同一锁边界；清理成功只发送本次完成通知，不撤销原回执。
- 宿主先停止新业务／媒体准入，保留合法提交和发布收尾；存储以单个保留工作者清理已结束所有者交出的连接，解除上层等待环。最终存储关闭加入已有清理工作者，根目录只在实际释放后交还。
- 真实SQLite屏障覆盖通知先登记／关闭先等待、COMMIT后首次关闭抛错、原工作者或清理工作者持续阻塞、重复关闭、首份INCOMPLETE不变及重开原键回执一致；相关阻断窗口零模型调用。

未改设计正文、AGENTS、STATUS、冻结资料、历史整理报告、工程配置或锁文件；未使用子代理、其他会话或调度。

## 最近有效验证

证据目录：`/private/tmp/iris-shutdown-validation-iybs3qfe`。`commands.json`保存实际命令、退出码和原始输出位置；`repair-assertions.json`定位本轮两项及前轮五组回归断言；`matrix-assertions.json`保留30组矩阵映射；`changed-files.json`分类全部差异及本轮增量。

定点unittest **11项通过**；最终全量unittest **680项通过，186.217秒，退出0**；全量Pyright **1.1.413，覆盖320份Python文件，零诊断，退出0**。编译、离线锁文件、JavaScript、全部差异／新增文件及文档链接／锚点检查见最终命令记录。首次全量运行因沙箱禁止回环端口产生7项权限错误，原始日志保留为 `interim-sandbox-*`；随后在允许本机回环端口的执行权限下完整重跑，未跳过测试。

实际环境：CPython 3.12.14、SQLite 3.53.1、uv 0.12.9、Node 26.8.1、macOS 26.6.2 arm64。**Pylance未验证**。最终377份受测清单、逐项SHA256和聚合指纹见 `tested-files.manifest`、`tested-files.json`、`tested-files.sha256`；算法为排序路径＋NUL＋文件SHA256＋LF。文档收尾后核对受测源码／测试／配置／资源未变，最终一致性证据见 `final-recheck.json`。

## 限制与停止点

存储／文件为ACTUAL，Provider适配器为SIMULATED，候选及G1参与者为显式SYNTHETIC。完整回归保留此前八项与五组修复、真实回环HTTP、跨进程恢复及旧装配兼容；不宣称真实供应商、生产鉴权或硬件断电已验证。持续阻塞以明确屏障保持，测试收尾后才允许其结束；不承诺能够终止真正永久阻塞的系统调用，仍保留实际占用。

无已知本轮范围内阻塞。监督重点为通知原子交接、清理工作者唯一性、上层合法收尾及根目录释放顺序。停在监督技术复核，由用户手动转交；不暂存、提交、推送、合并、部署或启动下一阶段，STATUS保持不变。
