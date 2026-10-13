# CO-05 主体引用误拒修复与 GLM 正式评测（2026-10-13）

实现、两版全量测试及正式运行代码 head：`0a07cf63946e8848c76e30b7287f88853fedcda5`。本报告随后单独提交；没有在看见正式结果后修改产品代码、提示词、语料或计分规则。本轮继续 PR #107 / `fix/consolidation-details`，只报告数据，由规划者作取舍。

## 规则与边界

CO-05 只删除报告文本中两种过宽的主体授权规则：任意拉丁词的白名单检查，以及 jieba 词性猜测人名。普通协议词、英文词和中文词不再被当作主体 ID。主体引用只检查 `主体[ID|名字]` 或 `主体[ID]`，对照本次材料中的 subjects 精确匹配 ID、名字或别名；材料之外的主体、大小写/拼写错误仍整项作为 invalid_output 拒绝，不自动纠正。输出 JSON 已有 `updates[].subject_ids` 的精确成员校验保持原样；report_only 模式原有的字段限制也保留。

没有增加普通词例外表，也没有添加正文人名猜测。局限是未按结构格式书写的人名/ID 不在这层授权检查范围内；正确归属、事实对象和语义仍需要模型遵守冻结提示词，并由外部判分复核。限定词、数字、记忆/来源引用、逐字引文、合并正文和写回修订校验均未改。180 秒共享请求预算、50 次上限、事务外模型调用保持原样。AST 对照确认 `Consolidation` 类、限定词/数字/合并正文函数无变化。

提示词沿用冻结提交 `d80db6f39669bd6055fe5bd8cc4a26df2cffc2ab`，不重新冻结：

| 文件 | SHA-256 |
|---|---|
| `consolidation_pair_v3.md` | `4ab2c25a0dd803905889df2f35328683ebc0e68929db75bc3135a342e56cab35` |
| `consolidation_conflict_report_only_v2.md` | `6247ea813cfe6bd0921dc91f13797159749e32982a3a9db9dcbc46e87be04673` |
| `consolidation_dependency_report_only_v2.md` | `fcb69095a596009a151be401e75d0e734918eafb30d256bae4001479a3ca6d6d` |

## 测试与离线回放

- 先在原产品代码 `75e53a2` 上写新测试：26 failed、67 passed；失败项对应普通协议词、英文词、陈述/向青芽在 reason 或 annotation 中被误拒。
- 最小修复后，同一测试文件 93 passed（SHA-256：`29f4109a666e9c6c06c913e7f9600fc28b8638863e7de559e01223aab050646c`）；包括 CS032 的 yingo/yingluo、仅 ID、名字/别名、全库存在但当前材料无授权的主体、JSON subject_ids、恢复后的写回复核，以及既有数字/引文/限定词用例。
- Python 3.13.13：`uv run pytest`，3095 passed、4 warnings，pytest 809.41 秒。
- Python 3.12.12：`uv run --locked --isolated --python 3.12 pytest`，3095 passed、4 warnings，pytest 840.71 秒。两版串行；每版结束已删除自己 worktree 的 `.pytest-tmp`。
- `uv build` 成功生成 wheel 和 sdist；worktree `.venv` 仍为 Python 3.13.13。4 个警告为第三方 jieba 的转义警告及 Starlette/httpx 弃用提示。
- 对上一轮 rejection-diagnostics 的 19 个原始输出仅离线重放报告校验：18 个普通词误拒解除，CS032 的 `主体[yingo|映罗]` 仍以 unknown_report_subject 拒绝。这不是模型预览或正式重跑，也不替代质量判分。预览次数 0。

## 正式运行

看结果前登记：误合并（含丢细节）≤5%；没有基础设施作废；应合并识别 ≥7/12；矛盾与依赖一致合计 ≥7/18；写回损坏 ≤2/40。保留全部案例，校验拒绝不从质量分母删除。按 `merge_scoring_v2`，公开集 mutual_details 无标签，建议命中不适用，建议误报单列。

