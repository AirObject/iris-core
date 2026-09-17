# 实际工作状态

最新工程技术验收为2026-09-17的梦境与长期维护；本次提交以`abd7717393b713d4369f1b3ac4d5245d08dea0e3`为父提交，标题`feat(dream): 实现梦境调度、长期维护与周期persona`，实际提交通过Git定位。真实两轮persona已发布；学习权限拒绝后持久停发，其他真实用途未全部执行，详见[独立限制](DEFERRED_ISSUES.md#dream-real-validation)。不宣称供应商质量通过，未推送。

本页只记录已交付能力、对应版本和仍有效的限制。当前实施授权及停止点见[CURRENT_TASK](CURRENT_TASK.md)，后续顺序见[剩余路线](../architecture/implementation-options.md#source-line-1202)。

## 已交付能力与版本

下表测试均由当时执行者实际运行，结果只对应所列版本；监督者复核源码、Git及证据，没有亲自运行这些测试。早期内部组件合并记入所属完整能力，详细过程通过Git读取。

| 能力及验收范围 | 提交 | 对应验证依据 |
| --- | --- | --- |
| 配置注册表、显式解析与不可变快照、日志纯内存校验 | `b94cd5070b9da8c2683e0f7632c248175095acf2` | 139项unittest；全量Pyright零诊断。后续持久化／Provider及各装配配置在对应交付中验收 |
| 控制台与文件运行诊断，双端有界队列、截点、轮转、保留与清理所有权 | `fdbf7a99a307b39b15764a6622b64af9a095b1e2` | 320项unittest；全量Pyright零诊断，实际自有临时文件验证 |
| 持久化事务、幂等原键恢复及同事务审计 | `6a99353c6035629bcff866e8367d6fd69fa93113` | 412项unittest；全量Pyright零诊断，真实SQLite及独立进程恢复 |
| Provider基础服务与模拟适配器，调用／尝试／费用／预算及结果交接账本 | `128f908e8d780da647949dfe3f6c21980b31fcf2` | 479项unittest；全量Pyright零诊断，模型模拟、SQLite账本实际运行 |
| 持久接入、H/T/R冻结与三终态、门控／回流、持久配置身份及最小只读HTTP | `6a41859fdfc8c0f80305e792221e19fdab2a9fa2` | 569项unittest；全量Pyright零诊断，187份受测文件核对一致 |
| 正式记忆、完整来源与真实媒体文件，原子终结、受控读取、释放／GC及恢复 | `f89cbbe773b119a7e683367410f17eda4538739a` | 680项unittest；全量Pyright零诊断，377份受测文件核对一致；模型及认知候选仍为模拟／合成 |
| 本地词法信息获取、使用反馈、外部状态、多目标及提醒响应 | `8bc948c1822afb931625b77563df710fc9bbf20d` | 按批准后的[F口径](../architecture/local-information-feedback.md#f-qualification)技术验收；两平台恢复及用户确认73组相关性，445份原受测集合。完整版本链见该提交的CURRENT_TASK |
| 文本学习独立宿主、持久候选、记忆／来源原子终结、首次persona审核发布 | `c2be9327b80be3b3ccb12262af7ff15c4539ea2d` | macOS／Linux各890项unittest；全量Pyright零诊断，542份受测文件核对一致。此版本证明工程及本地验证，真实供应商试验见下一行 |
| MiniMax／DeepSeek真实文本协议、受控登记／凭据／试验工具与恢复；工程已交付，质量暂缓 | `9936385d8697b7add662562c3465c041a48d76fa` | 最终macOS／Linux各17项关联回归；锁定Pyright 1.1.413全量零诊断，578份文件／570份Python。真实请求按[原报告](/private/tmp/iris-deepseek-live-jve6u6qk/continuation/report-complete.md)分版本解释，未重跑全量unittest |
| 异步embedding、两代语义索引、混合查询／HTTP、固定来源与原键恢复；真实小包完成，质量暂缓 | `c7bf865b20a67b3fc373839be7cd00ec6d0855c3` | Docker Linux arm64 29项定点／关联通过，锁定全量Pyright零诊断；672份受测文件与提交树一致；12＋6真实调用，恢复／缓存新增发送0 |
| 日常认知统一宿主、图片准备、有界工具、六类候选、SUBJECT来源、目标合并、persona导入与初始化续办；工程验收，真实验证有限 | `abd7717393b713d4369f1b3ac4d5245d08dea0e3` | 844份受测文件；本轮60个具名成功检查按版本复用，最终Pyright776文件零诊断；真实图片2成功、学习1失败后停止 |
| 梦境与长期维护：原生调度、来源影响、时间衰减／到期删除、周期persona、管理控制及回流恢复 | 本次提交（父提交`abd7717393b713d4369f1b3ac4d5245d08dea0e3`） | Docker Linux arm64 37项具名检查按版本对应通过；最终Pyright867份Python零诊断；876份工程指纹一致 |

2026-09-17：[梦境与长期维护](../architecture/dream-and-long-term-maintenance.md)工程范围通过监督技术验收，两项阻塞及合法触发积压关联问题已修复，集中复核未发现新的阻塞问题。[执行证据](/private/tmp/iris-dream-repair-ydi3692c/index.json)与[监督核对](/private/tmp/iris-dream-acceptance-70r3s6oi/review.json)对应876份工程指纹`a77fe11f9cf1c538f7f81702a69bd5ce89a977fe61b788d1c8af8eadb55dd59f`。37项具名成功按[版本对应](/private/tmp/iris-dream-repair-ydi3692c/version-reuse.json)解释，中间失败保留；原生两轮persona的6次回环请求间隔均不少于30秒，1002输入FIFO及四种新进程恢复通过。最后锁定Pyright1.1.413覆盖867份Python零诊断；未重跑全量unittest，Pylance未验证。历史11次真实发送、21个停止槽及未完成真实用途单列于[限制记录](DEFERRED_ISSUES.md#dream-real-validation)，本次新增真实请求0。

2026-09-16：[日常认知与图片学习](../architecture/daily-cognition-and-image-learning.md)工程范围通过监督技术验收。统一宿主、六类候选、SUBJECT来源、目标合并、persona导入、图片／语义联动及可信初始化续办已交付；本轮静态复核未发现新的阻塞工程问题。[监督核对](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-daily-stage-acceptance-y3y63i5a/review.json)与[执行证据](/private/tmp/iris-daily-rerun-aczy2fsy/handoff-index.json)对应844份受测文件指纹`670ec29e674e0c1cea8d3e1551fc2a5b65c15bb5e4a67d4785ec6bab4e58680a`；文档收尾保留788份工程文件。Docker Linux arm64本轮60个具名成功检查，15组矩阵按版本复用；最终锁定Pyright1.1.413覆盖776份Python零诊断，未重跑全量unittest。新包3次实际发送、学习无有效应用、后续29槽停止，旧3次失败保留；[真实结果与未验证范围](DEFERRED_ISSUES.md#daily-cognition-real-output)不写成真实闭环或质量通过。Pylance未验证。

2026-09-15：[语义检索小档工程与真实闭环](../architecture/async-embedding-semantic-retrieval.md)在已批准范围内通过监督技术验收并提交。版本为语义提交`c7bf865b20a67b3fc373839be7cd00ec6d0855c3`的672文件指纹`bde65f12bc611d56bb85d62020fe85520e14396684b811f08c5e968b31eaa348`；此前受控核心证据复用，本轮用量配置／v4账本／v2usage、UNKNOWN门控、原生交接及共享传输增量静态核对未发现新的阻塞问题，依据见[监督核对](/private/tmp/iris-semantic-stage-acceptance-63h1n5qc/review.json)。执行者29项定点／关联测试通过，锁定Pyright1.1.413全量零诊断；未重跑全量unittest。

真实12 DOCUMENT＋6 QUERY均HTTP200、原生提交并完成实际清理，报告1134 tokens，费用与供应商实际扣额未知；18个原槽已全部消耗。原进程后的新解释器恢复、原键及缓存复用新增发送0，见[逐项核账](/private/tmp/iris-semantic-allocated-ryg1zesg/send-accounting.json)。首次EXECUTE因HTTP验证使用不同principal在18次成功后退出1，原失败保留；只修正验证工具身份后RECOVER退出0，生产源码从发送到恢复不变，两个验证文件的版本差异另见[版本对应](/private/tmp/iris-semantic-allocated-ryg1zesg/code-version-binding.json)。[相关性质量未达标，用户已决定暂缓并继续推进](DEFERRED_ISSUES.md#semantic-retrieval-quality)，不写成完整效果资格通过。Pylance、4096完整大档、真实冷查询250ms成功率及生产条件未验证。

## 当前限制与验证边界

- DeepSeek固定14槽已执行，两平台persona已审核发布，正式记忆分别5／12条，恢复零新增发送；这不证明学习质量达标。旧MiniMax UNKNOWN及外层保守责任仍保留，不能视作结清或自动续用额度。原请求、标注及费用估算证据保持原样。
- 配置支持已批准装配的一次初始化与持久恢复；在线编辑、安全热激活／回退、存量迁移、持久FAULTED解除及UNKNOWN人工结案未交付。[默认时区](../product/operations-and-management.md#initial-default-timezone)已在日常宿主的可信配置准备中实现，Web首次确认／修改尚未实现。
- 语义检索小档工程及真实闭环已技术验收，相关性问题暂缓；[日常认知与图片学习](../architecture/daily-cognition-and-image-learning.md)工程已交付，实际图片成功但质量有偏差，真实学习／工具及本宿主真实向量闭环未完成。周期梦境／persona与自动长期维护工程已交付，真实效果与未执行用途见[梦境限制](DEFERRED_ISSUES.md#dream-real-validation)；[音视频测试暂缓](DEFERRED_ISSUES.md#audio-video-validation)。既有模拟／合成参与者不代表这些完整能力。
- 实际临时文件／SQLite、受控HTTP、故障注入及新进程恢复支持各自断言；后续已包含Docker Linux验证，不能继续沿用早期“Linux未测试”的全局描述，也不能据此声称生产部署完成。生产身份／鉴权、目录与G2、完整管理Web、备份升级、最大合法词项资格、P/X及长期负载、介质掉电保障仍未验证或交付。
- 永久阻塞的底层资源在有界返回后仍保留实际占用，清理状态与提交／远程结果分开表达。历史环境不能证明新环境就绪；后续检查范围只按[统一验证规则](../CODING_STANDARDS.md#validation-environment)。Pylance未验证。

## 历史定位

使用`git show <上表提交>:docs/work/CURRENT_TASK.md`读取对应交付的命令、原始证据路径、文件指纹与限制；需要完整累计历史时读取`9936385d8697b7add662562c3465c041a48d76fa:docs/work/STATUS.md`。一次性文档整理过程只保留在该提交的`docs/work/ORGANIZATION_REPORT.md`，不再作为工作区文档或待定事项来源。未提交的当前计划及独有证据保持保全。
