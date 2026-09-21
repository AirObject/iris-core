# 当前任务

**2026-09-21：核查并修复结构性依赖问题；定点代码、测试及文档纳入本次本地提交，runtime目录迁移待用户明确答复。** 本轮用户明确授权不受原监督／执行分工限制，同时修复代码后处理文档，并在修复后明确授权提交commit；该例外仅用于本任务，不改写[AGENTS](../../AGENTS.md)的一般协作分工。不使用子代理，不推送、部署或调用真实模型。

## 基线与核查结论

工作区为`/Users/cassia/Local/Code/iris_memory_core`，分支main，起点`9d483c7384836b94e5433a9bd1045a16b6a56bc8`，开始时工作树干净。外部通信源码已在`1f2cc1e`、相应文档已在`9d483c7`提交；旧工作记录中的“另一个worktree未提交”不是当前状态。原验收全过程及独有信息均可从该基线的CURRENT_TASK／STATUS追溯，不复制旧过程记录。

- information是已批准的跨模块协调实现，命令命名空间不等于业务表所有者；原有retrieval／state／goals／memory等归属保持。已在[所有权正文](../architecture/ownership.md#implementation-packages)补齐实现包映射，不新增M16。
- 共享记录原语及固定仓储声明移入persistence；原information路径保留同一符号的兼容导出，生产代码不再依赖兼容路径。
- 用户进一步选择由Provider定义输入端口；已移除本轮新增的contracts包，输入端口放在provider/media_input.py，media适配器交接所需字节和有限校验能力，保留业务对象、原租约及实际完成通知。goals改用只读通知路由端口，移除对IdentityAuthority的具体依赖。
- [架构测试](../../tests/architecture/test_import_boundaries.py)及[允许清单](../../tests/architecture/rules.py)约束导入、共享工具、媒体方向、权限方向及模型网络出口；持久层配置接入作为明确文件级例外，不宣称全部依赖图无环。
- cognition中的梦境认知候选和dream运行控制职责不同；configuration文件数本身不能证明违反所有权。未合并数据所有者或改动持久格式。

## 目录整理的待答复事项

自动审批拒绝了一次整库批量迁移，理由为大量移动／删除及导入重写缺乏分步验证。其余修复已按小范围独立修改推进。已提供[56个runtime文件的具体迁移清单](/private/tmp/iris-structure-repair/runtime-move-proposal.json)，建议进入`runtime/application/`的content、daily、managed、semantic、text、information、communication、dream八个子包；门控和持久调度记录等保留。已请求用户明确授权按该清单逐包迁移、同步导入／入口／文档并验证，答复前不执行该部分，不把目录问题写成已修复。

## 验证与交付

验证使用现有`iris-s2-z4e8arar:core` Linux arm64镜像、Python3.12.14／SQLite3.53.4及锁定Pyright1.1.413。源码只读挂载，以非root运行，外网关闭；本次输入端口复验无需秘密卷。

Provider输入端口的最终回归18项通过，覆盖架构方向、跨库拒绝、内部媒体工作、取消后实际占用、门控竞争、完整材料、拒绝保护、图片编码／拒绝及宿主学习闭环；增加原生租约直接输入拒绝、原字节身份及释放后输入失效断言。全量Pyright1.1.413覆盖999文件、0错误／警告，退出0；初轮两处类型收窄诊断已修复，原始输出保留。未跑全仓unittest、真实供应商或Pylance。

当前输入端口版本的日志、命令、退出码、文件指纹与限制见[交付清单](/private/tmp/iris-provider-input-fj5l3f6v/manifest.json)。此前结构修复的39项不同成功检查、2项TLS跳过、1000文件类型检查及导入对比保留在[前版证据](/private/tmp/iris-structure-repair/manifest.json)，不作为当前版本重新执行结果。Provider→media保持零导入，不宣称全库无环。提交前核对工程文件与验证版本一致，仅更新提交授权及工作记录；本地提交后停止，runtime目录迁移仍待答复。
