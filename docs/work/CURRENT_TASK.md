# 当前任务

任务：**持久化事务基础与同事务审计：用户已验收，按授权完成独立本地提交后停止。**

## 基线、授权与范围

2026-09-10，目录及Git根为`/Users/cassia/Local/Code/iris_memory_core`，`main`，父提交`fdbf7a99a307b39b15764a6622b64af9a095b1e2`。本阶段独立提交由本记录所属提交定位。收尾前核对原34份改动及受测88文件指纹完全一致；configuration、deployment-candidates、logging、persistence-and-transactions四份架构正文保留已验收版本。

用户已授权唯一执行子代理更新运行时、采用最新兼容依赖并完成[整体契约](../architecture/persistence-and-transactions.md#persistence-foundation-contract)：[配置§11.12](../architecture/configuration.md#configuration-persistence-validation-contract)、事务§8、[审计§10.9](../architecture/logging.md#transactional-audit-contract)和整体验证。配置独立入口、有限仓储/UoW能力、原子回执与幂等恢复、同事务审计及全部范围内审查修复已完成。用户现已明确“我已审查，验收并提交”。本次只更新CURRENT_TASK／STATUS验收记录，并将原34份已验收改动加STATUS共35文件显式暂存、核验后创建一次独立本地提交；不推送或启动下一阶段。下一步方向仅由主会话规划，不交执行子代理操作。

## 环境与准入证据

- uv 0.12.9将CPython 3.12.14隔离安装在`/Users/cassia/.local/share/iris-memory-core/python/`，未安装公共bin，旧共享运行时保留。项目`.venv`使用新解释器，Python范围仍为3.12。来源：[Python发布页](https://www.python.org/downloads/release/python-31214/)、[Astral构建20260901](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901)。现有开发依赖采用[Pyright 1.1.413](https://github.com/microsoft/pyright/releases/tag/1.1.413)，锁定nodeenv 1.10.0、typing-extensions 4.16.0。
- 实际解释器`3.12.14 (main, Sep 1 2026, 14:09:38) [Clang 22.1.3]`，macOS 26.6.2／arm64；SQLite `3.53.1`、threadsafety=3。source_id为`2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9`。46项compile_options含THREADSAFE=1、MUTEX_PTHREADS；排序换行连接无尾换行SHA-256为`054a2945f636bf3b8f648d6abe8ed75a02c74ce6d54dbad67d7ad1777b58d606`。
- 自有临时准入探针退出0：WAL、FULL、foreign_keys=1、read_uncommitted=0、busy_timeout=50、wal_autocheckpoint=100回读及显式DDL提交／回滚、数据回滚、外键拒绝通过，连接关闭且临时目录清理。上述数值为探针输入。原执行者的3.12.13／SQLite 3.50.4结果不用于代表新环境。
- 核对[CPython 3.12.14 BytesIO源码](https://github.com/python/cpython/blob/v3.12.14/Modules/_io/bytesio.c)：精确bytes的初始化共享、无共享时getbuffer、等长且无export时getvalue共用原缓冲。新环境重新通过E=512、302／511／512／513地址边界、超限前无分配及主／回退单缓冲峰值512的既有测试。

## 有效验证与收尾核对

下列为实现轮最终有效结果，验收收尾确认源码／测试／工程文件与受测指纹一致后复用，未重复运行测试。原检查使用本项目解释器，运行前缀为`uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync`，均退出0：

- `pyright --version`为1.1.413；`pyright --project pyproject.toml --pythonpath .venv/bin/python`为0错误／警告／信息。
- `python -m unittest discover -s tests -t . -v`：412项通过，4.961秒；保留原320项行为覆盖，新增92项。
- `python -m compileall -q companion_memory tests`；另`uv --cache-dir /tmp/iris-memory-core-uv-cache lock --check --offline`通过。
- `git diff --check`及24份新增文件逐份`git diff --no-index --check`无空白错误；实现轮完整已跟踪／未跟踪差异已审查。验收收尾另核对35文件清单、记录差异、暂存内容及`git diff --cached --check`；不修改实现。未查看编辑器，不声称Pylance通过。

受测集合为`companion_memory/**/*.py`、`tests/**/*.py`、`.python-version`、`pyproject.toml`、`uv.lock`共88文件。按相对路径排序，将每项编码为`路径 + NUL + 文件SHA-256十六进制 + LF`后计算SHA-256，集合指纹为`9a919f1110699723dfc5dfa21d3fb62fa8d4b76f8da43463c48b47e67d1c1ff2`。此记录更新不改变受测集合。

测试仅使用自有临时目录、两组合成仓储、独立连接及受控子进程。临时文件位于本机APFS；父目录FD承担服务生命周期所有权锁，与SQLite事务锁分开。已覆盖COMMIT前／后屏障由父进程kill并等待结束、新解释器核验全无／全有、子进程写锁、旧快照与新鲜确认、幂等原结果、审计/诊断隔离、首错和迟到清理证据。建库身份由可信测试装配在任何数据库副作用前持有并保留。

生产路径／G2、生产建库身份存储、实际业务模块、Provider调用、Linux／Docker、性能目标和介质掉电保证仍范围外；受控故障注入、进程终止及本机普通重启不代表这些保障已验证。
