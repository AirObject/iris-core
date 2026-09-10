# 当前任务

任务：**Provider基础服务与模拟适配器：用户已审核通过，按明确授权完成一次独立本地提交后停止。**

## 基线、授权与交付

2026-09-10，目录与Git根为`/Users/cassia/Local/Code/iris_memory_core`，`main`，父提交为`6a99353c6035629bcff866e8367d6fd69fa93113`。本阶段由本记录所属提交定位；提交前已核对44份已验收改动及119份受测文件指纹，保留全部授权内容。历史已验收里程碑及提交定位见[STATUS](STATUS.md)。

用户授权唯一新子代理推进整体阶段；主会话按用户委托完成草案审查、F1–F5及配置／有界文本衔接批准、定稿核对、实现派发及最终技术验收。**用户现已明确“审核通过，提交commit”，授权本阶段一次独立本地提交。** 主会话只做静态审查与独立文件指纹核对，未运行项目代码；实现、故障修复和下列实际运行由执行子代理完成。当前未发现阻塞或需用户裁决的契约冲突。

交付正文分别见[Provider契约](../architecture/provider.md#provider-foundation-contract)、[配置契约](../architecture/configuration.md#configuration-provider-validation-contract)及[持久化衔接](../architecture/persistence-and-transactions.md#provider-persistence-bridge)：四能力规范化及显式模拟适配器、真实本地SQLite账本／费用／共享预算／交接、登记确认后发送、同事务审计、有限尝试／总期限／取消／UNKNOWN、受限查询、独立完整配置校验及旧格式兼容。旧三个配置入口及审计文本限制保持。

## 最近有效验证

受测集合为companion_memory／tests全部`.py`及`.python-version`、`pyproject.toml`、`uv.lock`，共119份。按相对路径排序，逐项拼接路径、NUL、文件SHA256十六进制、LF，再计算总SHA256：`4c69b3f4e9fef825b02fad7fe575ad8934f67e85c8c90b2f2e415b0e8315a2f9`。主会话已独立核对该指纹与最终受测源码／测试一致；本次收尾代码未变，不重跑测试。

执行环境为CPython 3.12.14、实际SQLite 3.53.1、Pyright 1.1.413、uv 0.12.9；未安装或更新依赖。以下结果来自执行子代理最终修复轮实际命令；Python工具共用`uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync`前缀：

- `python -m unittest discover -s tests -t . -v`：479项通过，9.164秒（原412＋新增67）；`pyright --project pyproject.toml --pythonpath .venv/bin/python`：0错误／0警告／0信息；`pyright --version`：1.1.413；`python -m compileall -q companion_memory tests`退出0。
- `uv --cache-dir /tmp/iris-memory-core-uv-cache lock --check --offline`退出0；`git diff --check`及31份未跟踪文本逐份差异空白检查通过；受影响链接／锚点有效，三份架构原已批准正文与HEAD前缀逐字一致。
- 真实自有临时SQLite及受控屏障覆盖关键提交／审计原子性、35→50迟到差额与冲突、共享预算及超额风险、完整性／隔离拒绝、单快照汇总、实际Logger、完成时间两侧仲裁、有限恢复和清理所有权。父进程在五类窗口SIGKILL子进程并等待退出，再由全新解释器同库恢复；模拟器调用证据与真实提交证据分开。父提交旧源码建库后的当前实现打开／原键回放／继续事务实验通过，未迁移或补表。

最后所有权修复已由主会话核对：明确NotCommitted、已提交结果和仍在途清理分别保留；存储故障后不新发模型，清理结束前不释放槽位或owner；等待方保留原执行任务。实际覆盖不等于矩阵中每个变体全部通过，验收预期正文保持。

## 边界与停止点

仅模拟适配器执行模型侧行为，本地SQLite记账真实。真实配置快照／profile／价格版本仍缺失，不伪造；生产G2／路径、真实模型／供应商／HTTP／计费／凭据、Linux／Docker、Web、运行模式／梦境／业务模块、热激活及完整生产预算仍范围外。未做性能压测、介质掉电或编辑器Pylance验证；测试门控不代表生产鉴权，进程终止恢复不外推其他平台或掉电保证。

本次仅更新CURRENT_TASK／STATUS收尾表述，显式暂存44份授权文件，核对暂存清单、差异、受测指纹及`git diff --cached --check`后创建一次本地提交；不推送、合并或amend。提交后停止，下一阶段未授权。后续由用户手动转交并维护一个只读监督会话与一个执行会话，不再通过子代理推进；本次不创建新会话或任务。
