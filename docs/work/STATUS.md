# 实际工作状态

2026-09-27：初始化完整重构已本地交付；基础资料可默认，只填两组Provider API key即可建立本地角色与工作区，不再要求费率、预算或首次模型审核。8080已更新并保留管理员、会话、原草稿及实例身份；真实用户连接仍待API key，真实供应商与模型效果未验证。本次改动已本地提交，版本化验证、初轮失败及限制见[CURRENT_TASK](CURRENT_TASK.md)。

本页只记录已交付能力、对应版本和仍有效的限制。当前实施授权及停止点见[CURRENT_TASK](CURRENT_TASK.md)，后续顺序见[剩余路线](../architecture/implementation-options.md#source-line-1202)。

## 已交付能力与版本

下表结果只对应所列版本。历史条目由当时执行者运行、监督者复核；2026-09-26条目由获授权总控与子代理执行和审查。早期内部组件合并记入所属完整能力，详细过程通过Git读取。

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
| 梦境与长期维护：原生调度、来源影响、时间衰减／到期删除、周期persona、管理控制及回流恢复 | `e37e2f7bb467246d4d34ea7a43a75ca5df991f00` | Docker Linux arm64 37项具名检查按版本对应通过；最终Pyright867份Python零诊断；876份工程指纹一致 |
| 受保护引导、本地管理员／独立宿主令牌、管理Web／独立审计、配置激活回退、一致备份恢复及兼容升级 | 管理交付提交（父`e37e2f7`，精确版本见本页提交记录） | 973份工程指纹核对；本轮8项定点通过，关联结果及同版本复跑分别记录；锁定全量Pyright零诊断，实际浏览器及前轮版本化证据复用 |
| Core外部通信：HTTP媒体／协议客户端、WS／ACK／可信路由与恢复、Web外部连接／配置续办、TLS代理及显式托管升级 | 源码`1f2cc1e`／文档`9d483c7`；原验收集合指纹见下文 | 75项托管、13项关联、3项最终浏览器；锁定全量Pyright及前端类型／构建通过，30分钟混合负载；四项修复与前轮证据按版本核对 |
| 新实例最小向导、安全默认草稿、持久待配置与原键续办、零入口／后登记、旧管理绑定及页面等待保护 | main@`523c1d9`上的未提交工作树；1044份工程集合`e641dca3174adc6da3304e036a14a5e1dc08115fb8ec08930d48b8fc9f58c754` | 37项定点／关联、全量Pyright零诊断；后端指纹对应及最终Web两项类型／构建、实际浏览器，详细限制见下 |

2026-09-27：[完整初始化重构](../product/operations-and-management.md#source-line-986)已本地交付。两组直接密钥、默认资料、普通USAGE_ONLY、本地角色原子发布及工作区、后续双密钥激活、专用私有卷和未开始业务的显式格式升级均完成。最终1067份工程集合`7e2cd5349f3fa8ef5f1b72113ac2052c0847dfaa5775aecc1dfaba89109ace83`；1022份Python全量Pyright零诊断，前端类型／构建及Compose静态合并通过。首轮75项有2失败，修订后12项角色／恢复、9项诊断及1项Provider版本分别通过，未变范围依精确指纹复用；不声称单次最终全套或并发负载全绿。实际浏览器创建到READY、状态／目标、双密钥激活及重启验证，QA请求0。8080最终镜像`f7659150f8a5…`，停机备份及原身份／草稿摘要核对一致；用户真实配置仍待两组key。原始命令、退出码、各版指纹、失败及前轮独有记录保全见[交付清单](C:/Users/leaf/AppData/Local/Temp/iris-onboarding-refactor-20260926-194034/delivery-manifest.json)。已本地提交，未正式部署。

2026-09-26：针对本地8080待配置错误缺少字段位置的反馈，已补有界原生字段诊断、中文定位与完成前校验、非空白生成／监管材料门控，以及待配置健康探针映射。最终1046份工程集合`be68c1644fc5f9e5272f310f4a1f5d3fb442a70afa68121df524057b8812b483`；19项关联结果按版本核对，最终9项定点、1003份Python全量Pyright零诊断、前端类型／构建通过，实际浏览器确认缺失提示与定位。新版已更新本地8080，原管理员和草稿修订2／内容指纹保留，业务仍待真实配置且发送暂停；真实请求0。原始失败、修复、命令退出码、指纹及备份见[本轮证据](C:/Users/leaf/AppData/Local/Temp/iris-configuration-diagnostic-20260926-141857/delivery-manifest.json)。未跑完整Playwright、Pylance、全仓unittest或真实供应商；未提交或正式部署。

2026-09-26：[新实例初始化](../architecture/managed-runtime-and-deployment.md#assembly)的批准本地工程范围通过技术验收。[交付清单](C:/Users/leaf/AppData/Local/Temp/iris-approved-delivery-20260926-083746/delivery-manifest.json)保存起点21份已有工作、实现／审查、命令原始日志及最终1044份工程指纹。Docker Linux amd64最终37项通过（590.353秒），锁定Pyright1.1.413覆盖1001份Python零诊断；最后两Web文件调整后，1026份后端输入完全一致，最终前端类型／构建重新通过。实际浏览器覆盖同版本续办、导入／配置门槛、零／多入口和旧格式管理，2次回环模拟、真实请求0；中间失败原样保留。未跑全仓unittest、完整Playwright脚本、Pylance或当前arm64；不外推生产、质量和长期性能。跨预览构建未决隐式默认保存不作为已验证兼容范围，旧预览未切换。本轮未提交、推送或部署。

2026-09-18：[外部通信](../architecture/external-communication.md)完整工程范围通过最终监督技术验收。[最终清单](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-communication-repair-ngfuvuwq/manifest.json)的1092份文件、154份证据和46份命令日志摘要一致；[监督验收](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-communication-repair-review-_7qauid1/review.json)保存1033份工程文件集合指纹`b2ecf46968922eb6d70318b59224409b7a5c324b3c92ef59082b5d88d9571565`。WAL陈旧候选拒绝、控制帧有界背压、授权路由持久终态分页及Web原激活续办均复验关闭，未发现新的阻塞问题；前轮完整审查及未受影响证据按版本复用，历史失败保留。75项托管、13项关联和3项浏览器通过；托管回归期间仅两份浏览器文件变化，最终浏览器已覆盖，运行源码不变。Pyright1.1.413全量零诊断，前端类型／构建通过；未重跑全仓unittest，Pylance未验证。监督只做静态和证据审查，真实供应商请求0；仍未提交或实切生产。

2026-09-17：[可部署、可管理的首期产品](../architecture/managed-runtime-and-deployment.md)通过整阶段工程技术验收，集中复核未发现新的阻塞问题。[最新交付](/private/tmp/iris-audit-mode-5h29kbzi/execution-index.json)的1031份快照及1101份证据摘要一致；[监督验收](/private/tmp/iris-managed-acceptance-qgz7gulo/review.json)对应文档收尾后973份工程指纹`5a54955232f54386fc08633e0feaec330f8d1da64a1b6a035692d34f847545a3`。本轮8项定点通过，关联28项中27通过、1项ADMISSION_BUSY失败保留，同版本独立复跑2项通过；不据此证明并行负载稳定或根因已排除。最终锁定Pyright1.1.413全量零诊断，真实浏览器覆盖专注审计、390／600像素及键盘；未变前端构建／类型检查及原十组验收按版本复用，未重跑全量unittest。模拟适配器与合成材料，真实请求0，Pylance未验证；本阶段已提交，未实切生产。

2026-09-17：[梦境与长期维护](../architecture/dream-and-long-term-maintenance.md)工程范围通过监督技术验收，两项阻塞及合法触发积压关联问题已修复，集中复核未发现新的阻塞问题。[执行证据](/private/tmp/iris-dream-repair-ydi3692c/index.json)与[监督核对](/private/tmp/iris-dream-acceptance-70r3s6oi/review.json)对应876份工程指纹`a77fe11f9cf1c538f7f81702a69bd5ce89a977fe61b788d1c8af8eadb55dd59f`。37项具名成功按[版本对应](/private/tmp/iris-dream-repair-ydi3692c/version-reuse.json)解释，中间失败保留；原生两轮persona的6次回环请求间隔均不少于30秒，1002输入FIFO及四种新进程恢复通过。最后锁定Pyright1.1.413覆盖867份Python零诊断；未重跑全量unittest，Pylance未验证。历史11次真实发送、21个停止槽及未完成真实用途单列于[限制记录](DEFERRED_ISSUES.md#dream-real-validation)，本次新增真实请求0。

2026-09-16：[日常认知与图片学习](../architecture/daily-cognition-and-image-learning.md)工程范围通过监督技术验收。统一宿主、六类候选、SUBJECT来源、目标合并、persona导入、图片／语义联动及可信初始化续办已交付；本轮静态复核未发现新的阻塞工程问题。[监督核对](/var/folders/vr/xq2gyj_j1w5f2rbw5h06rtqw0000gn/T/iris-daily-stage-acceptance-y3y63i5a/review.json)与[执行证据](/private/tmp/iris-daily-rerun-aczy2fsy/handoff-index.json)对应844份受测文件指纹`670ec29e674e0c1cea8d3e1551fc2a5b65c15bb5e4a67d4785ec6bab4e58680a`；文档收尾保留788份工程文件。Docker Linux arm64本轮60个具名成功检查，15组矩阵按版本复用；最终锁定Pyright1.1.413覆盖776份Python零诊断，未重跑全量unittest。新包3次实际发送、学习无有效应用、后续29槽停止，旧3次失败保留；[真实结果与未验证范围](DEFERRED_ISSUES.md#daily-cognition-real-output)不写成真实闭环或质量通过。Pylance未验证。

2026-09-15：[语义检索小档工程与真实闭环](../architecture/async-embedding-semantic-retrieval.md)在已批准范围内通过监督技术验收并提交。版本为语义提交`c7bf865b20a67b3fc373839be7cd00ec6d0855c3`的672文件指纹`bde65f12bc611d56bb85d62020fe85520e14396684b811f08c5e968b31eaa348`；此前受控核心证据复用，本轮用量配置／v4账本／v2usage、UNKNOWN门控、原生交接及共享传输增量静态核对未发现新的阻塞问题，依据见[监督核对](/private/tmp/iris-semantic-stage-acceptance-63h1n5qc/review.json)。执行者29项定点／关联测试通过，锁定Pyright1.1.413全量零诊断；未重跑全量unittest。

真实12 DOCUMENT＋6 QUERY均HTTP200、原生提交并完成实际清理，报告1134 tokens，费用与供应商实际扣额未知；18个原槽已全部消耗。原进程后的新解释器恢复、原键及缓存复用新增发送0，见[逐项核账](/private/tmp/iris-semantic-allocated-ryg1zesg/send-accounting.json)。首次EXECUTE因HTTP验证使用不同principal在18次成功后退出1，原失败保留；只修正验证工具身份后RECOVER退出0，生产源码从发送到恢复不变，两个验证文件的版本差异另见[版本对应](/private/tmp/iris-semantic-allocated-ryg1zesg/code-version-binding.json)。[相关性质量未达标，用户已决定暂缓并继续推进](DEFERRED_ISSUES.md#semantic-retrieval-quality)，不写成完整效果资格通过。Pylance、4096完整大档、真实冷查询250ms成功率及生产条件未验证。

## 当前限制与验证边界

- 外部通信30分钟负载在4 CPU／4GiB、正常后台调度下，3582次查询中1782次一秒内成功（49.75%）；60次上传最终确认、120次发送且无重发，90个持久ACK／30个UNKNOWN。客户端89个确认及1个UNCONFIRMED均按delivery_id核对，后者不计作客户端确认。准入／后台争用及原错误分布保留；本轮与其他验证有重叠，不据此宣称较前轮48.16%改善、生产性能或长期稳定性通过。新格式仅支持精确托管前身显式升级，fencing后向前恢复，旧备份不能无损回退；Web原操作续办不覆盖跨设备或已清除会话存储。
- DeepSeek固定14槽已执行，两平台persona已审核发布，正式记忆分别5／12条，恢复零新增发送；这不证明学习质量达标。旧MiniMax UNKNOWN及外层保守责任仍保留，不能视作结清或自动续用额度。原请求、标注及费用估算证据保持原样。
- 新托管装配已交付受控配置编辑、版本激活／回退及Web首次时区确认／修改，旧格式仍遵守各自能力边界。兼容构建切换保留新增数据，不等于任意存量迁移；持久FAULTED强制解除及UNKNOWN人工结案未开放。
- 语义检索小档工程及真实闭环已技术验收，相关性问题暂缓；[日常认知与图片学习](../architecture/daily-cognition-and-image-learning.md)工程已交付，实际图片成功但质量有偏差，真实学习／工具及本宿主真实向量闭环未完成。周期梦境／persona与自动长期维护工程已交付，真实效果与未执行用途见[梦境限制](DEFERRED_ISSUES.md#dream-real-validation)；[音视频测试暂缓](DEFERRED_ISSUES.md#audio-video-validation)。既有模拟／合成参与者不代表这些完整能力。
- 本地身份／鉴权、批准的容器持久目录、管理Web、备份恢复及兼容升级已完成工程验证；依据为Docker Linux arm64的实际临时文件／SQLite、受控HTTP、真实浏览器、故障注入和新进程恢复，尚未正式生产切换。第二架构、最大合法词项／大档资格、P/X及长期负载、介质掉电保障仍未验证；不能把单次并行准入拒绝的复跑通过外推为负载稳定。
- 永久阻塞的底层资源在有界返回后仍保留实际占用，清理状态与提交／远程结果分开表达。历史环境不能证明新环境就绪；后续检查范围只按[统一验证规则](../CODING_STANDARDS.md#validation-environment)。Pylance未验证。

## 历史定位

使用`git show <上表提交>:docs/work/CURRENT_TASK.md`读取对应交付的命令、原始证据路径、文件指纹与限制；需要完整累计历史时读取`9936385d8697b7add662562c3465c041a48d76fa:docs/work/STATUS.md`。一次性文档整理过程只保留在该提交的`docs/work/ORGANIZATION_REPORT.md`，不再作为工作区文档或待定事项来源。未提交的当前计划及独有证据保持保全。