2026-10-13 04:00:54—04:09:08（UTC+8）只运行一次 consolidation_v1，broad / report_only_v1，GLM glm-5.3-flash、high。连通检查 chat/embedding 成功后，正式运行串行完成 40/40；58 次网络调用全部 success，超时 0、429 0，作废运行 0、重跑 0。58 条 model_calls 与整理调用账本逐项对应，每例最多 4 次调用，全部维护任务 completed，运行前后源码指纹一致。正式运行墙钟 494.445 秒，8 次 JSON 修正包含在 58 次中。

外部观察工具用系统 Python 读取新 SQLite 库时曾报 unable to open database file；随后使用未改变的 worktree Python 3.13.13/SQLite 3.50.4 核对连通记录并附加只读监测，未重启真实运行。结束后同一环境完整核验 40 例、58 次调用，未发现执行中断、超时或限流。该观察工具问题与模型任务执行分开记录在 observer-notes.json、sqlite-monitor-status.json。

耗时按逐次网络调用计算，含 JSON 修正，百分位使用 NumPy 默认线性插值；不含连接检查、判分及数据库处理。各用途超时和 429 均为 0。

| 用途 | 调用数 | P50 秒 | P95 秒 | 最长秒 |
|---|---:|---:|---:|---:|
| 全部 | 58 | 7.612 | 15.840 | 17.596 |
| consolidation_conflict | 25 | 7.332 | 14.651 | 17.596 |
| consolidation_conflict_repair | 4 | 5.817 | 6.924 | 7.030 |
| consolidation_dependency | 8 | 11.430 | 15.193 | 15.563 |
| consolidation_dependency_repair | 1 | 2.836 | 2.836 | 2.836 |
| consolidation_merge | 17 | 6.029 | 15.924 | 17.254 |
| consolidation_merge_repair | 3 | 10.086 | 16.751 | 17.492 |

## 正式双判结果

两轮由未参与本轮代码或提示词修改的全新独立子代理 `/root/co05_judge_a`、`/root/co05_judge_b` 完成，均为 `gpt-6-astra`、推理 `max`、`fork_turns=none`。只提供本轮材料、冻结评分说明和格式约定，不提供实现者判断、历史结果或另一轮结果。A 完成 40 例后，对 83 个文件保存 SHA-256，将目录权限设为 000 并验证读取为 PermissionError，然后才启动 B；B 完成后恢复 A，83 个哈希全部未变。父会话在两轮完成后才读取语义判分。

每轮原评分、细节评分、写回审计均通过格式、案例 ID、顺序和材料指纹核验。使用未修改的 score.py 离线计算，正向项取 AND、损坏项取 OR，不人工调和分歧。历史比较来自 [CO-02 阶段 2](stage2-glm-20261013.md) 和 [CO-03 上一轮](details-v3-20261013.md) 的同一公开集双判。

| 指标 | 阶段 2 双判 | 上一轮双判 | CO-05 不利双判 | CO-05 预登记条件 |
|---|---:|---:|---:|---|
| 误合并（含丢细节） | 1/8，12.50% | 0/4，0.00% | 0/7，0.00% | ≤5% |
| 合并丢细节（已含于上一行） | 1/8 | 0/4 | 0/7 | 并入误合并 |
| 应合并识别 | 8/12 | 4/12 | 7/12 | ≥7/12 |
| 建议误报（标签诊断次数） | 0 | 0 | 1 | 单列 |
| 互有细节建议命中 | 不适用，0/0 | 不适用，0/0 | 不适用，0/0 | 公开集无标签 |
| 矛盾一致 | 5/10 | 1/10 | 6/10 | 计入下方合计 |
| 依赖一致 | 4/8 | 2/8 | 6/8 | 计入下方合计 |
| 矛盾与依赖一致合计 | 9/18 | 3/18 | 12/18 | ≥7/18 |
| 误拒合并（仅细节判断诊断） | 1/12 | 0/12 | 1/12 | 单列 |
| 实际写回损坏 | 4/40 | 0/40 | 1/40 | ≤2/40 |
| 有效案例 | 40/40 | 40/40 | 40/40 | 没有基础设施作废 |
| invalid_output | 1 | 19 | 6 | 逐项诊断 |

