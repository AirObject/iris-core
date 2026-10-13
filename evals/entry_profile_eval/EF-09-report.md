# EF-09：入口画像记忆请求预算与正式公开评测

基线 origin/main=782644018bf71e94cc0d13d89a6e5e66811513f3；分支 feat/entry-profile-memory-b。接入、迁移032、范围隔离、清除及 UX 接口延续 [EF-07-report.md](EF-07-report.md) 和 [EF-07-interfaces.md](EF-07-interfaces.md)。本次不改提示词、冻结语料或评分，也不新增迁移。最终 head、验证及判分结果在完成后补入。

## 请求预算和设置

- 仅记忆路径的 MemoryProfileLimits.timeout_seconds 默认值／允许上限由120改为180。生成与检查分别从各自首次调用开始计时；传给 Gateway 的绝对 deadline 不因 HTTP 重试／退避重置。仍允许调用方显式注入1—180秒的更短限制。
- 旧文档 DEFAULTS 和 runtime_settings 中已有 generate_timeout_seconds／check_timeout_seconds 不改、不迁移；旧路径仍按原规则读取。记忆路径之前就不读取这两个旧字段，继续使用独立 MemoryProfileLimits。保存的75／90秒旧设置保持原值、新路径实际使用180秒已作测试。
- 在网关成功返回及本地 JSON 解析后复核截止时间；恰好到期或迟到均失败，不执行记忆操作、游标、回执及投影提交。迟到原始输出与超时调用诊断保留，仍受清除保护。普通 Gateway 在截止已耗尽的退避分支若只返回 retryable，新路径补记 timeout 原因。
- 本路径原本直接 chat＋严格 JSON 校验，没有额外模型 JSON 修正请求，本次保持不变。完整返回的格式／协议错误不延长截止、不新增调用、不当作作废理由。EP001 缺必填 violations 继续是 invalid_check_verdict 质量结果；不会为它单独重跑。通用整理路径“首次、重试、JSON修正共享180秒”的既有回归也通过，但不能将它说成本入口路径新增了修正调用。
- 调用诊断兼容新增 timeout_seconds；没有新路由、请求参数或错误码。UX 接口清单已同步。规划者将在合并时更新设计17.3和DECISIONS，本线不修改它们。

## 先红后绿

新文件 tests/test_entry_profile_memory_budget.py，14项确定性／假模型测试：两阶段各180秒；重试剩余预算及同一绝对截止；首次／重试／检查超时不写回；自定义网关恰好到期或迟到返回；JSON处理不能延长截止；完整坏格式不修复／不重跑；旧设置保留；显式较短预算和非法上限。

- 实现前：12 failed、2 passed（两项格式失败回归已通过），budget-red.log。
- 首次实现回归发现通用 Gateway 在耗尽退避前可能丢失 timeout 原因，3 failed、152 passed；已在本路径补充分类，保留 budget-green.log。
- 修正后：155 passed，budget-green-final.log；覆盖新预算、任务、真实适配、运行器、旧文档路径、通用网关和整理共享截止回归。3条已有 jieba SyntaxWarning。
- 两个Python版本全量、构建和本次正式运行待顺序完成后记录。完整材料位于仓库外 /Users/cassia/Local/Code/iris-eval-artifacts/EF-09-20261014/。

## 正式运行预登记

公开 entry_profile_v1 全6例，仅一次真实 GLM，生成／检查均180秒、一次只有一个请求流。有依据≥90%、禁止内容0；全部冻结 must_cover 保留，覆盖和阶段损失只作诊断。两名未参与修改的全新 gpt-6-astra max 子代理互不可见独立判分，分歧取不利。再遇超时或429，整轮作废并立即停止，不自行重跑。EP001这类完整格式失败保留为质量结果，继续其他案例。

前一轮 EF-07/08 已因 EP002 检查119.983秒超时作废，完整材料保持不动；EP001缺 violations 单列，不另作为作废原因。新运行使用独立目录，不续跑、不筛选旧输出。本报告不代表隐藏验收通过。
