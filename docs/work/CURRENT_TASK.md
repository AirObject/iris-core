# 当前任务

**持久接入、三段批次、专注门控与恢复及最小只读观察：用户审核通过，已授权本地提交。** 用户于2026-09-11明确“审核通过提交commit”。本记录所属提交包含整体实现、已完成修复及验收记录；提交后停止，不推送、不进入下一阶段。

## 基线与实际范围

目录／Git根 `/Users/cassia/Local/Code/iris_memory_core`，`main`，父提交 `128f908e8d780da647949dfe3f6c21980b31fcf2`。提交前核对89份已审改动（22修改、67新增）及空暂存区；全部187份受测文件与最终验证指纹一致，复用有效检查，无源码／测试或环境变化。本次只更新CURRENT_TASK与STATUS的用户验收记录，共提交90份文件（23修改、67新增）：44份源码、33份测试／夹具、13份文档。保留所有已审内容，AGENTS、工程配置、锁文件及冻结资料不变；无子代理／其他会话、安装或部署。

## 修复与定向证据

- **有限保留与读取：** `Observations.started/trim_operations/local_evidence` 将已结束的全部确认尝试（含UNCONFIRMED）统一限容，不保留完成Task；仍在途任务不因淘汰而释放。设L为 `management.observation_row_limit`、A为 `runtime.max_active_entries`，登记最多L＋A条，任务引用最多A个；单次局部投影最多扫描／点读L＋A条，一页最坏L×(L＋A)，观察并发及等待另受现有配置约束。每个在途观察只持有限快照，不另建无界身份表。`CURRENT_PROCESS_RETAINED`只表示有限当前进程观察，缺失记录不表示已确认；原输入、协调端恢复责任、持久回执和真实存储所有者不由此窗口清理。
- **证据与所有权合并：** 完整库／模块／操作／scope／key、命令版本、指纹版本和值共同作为键。相同命令共享事实并合并所有在途任务，原完成回调继续生效；已知COMMITTED不被超时、拒绝、取消或迟到失败读取降级。同键异内容独立记录拒绝，不将合法原回执当损坏。超时更新原登记对象，不再按部分身份寻找并覆盖它。
- **清理独立：** 同一命令任一任务仍运行即保持占用；存储也可能在返回COMMITTED后继续关闭连接，此时结合公开存储健康保留清理观察。确认遇到已有writer而立即返回RECOVERY_UNAVAILABLE，不为每个查询虚构新的清理所有者。已有清理事实不被另一查询抹除；底层FAULTED时路径等所有权可保留到实际关闭。
- **新增7项回归：** 合法A=2、L=4配置中，真实临时SQLite writer屏障下连续三轮各12个不同原键确认，登记≤6、Task引用受限且可回收、每次观察点读≤6；解除屏障后继续12次确认，登记≤4，淘汰后仍能凭原输入查回原回执。COMMIT前／后重叠同身份确认、调用超时／原任务迟到、已提交连接关闭超过存储等待期限、回执SQL读取故障注入、反向完成顺序、读取期间提交、拒绝／取消和版本／内容冲突均有具体断言。保留入口／批次／实例权限投影，观察不输出指纹或恢复句柄、不执行模型或写恢复。

## 最近有效验证与文件对应

实际CPython **3.12.14**（`.venv/bin/python3`）、SQLite **3.53.1**／threadsafety=3、macOS **26.6.2 arm64**、uv **0.12.9**、Pyright **1.1.413**、nodeenv **1.10.0**、Node **26.8.1**；无新增依赖。以下 `U` 为 `uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync`，检查均退出0：

- `U python -m unittest tests.runtime.test_confirmation_retention -v`：新增7项通过；修复前真实屏障复现登记17条超界、同身份确认提前显示清理完成，以及成功返回后仍在关闭线程的漏报。
- `U python -m unittest discover -s tests -t . -v`：**569项，48.619秒，全部通过**，保留原562项。日志 `/tmp/iris-confirmation-final-unittest.log`；其中既有真实文件、跨进程恢复、回环HTTP及普通／优化解释器回归也已重新执行。
- `U pyright --version`；`U pyright --project pyproject.toml --pythonpath .venv/bin/python`：全量源码／测试／新增文件，0 errors／warnings／informations。
- `U python -m compileall -q companion_memory tests`；`uv --cache-dir /tmp/iris-memory-core-uv-cache lock --check --offline`（4 packages）；`node --check /tmp/iris-confirmation-status.js`（从当前http.py的SCRIPT逐字节提取）。
- 修复轮 `git diff --check`、全部67份新增文件UTF-8／AST／语义命名检查通过；提交收尾另核对90份暂存文件、`git diff --cached --check`及13份文档的路径／锚点，均通过。暂存和提交的受测文件与上述实际测试版本一致。

最终受测**187份文件**（182 Python＋5工程文件，含未跟踪文件及内嵌Web资源），清单 `/tmp/iris-runtime-tested-files.sha256`。原算法：Git已跟踪及未跟踪的companion_memory、tests、.gitignore、.python-version、.vscode、pyproject.toml、uv.lock去重排序；每项UTF-8路径＋NUL＋文件SHA256小写十六进制＋LF，对完整清单再取SHA256：**`954804a6bb62235e1dd6997121d7bd76979728f5ef8ff81018ecd6929cb0da43`**。验收记录收尾后核对暂存版本及提交版本与该受测指纹一致；仅文档变化，未机械重跑测试。

## 限制与停止点

新增证据为真实自有临时SQLite、线程屏障、SQL故障注入及受控完成顺序；模拟适配器与合成参与者仍只证明对应调用／事务语义。当前进程观察不替代持久原键确认或完整历史；不新增恢复写入、模型重试、配置编辑、FAULTED／UNKNOWN解除或生产能力。既有配置容量限制保持：64个平台仅为结构上限；原测试audit=2048、actor=bootstrap时12平台可持久、13平台审计超限回滚，另显式audit=8192的目录边界为短ID24／长ID17平台，均非通用容量保证。检查点仍随业务恢复代次线性保留，存储全检可能超时。**Pylance未验证**；未新增浏览器交互、Linux／Docker、性能或掉电证据。用户已审核通过；本地提交后停止，无推送或下一阶段授权。