上表只并列实测值与预登记条件，不给出验收或取舍结论。原计分器的历史 feasible 字段不代替 CO-05 的五项条件。实际合并为 CS001、CS003、CS004、CS007、CS009、CS010、CS012；相信程度上限 7/7、来源继承 7/7、受保护正文/相信程度 4/4，受保护对判合并错误 0。

| 指标 | A | B | 不利双判 |
|---|---:|---:|---:|
| 误合并（含丢细节） | 0/7 | 0/7 | 0/7 |
| 应合并识别 | 7/12 | 7/12 | 7/12 |
| 建议误报 | 1 | 1 | 1 |
| 矛盾一致 | 7/10 | 6/10 | 6/10 |
| 依赖一致 | 7/8 | 6/8 | 6/8 |
| 矛盾与依赖一致合计 | 14/18 | 12/18 | 12/18 |
| 误拒合并（仅细节判断诊断） | 1/12 | 1/12 | 1/12 |
| 实际写回损坏 | 0/40 | 1/40 | 1/40 |

五对未识别仍在 12 对分母内：CS002 的时间数字误拒、CS005 的 evidence 类型错误、CS006 缺少 keep_id、CS008 非逐字引文，以及 CS011 的 mutual_details 判断。前四项在下方拒绝诊断中记录。CS011 两轮都认为“记梦一直用纸质本子”包含“习惯用纸本记录梦”的信息，可原样保留前者；模型却将“习惯”当作另一侧独有细节，生成 co-3 合并待核对建议，故计 1 次建议误报和 1 次狭义误拒。依 v2，错误待核对建议本身不计作写回损坏。

## 两轮分歧

原评分 3 个布尔字段分歧，涉及 3 例；细节评分无分歧；写回审计 1 个分歧（同为 CS039）。全部取不利结论，未让任一判分者参考另一轮重判。

| 案例与字段 | A | B | 不利结论 | 分歧依据 |
|---|---|---|---|---|
| CS027 / contradiction_results/0/must_keep/0 | false | true | false | A 认为未保存时间性解释，旧正文没有落实过去偏好范围；B 认为原文“不再是最爱”仍体现过去曾最爱。两轮 resolution_correct/source_grounded 本已为 false，合计结果不变。 |
| CS032 / contradiction_results/0/must_keep/0 | true | false | false | A 认为保存了蓝白海报归属争议且未选边；B 认为标注/报告只写“市集海报”，遗漏要求保留的“最终海报”范围。 |
| CS039 / dependency_results/0/source_grounded（b） | true | false | false | A 认为正确说明已删除依据不可用、Iris 复述非独立支持；B 认为新增的“正文为null，无法确认原始来源曾记录的内容”不符合导出 before/after 仍存原文与来源的事实。 |
| CS039 / write_audit/has_damage | false | true | true | A 认为未改能力事实/相信程度，也未将删除当反证；B 将同一条关于原依据正文为空的已保存标注判为写回损坏。 |

CS039 的损坏判定针对工作项 1 写入 b 的标注；工作项 2 对 d 的输出被逐字引文校验拒绝，二者不混算。补充复核上下文：材料层对 lifecycle=deleted 的 a 实际向模型显示 content=null、sources=[]，而评测 before/after 保留“仲榕上了两个月吉他维修入门课”及来源原文。这一展示差异记录在 cs039-material-context.json，仍保留 B 的不利判定，不改变 1/40 损坏及 6/8 依赖一致，也未据此修改代码、提示词、材料或评分。

## invalid_output 逐项核查

