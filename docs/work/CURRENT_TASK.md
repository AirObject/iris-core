# 当前任务

任务：**日志规范化槽位与双端有界队列：用户已验收；独立本地提交归档**。

## 基线与授权

2026-09-10，实际目录及Git根均为`/Users/cassia/Local/Code/iris_memory_core`，分支`main`，HEAD为`a5de74e5588d180c347a712283d662102dc0c850`，与预期一致。验收收尾起点为已审查的10份Python差异及本文件，共11份变更（含5份未跟踪Python）；暂存区为空，无额外差异。已读取AGENTS、INDEX、STATUS、完整[代码规范](../CODING_STANDARDS.md)、[日志模块](../modules/logging.md)、[日志现行契约](../architecture/logging.md#runtime-diagnostics-contract)，重点核对准入顺序、容量／预留、所有权、计数及截点，并读取现有日志组件及配置公开快照接口。

用户已确认本切片验收通过，授权只更新本文件和STATUS，并将已审查的10份Python及两份验收记录显式暂存，创建独立本地提交，共12份文件；标题为`feat(logging): 增加有界队列与投递记账`。本次不修改源码、测试、契约、配置或依赖；上一日志事件切片的验收提交明确为上述HEAD。

## 保留的内部行为

- 规范化总占用≤P、低等级≤P−1；两端FIFO的排队和单个在途共同计Q，按批准的Q−R／Q阈值准入，保持禁用→模块过滤→端过滤→故障→容量。E／Q／R／P仍从已校验公开快照取得，无默认副本；不在锁内执行回调或输出。
- UNKNOWN持续占用字节及容量直到实际结束，迟到完成不重放、不重计或恢复端。双端决定与停止接收原子互斥；保留单个当前截点累计器、五类互斥分区及深不可变结果／观察。固定标签计数饱和于2^63−1；高等级失败只保留安全应急需求。内部消费者实际完成后释放借用句柄，无事件历史表。

## 两项定点修复

- **编码实际暂存**：核实CPython 3.12.13的`bytes(iterable)`使用`_PyBytesWriter`，小记录会从栈缓冲复制到最终bytes；L=512时两份内容峰值1024，原先仅凭长度提示作出的单缓冲结论不成立（[实现依据](https://github.com/python/cpython/blob/v3.12.13/Objects/bytesobject.c)）。现先测长L，再用`BytesIO(bytes(L))`共享临时精确bytes，通过唯一memoryview按整数逐字节填充，释放导出后`getvalue()`返回同一底层分配，关闭BytesIO释放其引用；没有增长、分块bytes或迭代器bytes构造。已核对[同版本BytesIO初始化、导出与getvalue源码](https://github.com/python/cpython/blob/v3.12.13/Modules/_io/bytesio.c)，并用只保存地址的测试验证初始化／可写视图／返回bytes的地址相同。
- **峰值依据**：未填充零字节也计入L，单槽始终仅一份L≤E内容；失败关闭主缓冲后才进入备用。每端≤QE、规范化≤PE，合计≤2QE+PE，无应急缓冲；对象头、终止NUL、memoryview和标量状态属于另有有限上界的容器／辅助开销。依据限本次CPython 3.12.13构造路径，换解释器须重新核验，不能仅比较最终长度。新增测试覆盖E=512下302／511／512字节合法事件、513字节超限，以及部分填充失败后的备用；主／备用实际保留内容峰值为512字节。
- **入队与计数事务**：先复制两端固定大小计数库，并在未发布副本上完成accepted／filtered／dropped及饱和更新；计数所需分配成功后才追加队列并发布两端计数引用。追加及发布处于同一锁和回滚边界，异常恢复原引用并移除本次节点，槽位仍由finally释放。定点注入accepted第一／第二处更新前及更新后MemoryError，确认双端已有排队／在途目标、健康计数、旧／新截点分区和槽位均不变；移除注入后验证正常入队、停止截点及完成通知。另覆盖暂存过滤／饱和更新的丢弃，未制造真实内存耗尽。

## 当前版本的实际验证

对象为上述HEAD加本记录所属提交的源码／测试。验收收尾重新计算45份Python及3份检查配置／锁文件的完整指纹，确认与246项测试对应的已测内容一致，故复用有效检查，不重复执行。修复轮使用Python 3.12.13、uv 0.12.9、Pyright 1.1.411，未安装依赖；当时下列命令实际执行且退出码均为0（`uv`命令统一带`--cache-dir /tmp/iris-memory-core-uv-cache`）：

```text
uv run --offline --no-sync pyright --version
uv run --offline --no-sync pyright --project pyproject.toml --pythonpath .venv/bin/python
uv run --offline --no-sync python -m unittest discover -s tests -t . -v
uv run --offline --no-sync python -m compileall -q companion_memory tests
uv lock --check --offline
git diff --check
git diff --cached --check
```

全量Pyright覆盖源码及测试45份Python（含5份新增）：**0 errors／0 warnings／0 informations**；unittest **246项通过＝修复起点240项＋新增6项**；原240项测试文件内容均未改，并以AST核对全部名称保留。覆盖容量／预留、在途占用、过滤首序、双端隔离、失败释放及入队回滚、UNKNOWN迟到成功／失败、停止接收两侧强制竞争、12组并发截点竞争、不可变观察、计数饱和及JSON转义／UTF-8边界等；线程使用Event／Barrier建立顺序，等待有界、finally释放并join，无sleep正确性假设。没有真实I/O验证，未查看编辑器Pylance诊断。

编译、离线锁文件、全部Python模块说明／AST及授权范围检查通过；5份新增文件逐一执行`git diff --no-index --check /dev/null <文件>`，无空白诊断（新增差异允许退出码1）。人工核对所有权、内存上界、固定标签、语义命名和能力边界。45份Python及3份共享配置／锁文件的排序路径→SHA-256映射，以紧凑排序JSON编码后的摘要为`f1c7649511446b1ec45ceaa516d3a61fbf52d8a7746e76d6ba313c08fd033dc8`；本次验收收尾核对全部48份文件及其暂存版本一致，显式暂存清单仅含授权12份文件，暂存差异检查通过。

## 停止点

**用户已验收，本记录随授权的独立本地提交归档；提交后停止。** 无公开Service／完整EmitReceipt、flush／close成功占位、真实输出、资源准备、轮转、恢复探测、应急投递或生产装配；内部停止接收后仍可处理既有队列，不代表资源已关闭。G2仍待定，未选择生产目录。模拟完成仅验证内存记账，不证明真实I/O、超时执行、持久化或一秒查询开销。不推送、合并、部署或开始下一切片。
