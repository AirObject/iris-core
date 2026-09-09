# 文档整理报告

> 本文仅记录历史整理过程，不提供现行约束。旧权威关系及旧路径均为历史描述；本次只适配迁移链接，现行入口见[INDEX](../INDEX.md)，冻结原始文档见[reference](../reference/README.md)。

历史记录日期：2026-09-09。包含首次文档整理、收尾审查及配置详细契约设计的分段记录，不包含业务实现成果。本报告作为一次性整理的历史记录保留，不再要求每个代码任务重做逐节哈希、全文还原或全量覆盖映射。当前任务由[CURRENT_TASK.md](CURRENT_TASK.md)记录，STATUS.md在切片完成节点简短更新。

下列起始状态、基线哈希、原覆盖映射和完整性结果记录首次整理时的事实；同日的收尾审查与本地基线范围见[收尾记录](#closeout-review)。配置契约归并时留存的哈希、行号与增量覆盖见[契约审查记录](#configuration-contract-review)。全文中的“本次／当前”、授权状态和检查结论均限于所记录的历史轮次；历史哈希、链接及正文校验结果不代表当前文件版本，不据此宣称当前检查通过。

## 起始状态与生效指引

工作目录为 `/Users/cassia/Local/Code/iris_memory_core`，分支 `main`，HEAD `eb5ebcb`。开始时没有受跟踪文件；三份原文都是用户已有的未跟踪文件。没有现有docs、工作记录、拆分目录、业务源码、依赖清单、测试／构建脚本或部署文件，因此本轮建立一个索引体系。

检查了 `/`、`/Users`、`/Users/cassia`、`/Users/cassia/Local`、`/Users/cassia/Local/Code`、仓库根目录的AGENTS.md与AGENTS.override.md，以及仓库内潜在嵌套指引：均未发现已有项目指引。用户级 `/Users/cassia/.codex/AGENTS.md` 存在但为空，未发现同级override。本轮新增根 [AGENTS.md](../../AGENTS.md)，它是当前仓库实际生效的根指引；没有根override覆盖，也没有新建override。没有修改全局配置或其他仓库指引。

首次整理未发现提交或PR的项目约定，故当时保留未暂存、未提交的可审查文件，三份原文也保持未跟踪状态。后续用户已明确授权收尾审查通过后，将原文与整理成果一并作本地文档基线提交；没有授权推送、合并或部署。

## 原文保护与识别

按实际标题及完整正文识别；三份均已完整读取，长文分段读取并补读了输出截断处。哈希算法为SHA-256，计算原始文件字节，包含换行。文件名、路径、内容、大小未变，未移动或重命名。

| 实际路径（仓库相对） | 标题／职责 | 字节数／行数 | 整理前SHA-256 | 整理后SHA-256 | 结果 |
| --- | --- | --- | --- | --- | --- |
| [companion_memory_cognition_system_design_integrated.md](../reference/companion_memory_cognition_system_design_integrated.md) | 独立陪伴角色记忆与认知系统设计／产品行为 | 100296／1263 | `ce997355992b359fcad08c62284b206db2d64dad46a165a457cd801646a41f62` | `ce997355992b359fcad08c62284b206db2d64dad46a165a457cd801646a41f62` | 一致 |
| [companion_memory_module_design_provider_logging_config.md](../reference/companion_memory_module_design_provider_logging_config.md) | 独立陪伴角色记忆与认知系统：技术架构与模块设计草稿／工程及模块 | 116264／1287 | `fb15ef0eaa5e3429c9cac7ebdc8f0e6f1114c5a39d8b12fbaba72856d5c362ed` | `fb15ef0eaa5e3429c9cac7ebdc8f0e6f1114c5a39d8b12fbaba72856d5c362ed` | 一致 |
| [CODING_STANDARDS.md](../reference/CODING_STANDARDS.md) | 代码规范：实现自描述与文档边界／实现表达 | 15538／235 | `be6a5909e936e4fab27d5343f67d81c774caedd150260095d09b399cfbc1af18` | `be6a5909e936e4fab27d5343f67d81c774caedd150260095d09b399cfbc1af18` | 一致 |

## 整理与同步方法

按完整职责章节提取正文：产品原文24个编号章归入13份主题文件，架构16个编号章归入11份主题文件和15份原有模块契约入口。架构第6节总标题由所有权文件中的模块索引承接，15个模块正文分别保存；I01与I02完整保留在所有权文件，不新建逻辑模块。代码规范全部保留原文按需完整阅读，不复制第二份。

阅读视图包含原文完整段落与表格，新增内容限于来源、局部定义、导航、模块边界定位和文档锚点。正文中的章节、模块、事务、验收与资料标记转换成真实链接；代码框不改写示意内容，其文档标记附单独定位链接。原文目录作为原文导航保留，新的任务导航由INDEX承接。

原文仍是权威依据。后续规则变动先修改对应原文，再同步受影响的阅读视图并核对相对链接；本映射及哈希仅作整理历史保留，不要求持续重做；不得只改视图形成分叉。原文的建议、示例、未定事项和草名保持原有性质。原代码树与prompt示例含编号不构成代码规范的例外，未来实现须语义化表达。

<a id="coverage"></a>

## 逐节覆盖映射

下表按原始实际标题逐项列出，忽略代码框中看似标题的注释；章节行号对应以上历史字节基线。每个已拆分小节的完整正文属于所列视图，并未仅保存标题或摘要。原文定位链接结合行号使用；视图提供每个标题的显式锚点。新增§11.9及后续章节行号偏移由[增量覆盖映射](#configuration-contract-review)承接，旧source-line锚点保留基线含义，不冒充当前行号。

### 独立陪伴角色记忆与认知系统设计／产品行为

| 原文章节／标题及行号 | 归属与保留原因 |
| --- | --- |
| [L1 · 独立陪伴角色记忆与认知系统设计](../reference/companion_memory_cognition_system_design_integrated.md#独立陪伴角色记忆与认知系统设计) | [product/overview.md](../product/overview.md#独立陪伴角色记忆与认知系统设计)；本节完整正文 |
| [L3 · 文档定位](../reference/companion_memory_cognition_system_design_integrated.md#文档定位) | [product/overview.md](../product/overview.md#source-line-3)；本节完整正文 |
| [L9 · 目录](../reference/companion_memory_cognition_system_design_integrated.md#目录) | [原文目录](../reference/companion_memory_cognition_system_design_integrated.md#目录)；保留原始导航，任务路线使用[INDEX](../INDEX.md) |
| [L38 · 1. 系统定位与范围](../reference/companion_memory_cognition_system_design_integrated.md#section-01) | [product/overview.md](../product/overview.md#source-line-38)；本节完整正文 |
| [L40 · 1.1 一个角色的统一认知系统](../reference/companion_memory_cognition_system_design_integrated.md#section-01) | [product/overview.md](../product/overview.md#source-line-40)；本节完整正文 |
| [L48 · 1.2 自治与外部执行](../reference/companion_memory_cognition_system_design_integrated.md#section-01) | [product/overview.md](../product/overview.md#source-line-48)；本节完整正文 |
| [L56 · 1.3 多入口支持与暂缓范围](../reference/companion_memory_cognition_system_design_integrated.md#section-01) | [product/overview.md](../product/overview.md#source-line-56)；本节完整正文 |
| [L64 · 1.4 不纳入的职责](../reference/companion_memory_cognition_system_design_integrated.md#section-01) | [product/overview.md](../product/overview.md#source-line-64)；本节完整正文 |
| [L70 · 2. 核心术语与职责](../reference/companion_memory_cognition_system_design_integrated.md#section-02) | [product/overview.md](../product/overview.md#source-line-70)；本节完整正文 |
| [L113 · 3. 信息组织与逻辑对象](../reference/companion_memory_cognition_system_design_integrated.md#section-03) | [product/overview.md](../product/overview.md#source-line-113)；本节完整正文 |
| [L141 · 4. 输入与接入规则](../reference/companion_memory_cognition_system_design_integrated.md#section-04) | [product/input-and-media.md](../product/input-and-media.md#source-line-141)；本节完整正文 |
| [L143 · 4.1 保留原始语境](../reference/companion_memory_cognition_system_design_integrated.md#section-04) | [product/input-and-media.md](../product/input-and-media.md#source-line-143)；本节完整正文 |
| [L151 · 4.2 自身输出和行动反馈](../reference/companion_memory_cognition_system_design_integrated.md#section-04) | [product/input-and-media.md](../product/input-and-media.md#source-line-151)；本节完整正文 |
| [L157 · 4.3 输入与管理操作分离](../reference/companion_memory_cognition_system_design_integrated.md#section-04) | [product/input-and-media.md](../product/input-and-media.md#source-line-157)；本节完整正文 |
| [L163 · 4.4 专注期的输入例外](../reference/companion_memory_cognition_system_design_integrated.md#section-04) | [product/input-and-media.md](../product/input-and-media.md#source-line-163)；本节完整正文 |
| [L169 · 5. 多媒体理解、隔离存储与清理](../reference/companion_memory_cognition_system_design_integrated.md#section-05) | [product/input-and-media.md](../product/input-and-media.md#source-line-169)；本节完整正文 |
| [L171 · 5.1 理解优先级](../reference/companion_memory_cognition_system_design_integrated.md#section-05) | [product/input-and-media.md](../product/input-and-media.md#source-line-171)；本节完整正文 |
| [L177 · 5.2 媒体理解被拒绝](../reference/companion_memory_cognition_system_design_integrated.md#section-05) | [product/input-and-media.md](../product/input-and-media.md#source-line-177)；本节完整正文 |
| [L187 · 5.3 文件与业务引用](../reference/companion_memory_cognition_system_design_integrated.md#section-05) | [product/input-and-media.md](../product/input-and-media.md#source-line-187)；本节完整正文 |
| [L195 · 5.4 无引用清理](../reference/companion_memory_cognition_system_design_integrated.md#section-05) | [product/input-and-media.md](../product/input-and-media.md#source-line-195)；本节完整正文 |
| [L205 · 6. 独立入口队列、三段式批次与终结规则](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-205)；本节完整正文 |
| [L207 · 6.1 一个具体入口，一套缓存总结队列](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-207)；本节完整正文 |
| [L215 · 6.2 三段的定义与方向](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-215)；本节完整正文 |
| [L234 · 6.3 输入推进与批次冻结](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-234)；本节完整正文 |
| [L244 · 6.4 学习指令中的范围约束](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-244)；本节完整正文 |
| [L252 · 6.5 保护、容量与正式记忆的边界](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-252)；本节完整正文 |
| [L260 · 6.6 成功终结：包括零记忆结果](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-260)；本节完整正文 |
| [L268 · 6.7 普通学习失败：失败终结，但保留历史衔接](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-268)；本节完整正文 |
| [L278 · 6.8 明确敏感拒学：终结并切断本轮历史衔接](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-278)；本节完整正文 |
| [L290 · 6.9 三种终结结果对照](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-290)；本节完整正文 |
| [L299 · 6.10 滚动示例](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-299)；本节完整正文 |
| [L313 · 6.11 终态与提交完整性](../reference/companion_memory_cognition_system_design_integrated.md#section-06) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-313)；本节完整正文 |
| [L321 · 7. 学习与内部自治](../reference/companion_memory_cognition_system_design_integrated.md#section-07) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-321)；本节完整正文 |
| [L323 · 7.1 触发事件](../reference/companion_memory_cognition_system_design_integrated.md#section-07) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-323)；本节完整正文 |
| [L329 · 7.2 加工与分类](../reference/companion_memory_cognition_system_design_integrated.md#section-07) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-329)；本节完整正文 |
| [L337 · 7.3 权限与持续反馈](../reference/companion_memory_cognition_system_design_integrated.md#section-07) | [product/batches-and-learning.md](../product/batches-and-learning.md#source-line-337)；本节完整正文 |
| [L347 · 8. 批次来源、窗口与对象级生命周期](../reference/companion_memory_cognition_system_design_integrated.md#section-08) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-347)；本节完整正文 |
| [L349 · 8.1 窗口只能来自冻结的本批次](../reference/companion_memory_cognition_system_design_integrated.md#section-08) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-349)；本节完整正文 |
| [L359 · 8.2 来源快照字段](../reference/companion_memory_cognition_system_design_integrated.md#section-08) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-359)；本节完整正文 |
| [L367 · 8.3 中段目标、上下文和派生依据分别记录](../reference/companion_memory_cognition_system_design_integrated.md#section-08) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-367)；本节完整正文 |
| [L375 · 8.4 遗忘和删除针对对象](../reference/companion_memory_cognition_system_design_integrated.md#section-08) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-375)；本节完整正文 |
| [L383 · 8.5 来源访问与外部缓存隔离](../reference/companion_memory_cognition_system_design_integrated.md#section-08) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-383)；本节完整正文 |
| [L391 · 9. 正式记忆与关系](../reference/companion_memory_cognition_system_design_integrated.md#section-09) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-391)；本节完整正文 |
| [L393 · 9.1 记忆语义字段](../reference/companion_memory_cognition_system_design_integrated.md#section-09) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-393)；本节完整正文 |
| [L411 · 9.2 主体联系](../reference/companion_memory_cognition_system_design_integrated.md#section-09) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-411)；本节完整正文 |
| [L417 · 9.3 扮演与其他关系](../reference/companion_memory_cognition_system_design_integrated.md#section-09) | [product/provenance-and-memory.md](../product/provenance-and-memory.md#source-line-417)；本节完整正文 |
| [L425 · 10. 相信程度、保留强度与防乒乓](../reference/companion_memory_cognition_system_design_integrated.md#section-10) | [product/lifecycle.md](../product/lifecycle.md#source-line-425)；本节完整正文 |
| [L427 · 10.1 相信程度的统一量表](../reference/companion_memory_cognition_system_design_integrated.md#section-10) | [product/lifecycle.md](../product/lifecycle.md#source-line-427)；本节完整正文 |
| [L447 · 10.2 保留强度](../reference/companion_memory_cognition_system_design_integrated.md#section-10) | [product/lifecycle.md](../product/lifecycle.md#source-line-447)；本节完整正文 |
| [L455 · 10.3 双指标如何共同参与](../reference/companion_memory_cognition_system_design_integrated.md#section-10) | [product/lifecycle.md](../product/lifecycle.md#source-line-455)；本节完整正文 |
| [L463 · 10.4 双阈值滞回](../reference/companion_memory_cognition_system_design_integrated.md#section-10) | [product/lifecycle.md](../product/lifecycle.md#source-line-463)；本节完整正文 |
| [L497 · 11. 召回、使用反馈与记忆生命周期](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-497)；本节完整正文 |
| [L499 · 11.1 有效记忆与纠正](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-499)；本节完整正文 |
| [L505 · 11.2 遗忘](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-505)；本节完整正文 |
| [L511 · 11.3 独立深度召回](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-511)；本节完整正文 |
| [L519 · 11.4 使用反馈和阈值恢复](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-519)；本节完整正文 |
| [L529 · 11.5 删除](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-529)；本节完整正文 |
| [L539 · 11.6 生命周期总览](../reference/companion_memory_cognition_system_design_integrated.md#section-11) | [product/lifecycle.md](../product/lifecycle.md#source-line-539)；本节完整正文 |
| [L558 · 12. 自我认知与稳定 persona](../reference/companion_memory_cognition_system_design_integrated.md#section-12) | [product/self-and-persona.md](../product/self-and-persona.md#source-line-558)；本节完整正文 |
| [L560 · 12.1 开放的自我认知](../reference/companion_memory_cognition_system_design_integrated.md#section-12) | [product/self-and-persona.md](../product/self-and-persona.md#source-line-560)；本节完整正文 |
| [L568 · 12.2 初始化自我](../reference/companion_memory_cognition_system_design_integrated.md#section-12) | [product/self-and-persona.md](../product/self-and-persona.md#source-line-568)；本节完整正文 |
| [L574 · 12.3 persona 的内容与监督](../reference/companion_memory_cognition_system_design_integrated.md#section-12) | [product/self-and-persona.md](../product/self-and-persona.md#source-line-574)；本节完整正文 |
| [L580 · 12.4 更新时机](../reference/companion_memory_cognition_system_design_integrated.md#section-12) | [product/self-and-persona.md](../product/self-and-persona.md#source-line-580)；本节完整正文 |
| [L588 · 12.5 来源变化与摘要滞后](../reference/companion_memory_cognition_system_design_integrated.md#section-12) | [product/self-and-persona.md](../product/self-and-persona.md#source-line-588)；本节完整正文 |
| [L598 · 13. 当前状态：外部权威的现在](../reference/companion_memory_cognition_system_design_integrated.md#section-13) | [product/current-state.md](../product/current-state.md#source-line-598)；本节完整正文 |
| [L600 · 13.1 定义](../reference/companion_memory_cognition_system_design_integrated.md#section-13) | [product/current-state.md](../product/current-state.md#source-line-600)；本节完整正文 |
| [L606 · 13.2 管理权限](../reference/companion_memory_cognition_system_design_integrated.md#section-13) | [product/current-state.md](../product/current-state.md#source-line-606)；本节完整正文 |
| [L614 · 13.3 持续时间](../reference/companion_memory_cognition_system_design_integrated.md#section-13) | [product/current-state.md](../product/current-state.md#source-line-614)；本节完整正文 |
| [L624 · 13.4 与历史记忆、persona、目标的分界](../reference/companion_memory_cognition_system_design_integrated.md#section-13) | [product/current-state.md](../product/current-state.md#source-line-624)；本节完整正文 |
| [L632 · 14. 多目标、待办与提醒](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-632)；本节完整正文 |
| [L634 · 14.1 多个未来目标并行](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-634)；本节完整正文 |
| [L640 · 14.2 两种来源](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-640)；本节完整正文 |
| [L648 · 14.3 相似去重](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-648)；本节完整正文 |
| [L656 · 14.4 状态](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-656)；本节完整正文 |
| [L664 · 14.5 截止时间和提醒提前量](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-664)；本节完整正文 |
| [L672 · 14.6 过期处理](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-672)；本节完整正文 |
| [L680 · 14.7 对外提供](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-680)；本节完整正文 |
| [L686 · 14.8 专注梦境对目标操作的影响](../reference/companion_memory_cognition_system_design_integrated.md#section-14) | [product/goals.md](../product/goals.md#source-line-686)；本节完整正文 |
| [L694 · 15. 对外信息获取与入口边界](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-694)；本节完整正文 |
| [L696 · 15.1 两类获取请求](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-696)；本节完整正文 |
| [L702 · 15.2 第一类：回复准备](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-702)；本节完整正文 |
| [L718 · 15.3 其他入口的待处理提示](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-718)；本节完整正文 |
| [L730 · 15.4 缓存隔离与正式记忆共享](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-730)；本节完整正文 |
| [L738 · 15.5 第二类：定向记忆查询](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-738)；本节完整正文 |
| [L746 · 15.6 返回标识与使用路径](../reference/companion_memory_cognition_system_design_integrated.md#section-15) | [product/retrieval.md](../product/retrieval.md#source-line-746)；本节完整正文 |
| [L754 · 16. 梦境、专注隔离与入口缓存回流](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-754)；本节完整正文 |
| [L756 · 16.1 梦境职责](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-756)；本节完整正文 |
| [L762 · 16.2 内部整理与发布](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-762)；本节完整正文 |
| [L780 · 16.3 专注模式的入口门控](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-780)；本节完整正文 |
| [L805 · 16.4 每入口独立的梦境缓存](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-805)；本节完整正文 |
| [L815 · 16.5 梦境结束后按入口回流](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-815)；本节完整正文 |
| [L827 · 16.6 清空的准确含义与业务恢复](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-827)；本节完整正文 |
| [L837 · 16.7 模式切换与可观测性](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-837)；本节完整正文 |
| [L845 · 16.8 来源支持、循环和恢复影响](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-845)；本节完整正文 |
| [L853 · 16.9 非专注梦境与异常](../reference/companion_memory_cognition_system_design_integrated.md#section-16) | [product/dream.md](../product/dream.md#source-line-853)；本节完整正文 |
| [L861 · 17. 故障、提醒与审计](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-861)；本节完整正文 |
| [L863 · 17.1 运作原则](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-863)；本节完整正文 |
| [L869 · 17.2 provider 长期故障](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-869)；本节完整正文 |
| [L877 · 17.3 传输与业务状态](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-877)；本节完整正文 |
| [L885 · 17.4 突然失忆风险](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-885)；本节完整正文 |
| [L891 · 17.5 审计隔离](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-891)；本节完整正文 |
| [L897 · 17.6 缺口、积压与恢复信息分别显示](../reference/companion_memory_cognition_system_design_integrated.md#section-17) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-897)；本节完整正文 |
| [L905 · 18. 逻辑架构](../reference/companion_memory_cognition_system_design_integrated.md#section-18) | [product/overview.md](../product/overview.md#source-line-905)；本节完整正文 |
| [L926 · 18.1 主信息流](../reference/companion_memory_cognition_system_design_integrated.md#section-18) | [product/overview.md](../product/overview.md#source-line-926)；本节完整正文 |
| [L951 · 19. 概念接口与不可混淆的状态](../reference/companion_memory_cognition_system_design_integrated.md#section-19) | [product/overview.md](../product/overview.md#source-line-951)；本节完整正文 |
| [L984 · 20. Web 管理面板](../reference/companion_memory_cognition_system_design_integrated.md#section-20) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-984)；本节完整正文 |
| [L986 · 20.1 初始化引导](../reference/companion_memory_cognition_system_design_integrated.md#section-20) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-986)；本节完整正文 |
| [L992 · 20.2 图形解释](../reference/companion_memory_cognition_system_design_integrated.md#section-20) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-992)；本节完整正文 |
| [L1008 · 20.3 日常页面](../reference/companion_memory_cognition_system_design_integrated.md#section-20) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-1008)；本节完整正文 |
| [L1025 · 20.4 关键配置展示](../reference/companion_memory_cognition_system_design_integrated.md#section-20) | [product/operations-and-management.md](../product/operations-and-management.md#source-line-1025)；本节完整正文 |
| [L1033 · 21. 验收场景](../reference/companion_memory_cognition_system_design_integrated.md#section-21) | [product/acceptance.md](../product/acceptance.md#source-line-1033)；本节完整正文 |
| [L1150 · 22. 待细化的参数与异常契约](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1150)；本节完整正文 |
| [L1154 · P04. 目标去重和提醒时序](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1154)；本节完整正文 |
| [L1160 · P05. 当前状态的时间契约](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1160)；本节完整正文 |
| [L1166 · P06. 多媒体结果的有效性](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1166)；本节完整正文 |
| [L1172 · P07. 使用反馈、来源访问与persona发布细节](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1172)；本节完整正文 |
| [L1178 · P08. 短批次、最新辅助段和提前触发](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1178)；本节完整正文 |
| [L1184 · P09. 模式切换、回流开放时点与异常退出](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1184)；本节完整正文 |
| [L1192 · P10. 暂存介质、重启与物理资源故障](../reference/companion_memory_cognition_system_design_integrated.md#section-22) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1192)；本节完整正文 |
| [L1200 · 23. 参数与工程配置](../reference/companion_memory_cognition_system_design_integrated.md#section-23) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1200)；本节完整正文 |
| [L1242 · 24. 实施与验收组织](../reference/companion_memory_cognition_system_design_integrated.md#section-24) | [product/decisions-and-delivery.md](../product/decisions-and-delivery.md#source-line-1242)；本节完整正文 |

### 独立陪伴角色记忆与认知系统：技术架构与模块设计草稿／工程及模块

| 原文章节／标题及行号 | 归属与保留原因 |
| --- | --- |
| [L1 · 独立陪伴角色记忆与认知系统：技术架构与模块设计草稿](../reference/companion_memory_module_design_provider_logging_config.md#独立陪伴角色记忆与认知系统技术架构与模块设计草稿) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#独立陪伴角色记忆与认知系统技术架构与模块设计草稿)；本节完整正文 |
| [L7 · 目录](../reference/companion_memory_module_design_provider_logging_config.md#目录) | [原文目录](../reference/companion_memory_module_design_provider_logging_config.md#目录)；保留原始导航，任务路线使用[INDEX](../INDEX.md) |
| [L28 · 1. 架构结论与适用边界](../reference/companion_memory_module_design_provider_logging_config.md#section-01) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-28)；本节完整正文 |
| [L42 · 1.1 已给定的工程约束](../reference/companion_memory_module_design_provider_logging_config.md#section-01) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-42)；本节完整正文 |
| [L59 · 1.2 规模推导：用于设计，不是压测结论](../reference/companion_memory_module_design_provider_logging_config.md#section-01) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-59)；本节完整正文 |
| [L76 · 2. 运行结构与部署候选](../reference/companion_memory_module_design_provider_logging_config.md#section-02) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-76)；本节完整正文 |
| [L78 · 2.1 单容器内部结构](../reference/companion_memory_module_design_provider_logging_config.md#section-02) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-78)；本节完整正文 |
| [L109 · 2.2 存储候选](../reference/companion_memory_module_design_provider_logging_config.md#section-02) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-109)；本节完整正文 |
| [L123 · 2.3 持久化目录与网络](../reference/companion_memory_module_design_provider_logging_config.md#section-02) | [architecture/deployment-candidates.md](../architecture/deployment-candidates.md#source-line-123)；本节完整正文 |
| [L145 · 3. 同步时延与异步工作边界](../reference/companion_memory_module_design_provider_logging_config.md#section-03) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-145)；本节完整正文 |
| [L147 · 3.1 一秒接口契约](../reference/companion_memory_module_design_provider_logging_config.md#section-03) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-147)；本节完整正文 |
| [L155 · 3.2 回复准备路径](../reference/companion_memory_module_design_provider_logging_config.md#section-03) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-155)；本节完整正文 |
| [L176 · 3.3 写入类请求](../reference/companion_memory_module_design_provider_logging_config.md#section-03) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-176)；本节完整正文 |
| [L184 · 4. 持久化与允许损失范围](../reference/companion_memory_module_design_provider_logging_config.md#section-04) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-184)；本节完整正文 |
| [L186 · 4.1 接收承诺](../reference/companion_memory_module_design_provider_logging_config.md#section-04) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-186)；本节完整正文 |
| [L213 · 4.2 三种“重试”严格分开](../reference/companion_memory_module_design_provider_logging_config.md#section-04) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-213)；本节完整正文 |
| [L226 · 4.3 备份与物理故障](../reference/companion_memory_module_design_provider_logging_config.md#section-04) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-226)；本节完整正文 |
| [L234 · 5. 模块总览与数据所有权](../reference/companion_memory_module_design_provider_logging_config.md#section-05) | [architecture/ownership.md](../architecture/ownership.md#source-line-234)；本节完整正文 |
| [L236 · 5.1 十五个逻辑模块](../reference/companion_memory_module_design_provider_logging_config.md#section-05) | [architecture/ownership.md](../architecture/ownership.md#source-line-236)；本节完整正文 |
| [L258 · 5.2 基础设施](../reference/companion_memory_module_design_provider_logging_config.md#section-05) | [architecture/ownership.md](../architecture/ownership.md#source-line-258)；本节完整正文 |
| [L267 · 5.3 依赖约束](../reference/companion_memory_module_design_provider_logging_config.md#section-05) | [architecture/ownership.md](../architecture/ownership.md#source-line-267)；本节完整正文 |
| [L288 · 6. 模块契约草稿](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [十五个模块入口](../architecture/ownership.md#module-contracts)；总标题为索引，子节各自完整保留 |
| [L290 · M01 接入与入口注册](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/ingress.md](../modules/ingress.md#source-line-290)；本节完整正文 |
| [L303 · M02 运行模式与工作调度](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/runtime.md](../modules/runtime.md#source-line-303)；本节完整正文 |
| [L317 · M03 入口缓存与批次](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/buffers.md](../modules/buffers.md#source-line-317)；本节完整正文 |
| [L329 · M04 媒体](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/media.md](../modules/media.md#source-line-329)；本节完整正文 |
| [L343 · M05 认知加工](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/cognition.md](../modules/cognition.md#source-line-343)；本节完整正文 |
| [L357 · M06 记忆、来源与关系](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/memory.md](../modules/memory.md#source-line-357)；本节完整正文 |
| [L371 · M07 自我与persona](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/self-model.md](../modules/self-model.md#source-line-371)；本节完整正文 |
| [L381 · M08 检索与信息提供](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/retrieval.md](../modules/retrieval.md#source-line-381)；本节完整正文 |
| [L395 · M09 当前状态](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/state.md](../modules/state.md#source-line-395)；本节完整正文 |
| [L405 · M10 多目标与意图](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/goals.md](../modules/goals.md#source-line-405)；本节完整正文 |
| [L417 · M11 梦境整理](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/dream.md](../modules/dream.md#source-line-417)；本节完整正文 |
| [L429 · M12 Web与管理入口](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/management.md](../modules/management.md#source-line-429)；本节完整正文 |
| [L439 · M13 模型Provider](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/provider.md](../modules/provider.md#source-line-439)；本节完整正文 |
| [L451 · M14 日志与审计](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/logging.md](../modules/logging.md#source-line-451)；本节完整正文 |
| [L461 · M15 统一配置](../reference/companion_memory_module_design_provider_logging_config.md#section-06) | [modules/configuration.md](../modules/configuration.md#source-line-461)；本节完整正文 |
| [L473 · 7. 原子操作、检查点与恢复契约](../reference/companion_memory_module_design_provider_logging_config.md#section-07) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-473)；本节完整正文 |
| [L475 · 7.1 三层状态](../reference/companion_memory_module_design_provider_logging_config.md#section-07) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-475)；本节完整正文 |
| [L481 · 7.2 必须原子的业务操作](../reference/companion_memory_module_design_provider_logging_config.md#section-07) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-481)；本节完整正文 |
| [L501 · 7.3 学习提交的具体流程](../reference/companion_memory_module_design_provider_logging_config.md#section-07) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-501)；本节完整正文 |
| [L518 · 7.4 外部结果未知的限制](../reference/companion_memory_module_design_provider_logging_config.md#section-07) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-518)；本节完整正文 |
| [L526 · 7.5 索引一致性](../reference/companion_memory_module_design_provider_logging_config.md#section-07) | [architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md#source-line-526)；本节完整正文 |
| [L536 · 8. 模型能力接口与协议适配](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-536)；本节完整正文 |
| [L538 · 8.1 能力接口，不是三个通用URL](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-538)；本节完整正文 |
| [L555 · 8.2 ModelProfile与能力声明](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-555)；本节完整正文 |
| [L565 · 8.3 规范化错误与重试归属](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-565)；本节完整正文 |
| [L575 · 8.4 唯一入口、请求归因与执行流水线](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-575)；本节完整正文 |
| [L608 · 8.5 逻辑请求、网络尝试与缓存命中分开计数](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-608)；本节完整正文 |
| [L618 · 8.6 完整统计信息与统一口径](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-618)；本节完整正文 |
| [L634 · 8.7 Usage规范化与费用计算](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-634)；本节完整正文 |
| [L654 · 8.8 配额、预算与成本保护](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-654)；本节完整正文 |
| [L664 · 8.9 持久化、统计恢复与故障](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-664)；本节完整正文 |
| [L674 · 8.10 配置与安全边界](../reference/companion_memory_module_design_provider_logging_config.md#section-08) | [architecture/provider.md](../architecture/provider.md#source-line-674)；本节完整正文 |
| [L684 · 9. 内部上下文与成本控制](../reference/companion_memory_module_design_provider_logging_config.md#section-09) | [architecture/context-and-cost.md](../architecture/context-and-cost.md#source-line-684)；本节完整正文 |
| [L686 · 9.1 分离两个上下文构建器](../reference/companion_memory_module_design_provider_logging_config.md#section-09) | [architecture/context-and-cost.md](../architecture/context-and-cost.md#source-line-686)；本节完整正文 |
| [L705 · 9.2 请求频率控制](../reference/companion_memory_module_design_provider_logging_config.md#section-09) | [architecture/context-and-cost.md](../architecture/context-and-cost.md#source-line-705)；本节完整正文 |
| [L713 · 9.3 避免重复花费](../reference/companion_memory_module_design_provider_logging_config.md#section-09) | [architecture/context-and-cost.md](../architecture/context-and-cost.md#source-line-713)；本节完整正文 |
| [L725 · 9.4 预算与吞吐观察](../reference/companion_memory_module_design_provider_logging_config.md#section-09) | [architecture/context-and-cost.md](../architecture/context-and-cost.md#source-line-725)；本节完整正文 |
| [L737 · 10. 独立日志模块：等级、输出、审计与运行安全](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-737)；本节完整正文 |
| [L739 · 10.1 统一日志模型与三个记录类别](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-739)；本节完整正文 |
| [L751 · 10.2 标准等级与使用规则](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-751)；本节完整正文 |
| [L767 · 10.3 结构化字段与关联](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-767)；本节完整正文 |
| [L775 · 10.4 控制台、文件与Web三个输出端](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-775)；本节完整正文 |
| [L789 · 10.5 异步输出、背压与故障](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-789)；本节完整正文 |
| [L799 · 10.6 脱敏、权限与上下文隔离](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-799)；本节完整正文 |
| [L807 · 10.7 热配置与实现约束](../reference/companion_memory_module_design_provider_logging_config.md#section-10) | [architecture/logging.md](../architecture/logging.md#source-line-807)；本节完整正文 |
| [L817 · 11. 独立配置模块：统一参数、版本快照与安全热修改](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-817)；本节完整正文 |
| [L819 · 11.1 一个权威配置源，不是各模块各读一份文件](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-819)；本节完整正文 |
| [L837 · 11.2 参数注册表](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-837)；本节完整正文 |
| [L858 · 11.3 配置命名空间与修改范围](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-858)；本节完整正文 |
| [L878 · 11.4 热修改的生效类型](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-878)；本节完整正文 |
| [L895 · 11.5 变更、准备、发布与恢复](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-895)；本节完整正文 |
| [L919 · 11.6 运行中修改的具体例子](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-919)；本节完整正文 |
| [L933 · 11.7 少魔法变量，不把系统规则全部变成开关](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-933)；本节完整正文 |
| [L941 · 11.8 安全、权限与Web展示](../reference/companion_memory_module_design_provider_logging_config.md#section-11) | [architecture/configuration.md](../architecture/configuration.md#source-line-941)；本节完整正文 |
| [L953 · 12. 数据与接口草图](../reference/companion_memory_module_design_provider_logging_config.md#section-12) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-953)；本节完整正文 |
| [L955 · 12.1 权威业务数据分组](../reference/companion_memory_module_design_provider_logging_config.md#section-12) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-955)；本节完整正文 |
| [L980 · 12.2 外部能力接口](../reference/companion_memory_module_design_provider_logging_config.md#section-12) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-980)；本节完整正文 |
| [L1004 · 12.3 回复准备的逻辑返回](../reference/companion_memory_module_design_provider_logging_config.md#section-12) | [architecture/request-paths.md](../architecture/request-paths.md#source-line-1004)；本节完整正文 |
| [L1024 · 13. 代码组织建议](../reference/companion_memory_module_design_provider_logging_config.md#section-13) | [architecture/implementation-options.md](../architecture/implementation-options.md#source-line-1024)；本节完整正文 |
| [L1097 · 14. 工程验收与原需求映射](../reference/companion_memory_module_design_provider_logging_config.md#section-14) | [architecture/acceptance.md](../architecture/acceptance.md#source-line-1097)；本节完整正文 |
| [L1200 · 15. 实施顺序与设计冻结点](../reference/companion_memory_module_design_provider_logging_config.md#section-15) | [architecture/implementation-options.md](../architecture/implementation-options.md#source-line-1200)；本节完整正文 |
| [L1202 · 15.1 开发顺序](../reference/companion_memory_module_design_provider_logging_config.md#section-15) | [architecture/implementation-options.md](../architecture/implementation-options.md#source-line-1202)；本节完整正文 |
| [L1216 · 15.2 需要形成ADR的决定](../reference/companion_memory_module_design_provider_logging_config.md#section-15) | [architecture/implementation-options.md](../architecture/implementation-options.md#source-line-1216)；本节完整正文 |
| [L1239 · 16. 参考依据](../reference/companion_memory_module_design_provider_logging_config.md#section-16) | [architecture/references.md](../architecture/references.md#source-line-1239)；本节完整正文 |

### 代码规范：实现自描述与文档边界／实现表达

| 原文章节／标题及行号 | 归属与保留原因 |
| --- | --- |
| [L1 · 代码规范：实现自描述与文档边界](../reference/CODING_STANDARDS.md#代码规范实现自描述与文档边界) | [原文完整规范](../reference/CODING_STANDARDS.md#代码规范实现自描述与文档边界)；已有独立规范，不重复复制 |
| [L3 · 适用范围](../reference/CODING_STANDARDS.md#适用范围) | [原文完整规范](../reference/CODING_STANDARDS.md#适用范围)；已有独立规范，不重复复制 |
| [L9 · 核心原则](../reference/CODING_STANDARDS.md#核心原则) | [原文完整规范](../reference/CODING_STANDARDS.md#核心原则)；已有独立规范，不重复复制 |
| [L17 · 禁止将规划信息写入实现](../reference/CODING_STANDARDS.md#禁止将规划信息写入实现) | [原文完整规范](../reference/CODING_STANDARDS.md#禁止将规划信息写入实现)；已有独立规范，不重复复制 |
| [L19 · 禁止的内容](../reference/CODING_STANDARDS.md#禁止的内容) | [原文完整规范](../reference/CODING_STANDARDS.md#禁止的内容)；已有独立规范，不重复复制 |
| [L37 · 不应误伤真实业务信息](../reference/CODING_STANDARDS.md#不应误伤真实业务信息) | [原文完整规范](../reference/CODING_STANDARDS.md#不应误伤真实业务信息)；已有独立规范，不重复复制 |
| [L45 · 命名与目录](../reference/CODING_STANDARDS.md#命名与目录) | [原文完整规范](../reference/CODING_STANDARDS.md#命名与目录)；已有独立规范，不重复复制 |
| [L62 · 每个文件的独立可读性](../reference/CODING_STANDARDS.md#每个文件的独立可读性) | [原文完整规范](../reference/CODING_STANDARDS.md#每个文件的独立可读性)；已有独立规范，不重复复制 |
| [L64 · 文件级说明](../reference/CODING_STANDARDS.md#文件级说明) | [原文完整规范](../reference/CODING_STANDARDS.md#文件级说明)；已有独立规范，不重复复制 |
| [L87 · 独立可读不等于复制全部代码](../reference/CODING_STANDARDS.md#独立可读不等于复制全部代码) | [原文完整规范](../reference/CODING_STANDARDS.md#独立可读不等于复制全部代码)；已有独立规范，不重复复制 |
| [L95 · 注释与 docstring](../reference/CODING_STANDARDS.md#注释与-docstring) | [原文完整规范](../reference/CODING_STANDARDS.md#注释与-docstring)；已有独立规范，不重复复制 |
| [L97 · 表达要求](../reference/CODING_STANDARDS.md#表达要求) | [原文完整规范](../reference/CODING_STANDARDS.md#表达要求)；已有独立规范，不重复复制 |
| [L114 · 公共接口说明](../reference/CODING_STANDARDS.md#公共接口说明) | [原文完整规范](../reference/CODING_STANDARDS.md#公共接口说明)；已有独立规范，不重复复制 |
| [L131 · 引用规范](../reference/CODING_STANDARDS.md#引用规范) | [原文完整规范](../reference/CODING_STANDARDS.md#引用规范)；已有独立规范，不重复复制 |
| [L133 · 允许的引用](../reference/CODING_STANDARDS.md#允许的引用) | [原文完整规范](../reference/CODING_STANDARDS.md#允许的引用)；已有独立规范，不重复复制 |
| [L141 · 禁止的引用](../reference/CODING_STANDARDS.md#禁止的引用) | [原文完整规范](../reference/CODING_STANDARDS.md#禁止的引用)；已有独立规范，不重复复制 |
| [L151 · 文档与运行时的边界](../reference/CODING_STANDARDS.md#文档与运行时的边界) | [原文完整规范](../reference/CODING_STANDARDS.md#文档与运行时的边界)；已有独立规范，不重复复制 |
| [L153 · 文档不参与运行时决策](../reference/CODING_STANDARDS.md#文档不参与运行时决策) | [原文完整规范](../reference/CODING_STANDARDS.md#文档不参与运行时决策)；已有独立规范，不重复复制 |
| [L161 · 运行资源与设计文档不同](../reference/CODING_STANDARDS.md#运行资源与设计文档不同) | [原文完整规范](../reference/CODING_STANDARDS.md#运行资源与设计文档不同)；已有独立规范，不重复复制 |
| [L169 · 测试、日志与错误信息](../reference/CODING_STANDARDS.md#测试日志与错误信息) | [原文完整规范](../reference/CODING_STANDARDS.md#测试日志与错误信息)；已有独立规范，不重复复制 |
| [L171 · 测试](../reference/CODING_STANDARDS.md#测试) | [原文完整规范](../reference/CODING_STANDARDS.md#测试)；已有独立规范，不重复复制 |
| [L181 · 日志与错误](../reference/CODING_STANDARDS.md#日志与错误) | [原文完整规范](../reference/CODING_STANDARDS.md#日志与错误)；已有独立规范，不重复复制 |
| [L191 · 配置、常量与模块协作](../reference/CODING_STANDARDS.md#配置常量与模块协作) | [原文完整规范](../reference/CODING_STANDARDS.md#配置常量与模块协作)；已有独立规范，不重复复制 |
| [L203 · 未完成能力与维护](../reference/CODING_STANDARDS.md#未完成能力与维护) | [原文完整规范](../reference/CODING_STANDARDS.md#未完成能力与维护)；已有独立规范，不重复复制 |
| [L211 · 检查与合并门槛](../reference/CODING_STANDARDS.md#检查与合并门槛) | [原文完整规范](../reference/CODING_STANDARDS.md#检查与合并门槛)；已有独立规范，不重复复制 |
| [L213 · 自动检查](../reference/CODING_STANDARDS.md#自动检查) | [原文完整规范](../reference/CODING_STANDARDS.md#自动检查)；已有独立规范，不重复复制 |
| [L229 · 人工审查](../reference/CODING_STANDARDS.md#人工审查) | [原文完整规范](../reference/CODING_STANDARDS.md#人工审查)；已有独立规范，不重复复制 |

### 非标题正文区间及完整表格

| 原文 | 精确提取／保留范围 | 完整表格检查 |
| --- | --- | --- |
| [companion_memory_cognition_system_design_integrated.md](../reference/companion_memory_cognition_system_design_integrated.md) | 1236行进入正文视图；L9–L35留在原文，仅原目录、目录周围空行与架构模块总标题留在原文／索引；无业务段落遗漏 | 16张完整表、287条数据行；逐表原序行内容比对一致 |
| [companion_memory_module_design_provider_logging_config.md](../reference/companion_memory_module_design_provider_logging_config.md) | 1264行进入正文视图；L7–L25、L286–L289留在原文，仅原目录、目录周围空行与架构模块总标题留在原文／索引；无业务段落遗漏 | 22张完整表、280条数据行；逐表原序行内容比对一致 |
| [CODING_STANDARDS.md](../reference/CODING_STANDARDS.md) | 0行进入正文视图；L1–L235留在原文，完整规范按需读取原文件 | 4张完整表、24条数据行；直接链接原文 |

共享完整表没有拆成散行摘要：产品验收A01—A108、工程验收V01—V90、事务T01—T13和参考S01—S18均保留完整表，增加行锚点便于按任务定位。模块中的验收链接是阅读选择，范围扩大时需回完整表核对，不声称一个短链接清单覆盖该模块未来全部实现。

## 完整性核验结果与限制

- 三份原文字节哈希与整理前一致；路径、大小及未跟踪状态保留。
- 逐节映射覆盖234个实际标题；56个连续正文区间还原比对一致，无重叠。仅原目录／空行和架构第6节总标题留在原文及模块索引；代码规范完整留在原文件。
- 产品16张、架构22张完整表逐表逐行比对一致（合计567条数据行），含108项产品验收、90项工程验收、13项事务和18项参考资料。代码规范的4张表及24条数据行由原文直接保留。
- 新增文件1879处相对链接及原文40处目录链接检查无缺失路径／锚点；新增506个显式锚点在各文件内无重复，S01—S18均可定位。18个官方外链未联网验证。
- 实际路径集合为45个新增Markdown加3份受保护原文；逐文件对空基线生成并核查差异，新增范围仅根AGENTS与docs。受跟踪／暂存差异为空，HEAD仍为eb5ebcb；没有源码、测试、运行资源、依赖、迁移或部署改动。
- 无实现／构建／性能测试结果；本轮仅执行文档内容、映射、路径和仓库范围检查。

核对方法包括：逐章及逐子节与原文映射；56个原文连续区间在视图中各有一次完整正文；去除本轮添加的链接和锚点后逐字回比原始区间（跨章范围引用展开为各章链接时按记录还原）；原始表格按完整行序比较；按原文实际章节检查保留区间。不是以关键词次数代替覆盖检查。

另外按职责复核：冻结输入和来源所有权、普通失败留尾与敏感拒学不留尾、零结果成功、本地提交恢复与远程未知、对象删除与共享来源、专注门控及FIFO移交、外部状态和监督权限、Provider账本与日志分离、配置安全准入与旧快照并存。完整正文是这些限制的详细来源，导读没有批准新的例外。

相对文件和显式锚点采用本地解析检查；代码规范标题锚点按GitHub常见规则检查。没有安装Markdown渲染器，也没有逐页面图形渲染或验证所有客户端的自动标题算法。官方外链只保留与原文一致的18个地址及标记，没有联网检查页面、供应商现状或候选性能。未进行实现测试、构建、依赖安装、真实模型请求或压测；不作“系统已实现”或“语义无损绝对保证”的声明。

<a id="open-items"></a>

## 冲突、缺失与待确认项

本次配置切片已将注册表数据、冻结、接口和错误方面的缺失细化为[待批准决定D1–D7](../architecture/configuration.md#configuration-registry-decisions)。当前任务是文档契约待审查；下表其余未定事项保留，不自动获得本次实施授权。

本轮未发现需要整理者自行裁决的直接产品规则矛盾。下表区分互补解释、待定边界与尚未批准的候选，均不把整理结果当批准记录。用户要求的“已确定、建议、示例、待确认”是分类指引，原文草稿中的具体性质仍以对应措辞为准。

| 事项与性质 | 双方原文位置／具体依据 | 影响与本轮处理 |
| --- | --- | --- |
| 语言、部署、数据库、索引：候选与工程约束互补 | [系统§23](../reference/companion_memory_cognition_system_design_integrated.md#section-23)“语言、模型、数据库、暂存介质和协议未指定”；[架构§1.1、§2](../reference/companion_memory_module_design_provider_logging_config.md#section-01)给定Linux Docker、跨重启保留等约束，§2优先验证SQLite；[§13、§15.2](../reference/companion_memory_module_design_provider_logging_config.md#section-15)仍需批准Python候选和工程决定 | 不视为后文覆盖全部产品；未选HTTP框架、ORM、ANN、SDK或正式默认值，未开展验证 |
| 持久化建议与必须跨重启保留：工程细化 | [系统§16.4、§22 P10](../reference/companion_memory_cognition_system_design_integrated.md#section-22)待工程确定介质与恢复；[架构§4、§7](../reference/companion_memory_module_design_provider_logging_config.md#section-04)明确持久化确认与本地原子操作 | 保存新增工程约束；本地恢复不等于重放失败学习，未选择介质或承诺硬件绝对不丢 |
| 一秒范围与检索质量：需正式接口冻结 | [架构§1.1](../reference/companion_memory_module_design_provider_logging_config.md#section-01)基础响应目标1秒；[§3.1–3.3](../reference/companion_memory_module_design_provider_logging_config.md#section-03)查询完整基础时限及短事务写入；[§14 V28、§15.2](../reference/companion_memory_module_design_provider_logging_config.md#section-15)目标注入一秒、语义降级批准点 | 查询降级、各写接口具体预算须固定；700–800ms、200–300ms仍是试验起点，未测试 |
| 梦境切换与恢复开放：明确待批准 | [系统§16.6–16.9、P09](../reference/companion_memory_cognition_system_design_integrated.md#section-16)“建议”发布后开放且异常流程待定；[架构M02、§15.2](../reference/companion_memory_module_design_provider_logging_config.md#section-15)同一建议需正式冻结 | 不自动批准开放时点、在途收尾和管理员中止／恢复权限；专注拒绝范围保持原规则 |
| 三段与短批次参数：未固定数值 | [系统§6、§22 P08、§23](../reference/companion_memory_cognition_system_design_integrated.md#section-22)提前触发与安静入口尾部待定；[架构§9.2、§11.7](../reference/companion_memory_module_design_provider_logging_config.md#section-09)100/10消息是示例，未批准值不填代码 | 目标选择、超长输入与token预算在对应任务前决定；冻结后不得扩展来源或改目标 |
| 使用反馈、生命周期和来源审查：待接口权限契约 | [系统§8.5、§11.4–11.5、P07](../reference/companion_memory_cognition_system_design_integrated.md#section-22)时效、修订匹配、删除竞态、外部完整来源未定；[架构M06/M08、T07](../reference/companion_memory_module_design_provider_logging_config.md#section-07)要求凭据与幂等 | 不从原文推导任意反馈期限或跨入口完整来源权限；20/35、增益8、30天均不作为默认值 |
| 当前状态、目标及通知时序：待定 | [系统P04/P05](../reference/companion_memory_cognition_system_design_integrated.md#section-22)时间格式、合并字段、提醒路由和跨期策略；[架构M09/M10](../reference/companion_memory_module_design_provider_logging_config.md#section-06)仅建议醒来合并当前过期提示 | 不批准补发策略，不自动延期或结束目标，不从历史输入重建当前状态 |
| 媒体有效性与文件管理参数：待定 | [系统§5、P06](../reference/companion_memory_cognition_system_design_integrated.md#section-22)空文本、部分结果与普通失败判定；[架构M04](../reference/companion_memory_module_design_provider_logging_config.md#section-06)字节hash、情境复用与阶段发布 | 保留完整规则；上传限制、GC保护、理解指纹等具体定义待实现任务批准 |
| persona监督与非专注发布：需细化 | [系统§12.4–12.5、P07、§16.9](../reference/companion_memory_cognition_system_design_integrated.md#section-12)候选审查／失败与非专注协调；[架构M07/M11、T09](../reference/companion_memory_module_design_provider_logging_config.md#section-07)原子发布和步骤检查点 | 首次摘要、变化限制、审查格式及非专注协调未实现；保留候选不合格不得发布等约束 |
| Provider未知结果、预算与保留：机制未批准 | [架构§4.2、§7.4](../reference/companion_memory_module_design_provider_logging_config.md#section-04)建议暂停待恢复决定；[§8.7–8.10、§15.2](../reference/companion_memory_module_design_provider_logging_config.md#section-08)未知费用、版本价格、准入和保留约束 | 不能记未知为零、自动退款或重放；真实协议兼容、费率、限额和保留值未核实 |
| 日志、配置、秘密与管理权限：工程细节未定 | [架构§10–11](../reference/companion_memory_module_design_provider_logging_config.md#section-10)日志有界背压、配置apply_mode与激活流程为草稿细化；[M12、§15.2](../reference/companion_memory_module_design_provider_logging_config.md#section-15)管理与工程批准要求 | Provider、日志、配置独立约束保留；不批准库、SSE、handler布局、真实Schema、秘密保管、保留值或恢复按钮权限 |
| 规划示例与实现表达：文档／代码职责互补 | [架构§9.1、§13](../reference/companion_memory_module_design_provider_logging_config.md#section-13)示意prompt与目录含模块编号和S段简称；[代码规范“禁止将规划信息写入实现”](../reference/CODING_STANDARDS.md#禁止将规划信息写入实现)禁止机械复制这些信息到实现 | 视图保留原示例用于阅读；未来须转换为语义化名称和独立说明，不能将示例当规范豁免 |
| 外部资料与历史依据：本轮核验有限 | [架构文档定位](../reference/companion_memory_module_design_provider_logging_config.md)、[参考依据§16](../reference/companion_memory_module_design_provider_logging_config.md#section-16)包含“用户明确给定”等来源描述与官方地址 | 仅作为当前提供材料中的陈述保留，不依赖其他会话；没有核验供应商新版本、SQLite版本、实际价格或外链可用性 |

发生新的真实矛盾时，应补充双方准确位置、原始措辞和影响，停止受影响操作；其余工作可以继续。本次不修改受保护原文以消除未定项。

## 实际文件范围与交付

新增45份Markdown文件：根AGENTS 1份、总索引1份、产品13份、架构11份、模块15份、工作记录4份。所有新增文件位于根AGENTS或docs下；没有修改任何原有文件。下面清单覆盖本次新增文件及用途。

| 文件 | 用途 |
| --- | --- |
| [docs/product/overview.md](../product/overview.md) | 角色、术语、信息对象与概念接口 |
| [docs/product/input-and-media.md](../product/input-and-media.md) | 原始输入、媒体理解与引用清理 |
| [docs/product/batches-and-learning.md](../product/batches-and-learning.md) | 入口队列、冻结批次、终结与学习 |
| [docs/product/provenance-and-memory.md](../product/provenance-and-memory.md) | 批次来源、正式记忆与关系 |
| [docs/product/lifecycle.md](../product/lifecycle.md) | 双指标、召回反馈、遗忘与删除 |
| [docs/product/self-and-persona.md](../product/self-and-persona.md) | 自我认知与稳定persona |
| [docs/product/current-state.md](../product/current-state.md) | 外部管理的当前状态与时间 |
| [docs/product/goals.md](../product/goals.md) | 多目标、去重与提醒 |
| [docs/product/retrieval.md](../product/retrieval.md) | 回复准备、记忆查询与入口隔离 |
| [docs/product/dream.md](../product/dream.md) | 梦境整理、专注隔离与入口回流 |
| [docs/product/operations-and-management.md](../product/operations-and-management.md) | 故障、审计与Web管理行为 |
| [docs/product/acceptance.md](../product/acceptance.md) | 产品验收场景完整表 |
| [docs/product/decisions-and-delivery.md](../product/decisions-and-delivery.md) | 待细化契约、参数性质与实施组织 |
| [docs/architecture/deployment-candidates.md](../architecture/deployment-candidates.md) | 工程约束、规模与部署存储候选 |
| [docs/architecture/request-paths.md](../architecture/request-paths.md) | 同步时限、数据所有权草图与外部接口 |
| [docs/architecture/persistence-and-transactions.md](../architecture/persistence-and-transactions.md) | 持久化、事务、检查点与恢复 |
| [docs/architecture/ownership.md](../architecture/ownership.md) | 十五个逻辑模块、基础设施与依赖 |
| [docs/architecture/provider.md](../architecture/provider.md) | Provider能力、调用控制、计量与安全 |
| [docs/architecture/context-and-cost.md](../architecture/context-and-cost.md) | 内部上下文、批处理与成本控制 |
| [docs/architecture/logging.md](../architecture/logging.md) | 日志等级、输出、审计隔离与背压 |
| [docs/architecture/configuration.md](../architecture/configuration.md) | 统一配置注册表、快照与安全热修改 |
| [docs/architecture/implementation-options.md](../architecture/implementation-options.md) | 代码组织建议与设计冻结点 |
| [docs/architecture/acceptance.md](../architecture/acceptance.md) | 工程验收与产品要求映射完整表 |
| [docs/architecture/references.md](../architecture/references.md) | 原文参考依据与资料标记 |
| [docs/modules/ingress.md](../modules/ingress.md) | 接入与入口注册模块入口 |
| [docs/modules/runtime.md](../modules/runtime.md) | 运行模式与工作调度模块入口 |
| [docs/modules/buffers.md](../modules/buffers.md) | 入口缓存与批次模块入口 |
| [docs/modules/media.md](../modules/media.md) | 媒体模块入口 |
| [docs/modules/cognition.md](../modules/cognition.md) | 认知加工模块入口 |
| [docs/modules/memory.md](../modules/memory.md) | 记忆、来源与关系模块入口 |
| [docs/modules/self-model.md](../modules/self-model.md) | 自我与persona模块入口 |
| [docs/modules/retrieval.md](../modules/retrieval.md) | 检索与信息提供模块入口 |
| [docs/modules/state.md](../modules/state.md) | 当前状态模块入口 |
| [docs/modules/goals.md](../modules/goals.md) | 多目标与意图模块入口 |
| [docs/modules/dream.md](../modules/dream.md) | 梦境整理模块入口 |
| [docs/modules/management.md](../modules/management.md) | Web与管理入口模块入口 |
| [docs/modules/provider.md](../modules/provider.md) | 模型Provider模块入口 |
| [docs/modules/logging.md](../modules/logging.md) | 日志与审计模块入口 |
| [docs/modules/configuration.md](../modules/configuration.md) | 统一配置模块入口 |
| [AGENTS.md](../../AGENTS.md) | 项目工作指引 |
| [docs/INDEX.md](../INDEX.md) | 文档总入口 |
| [docs/work/CURRENT_TASK.md](CURRENT_TASK.md) | 当前任务 |
| [docs/work/STATUS.md](STATUS.md) | 实际工作状态 |
| [docs/work/TASK_TEMPLATE.md](TASK_TEMPLATE.md) | 小任务模板 |
| [docs/work/ORGANIZATION_REPORT.md](ORGANIZATION_REPORT.md) | 源文件保护、逐节覆盖、校验与待确认记录 |

临时提取、内容比对和链接检查脚本仅位于系统临时目录，不作为产品工具写入仓库。由于新增文件未跟踪，普通Git diff不展示新增正文；实际审查结合工作区状态、新增路径清单与逐文件对空基线的差异，不把空diff误作已验证所有新增内容。

下一项仅建议 [统一配置参数定义与只读注册表](CURRENT_TASK.md)：显式参数元信息、重复键与未知键拒绝、查询结果不可修改。语言和范围待用户批准；不涉及完整配置、Provider、日志或持久化实现。本轮整理完成后停止。

<a id="closeout-review"></a>

## 收尾审查与本地基线范围

2026-09-09收尾审查起点为 `main`／`eb5ebcb`；48份文档未跟踪，工作区受跟踪差异与暂存区为空，没有需要隔离的无关暂存内容。根AGENTS生效且无override覆盖。现有目录结构、三份根原文和39份阅读视图保持不变，没有再次拆分、合并或生成另一套报告。

审查了根AGENTS、INDEX、CURRENT_TASK、STATUS和本报告；配置任务联合核对了配置模块完整入口、架构配置§11、依赖§5.3、配置事务T12以及对应根架构原文段落。原文继续承担权威职责，视图要求先改原文再同步；AGENTS和INDEX均明确按任务选读。文档中的建议端口、Python候选、生效分类建议和未定参数没有得到本次提交的批准。

本次仅修正CURRENT_TASK、STATUS和本报告：说明参数权限／生效方式／校验依赖仅作元信息；本地文档提交不授权代码任务或技术选型；区分首次整理与本次收尾的Git状态。配置任务的可验证结果仍为元信息查询、重复键与未知键拒绝、查询结果不可修改；完成后停止，不实现热修改、数据库、Web、Provider、日志或其他模块。

收尾验证：三份源文件SHA-256与上表基线一致；配置阅读视图及其依赖、事务正文与对应原文段落核对一致；仓库Markdown导航的相对文件路径及章节锚点检查无缺失，不依赖本机绝对路径。报告中旧检出目录和全局指引路径仅记录当时检查环境，不是导航入口。没有执行实现测试、构建、性能测试或官方外链验证。

暂存行尾空白检查报告6处警告：根架构原文L3、L4、L292，部署候选视图L15、L16，以及接入模块视图L35。逐处确认都是原有的两个行尾空格，用于Markdown硬换行；按原文和阅读视图保护范围保留，不把该检查称为无警告通过。本轮修改的三份工作记录没有行尾空白。暂存文件路径和字节已逐项核对，均属于文档基线。

提交范围固定为上方清单列出的45份整理文档，加根目录三份权威原文，共48个明确文件路径；不使用目录通配暂存或 `git add .`。提交前再次核对暂存路径与文件字节，出现无关暂存内容则停止提交，不撤销或混入。文档基线使用提交说明 `docs: establish project documentation baseline`；是否成功、准确标识及剩余工作区状态以实际Git结果和交付回执为准。此提交只记录文档，配置代码任务仍待批准。

<a id="configuration-contract-review"></a>

## 配置参数定义与只读注册表：契约设计与审查历史

以下记录文档设计与定点审查历史；当时的待批准状态不覆盖用户后续批准。有效契约为§11.9，当前执行状态以CURRENT_TASK.md为准，本节不继续追加代码实现记录。

2026-09-09，任务曾由用户明确调整为文档契约设计。开始于 `main / 1ce4cc2`，工作区和暂存区干净。此前配置最小实现任务的语言、范围及验收建议没有成为本次开发授权。

先核对了INDEX、CURRENT_TASK、STATUS和Git状态，再按配置路线读取对应权威原文、模块依赖、T12、相关工程验收、产品§23与代码规范。新增§11.9先写入架构原文，再完整同步到现有配置阅读视图。原§11.1–11.8及其他权威正文不改写，产品原文和代码规范保持原字节。新增实现决定D1–D7全部待批准；没有需要裁决的直接矛盾，未定类型、数值和行为没有被自动归并为既定要求。

本轮修改7份已有Markdown：架构原文、配置架构视图、配置模块入口、INDEX、CURRENT_TASK、STATUS和本报告；没有新增仓库文件。当前任务为“配置参数定义与只读注册表的详细契约设计”，状态为“详细契约待审查”。

本次定点澄清保留此前7份未提交改动，只修改权威契约、配置视图、CURRENT_TASK、STATUS和本报告；不新增报告或重整目录。原文明确排除默认自洽检查，故D2新增静态合法性要求作为待批准修订并列保留；其支持类型映射、空值／枚举相等／范围适用规则仍待批准。依赖关系环与循环值结构区分、冻结失败保持原集合并可继续构建，以及安全规则说明作澄清；五项接口、排序和重复冻结返回语义不变。

### 归并时留存的原文哈希（历史版本）

上方历史哈希继续标识最初整理基线。本表保留归并批准修订时记录的字节信息与SHA-256，不随当前文件修订更新，也不代表后续版本已重新核验。澄清前架构哈希为 `595b77c4acc53fbf9da9c8773b8e7e594fd81f3228e70d3b476d9c8349276679`，仅用于本次差异识别：

| 原文 | 当时字节数／行数 | 当时SHA-256 | 相对基线变化 |
| --- | --- | --- | --- |
| [系统原文](../reference/companion_memory_cognition_system_design_integrated.md) | 100296／1263 | `ce997355992b359fcad08c62284b206db2d64dad46a165a457cd801646a41f62` | 字节不变 |
| [架构原文](../reference/companion_memory_module_design_provider_logging_config.md) | 141270／1487 | `061c7e650c82d7b33ecb2bb5920004b52eed4b22c83f8c59eeac79ea770bf631` | 配置详细契约§11.9已归并批准修订 |
| [代码规范](../reference/CODING_STANDARDS.md) | 15538／235 | `be6a5909e936e4fab27d5343f67d81c774caedd150260095d09b399cfbc1af18` | 字节不变 |

### 增量覆盖映射

归并时新增连续正文为架构L951–L1150，共200行，完整归入原有`architecture/configuration.md`，没有新增另一套独立需求文件。原文与视图使用相同语义锚点；本表行号对应上方历史架构哈希，不代表后续文件行号。

| 当时原文章节及行号 | 完整阅读视图 |
| --- | --- |
| [L953 · 11.9 配置参数定义与只读注册表契约](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-contract) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-contract) |
| [L959 · 11.9.1 依据、确定程度与范围](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-evidence) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-evidence) |
| [L973 · 11.9.2 数据定义](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-data) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-data) |
| [L1023 · 11.9.3 校验与依赖边界](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-validation) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-validation) |
| [L1050 · 11.9.4 注册、所有权与冻结](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-lifecycle) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-lifecycle) |
| [L1072 · 11.9.5 公开接口](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-ports) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-ports) |
| [L1088 · 11.9.6 错误契约](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-errors) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-errors) |
| [L1109 · 11.9.7 验收例子](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-examples) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-examples) |
| [L1143 · 11.9.8 批准范围与停止点](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-decisions) | [配置契约完整正文](../architecture/configuration.md#configuration-registry-decisions) |

历史覆盖表和其他阅读视图中的`source-line-*`继续指向原始基线位置；架构原文原L1–L950仍在原位，原L951及之后均后移200行，原章节内容不变。例如原第12节标题L953现为L1153，原第16节标题L1239现为L1439。因此不批量改写历史锚点或无关视图；原映射加本增量表记录当时的完整覆盖。

### 文档检查与限制

此前详细契约草稿的实际检查结果（定点澄清前）：

- 对比Git基线字节，去除新增§11.9后架构原文与HEAD完全一致；产品原文、代码规范字节不变。新增完整正文与配置阅读视图一致，比较只忽略段落末尾用于文件结尾的空行；配置视图原有正文与HEAD一致。
- 原§11.2的24项元信息全部出现在详细定义中；补充的理由、消费者、验证方式字段形状及全部新增类型／行为明确标为提案，D1–D7在原文与当前任务中逐项待批准。人工核对了依据分类、五项能力、六类错误、生命周期和验收例子的对应关系。
- 本地解析检查48份Markdown、1967处相对链接，没有缺失目标文件／锚点，没有重复显式锚点。新增9项章节映射与实际标题、行号一致；哈希按当前文件原始字节重新计算。
- 首次`git diff --check`发现配置视图文件尾部新增空行，已修正；最终该检查无警告。修改集合精确为上述7份文档，暂存区为空，没有新增仓库文件，HEAD仍为 `1ce4cc2`。
- 检查使用本机已有Python临时处理文档文本与Git差异，临时脚本位于系统临时目录；这不选择项目Python技术栈，也没有建立项目测试工具链或安装依赖。

本次定点澄清后复核：相对本轮起点仅上述5份既有文档变化，INDEX与配置模块入口字节未动；原文改动限于§11.9，原排除条款及旧例子保留，D2修订并列待批准。五项接口正文与六类错误名称不变，当前哈希和9项章节行号一致；48份文档、1967处相对链接再次检查无缺失文件／锚点及重复显式锚点，`git diff --check`无警告。所有既有未提交改动保留，没有新增仓库文件或暂存内容。

注册表行为例子尚未实现或执行，不将它们标为测试通过；也不宣称V73/V74、配置激活或T12已经完成。没有图形渲染、外链联网核验、业务测试、构建或性能结果；相对链接检查使用本地解析与常见标题锚点规则，不承诺每个Markdown客户端的渲染表现。

### 历史待批准状态（已被后续审批替代）

具体待批准决定见[权威D1–D7表](../reference/companion_memory_module_design_provider_logging_config.md#configuration-registry-decisions)：数据表示、默认／校验深度、键与重复、深不可变、冻结与依赖、公开端口和错误形式。语言、运行库、真实参数默认值和技术栈继续未定。本次只有设计授权，不执行代码、迁移、依赖安装、提交、推送或部署；完成文档检查并报告后停止。

后续审批记录：用户已批准契约及静态校验修订，随后明确Python 3.12和uv管理。旧的默认自洽排除条款及相关例子已移出有效正文，仅在上述历史记录保留。之后用户暂停实现并要求删除源码；当前任务和后续验证由CURRENT_TASK.md记录，切片完成节点简短更新STATUS.md，本报告不继续补记实现结果。