上一轮 19 项（unknown_report_identifier 16、unknown_report_name 2、unknown_report_subject 1）；本轮 6 项：unknown_report_number 1、ungrounded_report_quote 3、JSON 修正后 invalid_output 2。unknown_report_identifier、unknown_report_name、unknown_report_subject 均为 0。以下是执行者的拒绝原因取证，不替代外部语义判分。

| 案例/项目 | 拒绝类别 | 是否真实输出错误及证据 |
|---|---|---|
| CS002/1 | unknown_report_number | **否，确定误拒。** reason 比较创建时间 18:01 与 18:05；材料 created_at 分别为 `2026-11-03T18:01:00+08:00`、`2026-11-03T18:05:00+08:00`。数字提取先删除拉丁标识符，把 `T18` 一起去掉，允许数字中缺失 18。 |
| CS008/1 | ungrounded_report_quote | **是，违反冻结的逐字引文约定。** 引号内把“设定／背景身份”“守钟人／守护北塔时钟的人”拼成对照，不是任何材料的连续原文；两边语义有依据，未据此认定有凭空事实。 |
| CS025/1 | ungrounded_report_quote | **是，违反逐字引文约定。** “曾计划14号开幕”“曾定于11月14日14点在榕庭”是有依据的历史释义，但不是连续原文。 |
| CS026/1 | invalid_output | **是，报告 JSON 字段错误。** 首次和唯一一次修正均含不允许的 unique_details，离线复核均为 invalid_fields。 |
| CS027/1 | invalid_output | **是，报告 JSON 字段错误。** 首次和唯一一次修正均含不允许的 unique_details，离线复核均为 invalid_fields。 |
| CS039/2 | ungrounded_report_quote | **是，违反逐字引文约定。** 引号内将原文仲榕替换成合法的 `主体[zhong_a]`，含义有依据，但冻结提示词禁止在引文里改称谓。 |

合计：5 项真实输出约定错误（2 个字段错误、3 个逐字引文错误），1 项确定误拒。真实“引用格式错误”不等于认定发生了事实虚构；三个引文项的具体差别在表中列明。上述 6 项均未写回，全部保留在本轮质量结果中。

另有 2 项 unsafe_write：CS005/1 的 evidence 使用字符串 s1/s3 而非可见来源消息整数 ID；CS006/1 在 decision=merge 时缺少 keep_id。这两项都是实际结构错误，没有写回。限定词与其余写回校验依 CO-05 保持不变，因此本轮不修 CS002 的时间数字问题，也不调整引文约定；交由规划者另行安排。

## 材料与指纹

完整材料、调用记录、原始输出、判分、测试日志和诊断均在仓库外：

`/Users/cassia/Local/Code/iris-eval-artifacts/consolidation-subject-validation-20261013/`

- 源码/运行器指纹：`04017d4b11083f3a12137684725b8420a4066effdd7295b6bfced0d209085c88`（正式运行 head；本报告新增后该含报告的综合指纹自然变化）。
- 冻结语料文件 SHA-256：`4600ec32fa27d37cc2b44084dabab1b003dcc70c24205c5e268dbdd80247ac75`。
- 正式材料 SHA-256：`2341e8ae40821ece759b2b1caf01e6f7a19aec51db378257413423476007ed51`。
- 原始运行：run-01/；有效性和耗时：formal-audit.json、model-calls-safe.json；拒绝诊断：rejection-diagnostics.json、rejection-validation-checks.json、rejection-review.json。
- 先红后绿：tdd-red.log、tdd-green.log、scope-and-tdd-audit.json；全量测试：pytest-313.log、pytest-312.log；构建：build.log。
- 双判：judge-a/、judge-b/、各自 frozen-sha256 JSON、judging-isolation.json；官方计分输出：score-a/report.json、score-b/report.json、score-double/report.json；汇总：scoring-summary.json。
- 未寻找或运行隐藏集。没有新增模型用途、修改 scheduler 或其他工作线源码，也没有新增迁移或前端改动。
