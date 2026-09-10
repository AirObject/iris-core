# 当前任务

任务：**日志双端异步写出与 I/O 超时控制：用户已验收；独立本地提交归档**。

## 基线与授权

2026-09-10，用户确认验收本切片及完成发布／超时仲裁修复，授权仅更新本文件和 STATUS，并创建独立本地提交。实际目录及 Git 根均为 `/Users/cassia/Local/Code/iris_memory_core`，分支 `main`，提交前 HEAD 为 `f090d534d1a299aaf5346bcc58c92fe956071344`；起点暂存区为空，5 份已跟踪修改及 4 份未跟踪文件均为既有实现，没有额外差异。已读取 AGENTS、INDEX、CURRENT_TASK 和 STATUS；本次不修改源码或测试。

提交严格包含 logging_service 下的 `__init__.py`、`_async_writer.py`、`_queues.py`、`_settings.py`，tests/logging_service 下的 `async_support.py`、`test_async_writes.py`、`test_io_deadlines.py`、`test_settings_and_routing.py`，以及本文件和 STATUS，共 10 份文件。提交标题为 `feat(logging): 增加双端异步写出与 I/O 超时控制`。

## 已验收行为与限制

- console／file 独立后台写出，每端一个工作线程和未完成 I/O，共用一个监测线程；逐端 FIFO，写出不持有准入锁。io_timeout_ms 只从统一配置已校验快照的公开接口取得。
- 每端固定完成记录和短互斥锁统一结束时刻发布／超时仲裁，避免期限内完成因记账延迟被误判。确定性回归先复现 19ms 成功、20ms 监测导致 UNKNOWN 和丢弃积压；修复后保持 READY，第二条继续写出。期限前短写／异常为 WRITE_FAILED，实际超时为 IO_TIMEOUT，保留首个故障。
- UNKNOWN 保留缓冲及容量直至实际结束；迟到完成不重计、不重放、不恢复。沿用共享 bytes 和编码预算，无逐事件线程、Future 或历史任务；内部退休／join 管理工作者，未结束调用仍明确占用，借用端口不关闭。
- 验证仅覆盖内存替身下的并发、故障、回收和引用释放，不证明真实 I/O、性能或持久化。没有完整公开 Service、initialize／flush／close、真实资源准备、轮转、应急投递、恢复或生产装配，G2 仍待定；内部工作者退休不等于完整 close。

## 验证依据与本次复用

来源为 2026-09-10 完成发布竞争修复后的实际检查，版本为上述父提交加本次提交的源码／测试。环境：Python 3.12.13、uv 0.12.9、Pyright 1.1.411。**270 项 unittest 通过（原 267 项正文保留＋新增 3 项）；全量 Pyright 0 errors／0 warnings／0 informations；编译及离线锁文件检查通过。** 当时实际执行且最终退出码均为 0：

```text
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync pyright --version
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync pyright --project pyproject.toml --pythonpath .venv/bin/python
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync python -m unittest discover -s tests -t . -v
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync python -m compileall -q companion_memory tests
uv --cache-dir /tmp/iris-memory-core-uv-cache lock --check --offline
git diff --check
```

本次重新计算 49 份 Python 及 `pyproject.toml`、`uv.lock`、`.vscode/settings.json` 共 52 份文件的排序路径→SHA-256 映射，以紧凑排序 JSON 编码后的摘要为 `b4c458565c6f283b8fd2a524892cfad81dbef27e3bc6ab5494d4b276bb776c7f`，与已测版本完全一致，因此按用户授权复用以上有效结果，不重复运行。未查看编辑器 Pylance 诊断。

收尾按明确清单暂存，核对暂存恰好 10 份文件、与工作区及 52 份已测文件一致；执行 `git diff --check` 和 `git diff --cached --check`，不夹带其他修改。

## 停止点

**用户已验收，本记录随授权的独立本地提交归档；提交后停止。** 不安装依赖、不推送、不合并、不部署或开始下一切片。
