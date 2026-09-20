# 当前任务

**Core外部通信完整补全于2026-09-18通过最终监督工程技术验收；四项阻塞均关闭，当前停于交付收尾。** 批准范围见[通信方案](../architecture/external-communication.md)，分工见[AGENTS](../../AGENTS.md)，已验收能力与限制见[STATUS](STATUS.md)。未授权提交、推送、合并、生产部署或下一项实现。

## 工程位置与验收版本

监督主仓库为`/Users/cassia/Local/Code/iris_memory_core`（main）；工程仍在`/Users/cassia/.codex/worktrees/5b4a/iris_memory_core`（detached），共同HEAD为`71e9a122f67e06c4f8ce478862d5315f840ebd34`。最终清单含110份未提交工程变更，其中本轮修复21份。最新AGENTS及docs从监督主仓库读取；worktree文档保留原交接快照。工程未复制回主仓库，不另建空基线，保全原修改及证据。

[最终执行清单](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-communication-repair-ngfuvuwq/manifest.json)SHA256为`ac4181e0a0d7a4dca506e4f2e416f20919197b380466fa76b5c42936f7b5590a`；1092份文件、154份证据、46份命令日志及源码归档摘要全部核对一致。[监督验收清单](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-communication-repair-review-_7qauid1/review.json)保留1033份工程文件映射（排除docs／AGENTS），集合指纹为`b2ecf46968922eb6d70318b59224409b7a5c324b3c92ef59082b5d88d9571565`。

## 四项修复与复验

| 已关闭问题 | 静态与执行证据 |
| --- | --- |
| 升级遗漏WAL | 独占资源下恢复／checkpoint、确认日志关闭后核对指纹；旧实例原生提交、主文件未变、强制退出后，陈旧候选在fencing前拒绝，原提交可确认 |
| WS控制帧无界写入 | 所有实际写出共用缓冲预算，控制帧等待drain，超限abort并清理；真实不读socket的Ping洪泛峰值32698字节，关闭后缓冲归零 |
| 宿主持久终态不可查询 | 原goals所有者按授权宿主／入口／路由投影，每页4计划、每计划至多2尝试；ACK回执丢失、三终态、分页、越权及独立进程重启核对通过，无新发送；协议与客户端同步 |
| Web激活刷新丢失 | 两配置页面共用实例内原请求／激活ID续办；丢请求、超时、丢响应、消费者失败及双向切页均保持同一ID、单次保存 |

[前次审查](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-communication-review-87y5g6c9/review.json)与[前轮交付](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-communication-complete-i_00r3lt/manifest.json)原样保留；原390份证据和109份命令日志再次核对一致。结合前轮完整审查与本轮21份修复差异，未发现新的阻塞问题。

## 最近验证与能力限制

执行者在同一Docker Linux arm64环境完成75项托管、13项关联及3项最终浏览器检查；锁定Pyright1.1.413全量0错误／警告，前端类型、浏览器类型和构建退出0。最终Pyright命令为`uv run --offline --no-sync python -m pyright --project pyproject.toml --pythonpath .venv/bin/python`。托管回归与负载期间仅浏览器夹具／测试两文件变化，运行源码及受测模块不变；最终浏览器已覆盖这两文件，46项命令的前后版本映射均核对。历史失败保留，不宣称全仓unittest或Pylance通过。

30分钟混合负载使用4 CPU／4GiB、正常后台调度：3582次查询中1782次一秒内成功（49.75%）；60次上传最终确认；120次发送及唯一接收，90个持久ACK、30个UNKNOWN，无重发。客户端89个COMMITTED及1个UNCONFIRMED回执按delivery_id与90个持久ACK逐项对应，不将未确认回执写成客户端确认。真实供应商请求0。负载与回归／浏览器／类型构建有重叠，不与前轮48.16%作隔离性能比较；准入和后台争用、原错误分布继续保留，不宣称生产性能或长期稳定性通过。

升级仅覆盖精确当前托管前身；fencing前可中止准备，之后向前恢复，旧备份不是无损回退。浏览器续办限同一实例的保留会话存储，不外推跨设备或清除存储。清理证据记录本轮自有容器、秘密卷及镜像已移除。

## 停止点

本轮监督只核对源码、Git、原始证据和文档，未运行项目、测试、构建、容器或模型。仅维护CURRENT_TASK／STATUS；工程、原证据及其他既有文档差异保留。主仓库及worktree的git diff --check、17份差异文档的887处本地链接／锚点通过，工程1092份文件及154份证据再次核对一致。停止，不自动开展下一项工作或调度其他会话。
