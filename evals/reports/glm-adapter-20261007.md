# GLM 网关适配与验证

适配、离线测试及三项真实验证完成。learning_v3 的 15 个批次全部成功，学习调用 P95 为 14.09 秒；E001／E011 对应记忆的时序观察最长分别为 5.397／7.804 秒。全程没有 429 或超时，没有调用判分模型。本报告不作学习质量或 M1 门槛结论。

## 来源与范围

基于 `origin/main` 的 `f70728621c2a375cd1933dda2d6733cbf5843ff1`，已包含 PR #9（调度）、#10、#11，以及 DECISIONS.md 2026-10-06「GLM 默认推理档位为 low」。实施前阅读 AGENTS.md、2026-10-05／10-06 各项决定、两份 GLM 探测报告的适配建议和现有错误分类。用户补好配置后再次 `git fetch origin`，origin/main 仍为上述提交，无需 merge；两套已通过的全量测试对应本次真实验证所用的相同产品代码。

分支 `feat/glm-adapter`，独立 worktree `iris-core-glm-adapt`。本轮仅修改模型网关、模型健康指纹、诊断状态、CLI 的 models check、学习／端到端评测的来源与报告、迁移 006、相关测试和文档。未改 learning.py、api.py、memory_ops.py、提示词、评分说明或公开语料；未寻找或读取隐藏集。

## 配置和诊断

在已有配置的 `[chat]` 组增加：

```toml
reasoning_effort = "low"
```

字段可选；仅接受非空字符串，不限定服务商枚举。未配置时不发送参数；embedding 请求不发送。普通生成、JSON 修正、健康探测使用同一项配置，不按主机或模型名字自动猜测。用户的真实配置只由项目加载器读取，没有修改或复制。

`models check` 在连接测试前显示档位，连接失败也能看到。学习检查点与端到端运行指纹包含档位，报告及外部材料的运行来源记录 `chat_reasoning_effort`。改变档位不能复用之前的学习检查点，也触发用途健康状态的配置变化；未配置时保留旧健康指纹，避免升级清除已有暂停。历史判分材料继续可读，缺少档位时不追填 low。

迁移 `006_glm_diagnostics.sql` 增加：

| 字段 | 含义 |
| --- | --- |
| reasoning_effort | 本次对话 HTTP 尝试实际采用的配置档位；未配置／embedding 为 NULL |
| reasoning_present | 成功解析响应后是否存在 message.reasoning_content 或 message.reasoning；HTTP 错误与旧记录未知 |
| reasoning_chars | 已返回推理字符串的 Unicode 字符数；无字段为 0，非字符串字段为 NULL |
| reasoning_tokens（原有） | usage.completion_tokens_details.reasoning_tokens；缺失为 NULL |

只用 `message.content` 解析正文。推理文本不写入调用记录、学习原始正文或修正请求；不从推理字段补正文。completion_tokens 已包含推理，不能再加 reasoning_tokens。用量合计仅覆盖已知部分，未报告用量不是零消耗。

## 错误分类

优先识别已知 `error.code`，再兼容仅提供已知 `error.type` 的其他服务商，最后按 HTTP 状态兜底。code／message／type／param 为空字符串时可正常兜底，不依赖服务商消息文案；错误消息不原样写入记录。

| 信号 | 分类与处理 |
| --- | --- |
| SensitiveContentDetected 及点号细分；InputTextSensitiveContentDetected、OutputTextSensitiveContentDetected；InputTextRiskDetection、OutputTextRiskDetection | content_rejection：沿用 refused 终态；无 JSON 修正，无记忆写入 |
| 成功响应 finish_reason=content_filter，包括带部分正文 | 同上 |
| InvalidParameter、MissingParameter、InvalidEndpoint 等；HTTP 404 兜底 | configuration；暂停至配置变化 |
| AuthenticationError；HTTP 401 兜底 | authentication／invalid_key；暂停至配置变化 |
| InvalidSubscription、InvalidAccountStatus、AccountOverdueError、OperationDenied、AccessDenied、QuotaExceeded、SetLimitExceeded 等；HTTP 402／403／423 兜底 | account／account_problem；无调用内短间隔重试，沿用账户恢复探测 |
| AccountRateLimitExceeded、RPM／TPM 限流、并发上限、ServerOverloaded、RequestBurstTooFast；HTTP 408／429／5xx 兜底 | retryable；受总预算和熔断约束 |
| ContentSecurityDetectionError、InternalServiceError、传输错误、超时 | retryable；审核服务故障不算内容违规 |
| MiniMax input_sensitive／output_sensitive／base_resp | 保留现有识别及可用正文规则 |

方舟的 QuotaExceeded 在其他排队 API 中也可能表示队列已满。本网关调用同步 chat／embeddings，按本任务约定保守归账户额度问题，不按消息文字猜测并反复重试。

没有有效 Retry-After 时，两次调用内重试分别等待 `2 + U(0,2)`、`4 + U(0,4)` 秒；有效秒数或 HTTP 日期直接决定等待，不再套旧固定延迟。非有限数值和无效日期回退到抖动退避。调用内最多三次 HTTP 尝试，健康状态可能提前暂停；首次学习、重试及 JSON 修正仍共享 180 秒截止时间。等待若会耗尽剩余预算，则结束本次调用，不延长预算。

依据：[方舟错误码](https://docs.volcengine.com/docs/ark/error-codes?lang=zh)、[深度思考](https://docs.volcengine.com/docs/ark/deep-thinking?lang=zh)、[Chat API](https://docs.volcengine.com/docs/ark/chat-api?lang=zh&redirect=1)。2026-10-07 复核官方检索结果；动态页面直接打开时部分正文不可见。具体 GLM 字段及 token 语义也沿用已提交的 PR #8／#11 实测依据。代码注释注明错误码表来源。

## 离线验证

先写本地假 HTTP 服务测试，在原实现得到 18 项预期失败、1 项原有 content_filter 行为通过，再实现适配。随后补充未配置档位保留旧健康指纹的失败测试并修复。测试包括空错误字段、码优先于 HTTP、拒绝终态与零写入、JSON 修正不读取推理、缺失 usage、参数发送／省略、健康探测、指纹变化与复用隔离、CLI、迁移备份及旧判分材料兼容。

原分类测试继续覆盖 MiniMax、通用 HTTP 和传输错误；只按新要求修改原先 403=密钥无效的期望，以及固定退避的期望。测试注入确定性抖动，不依赖随机时长。

| 检查 | 结果 |
| --- | --- |
| uv run pytest；Python 3.13.15 | 436 passed，64.60 秒 |
| uv run --locked --isolated --python 3.12 pytest；Python 3.12.14 | 436 passed，66.80 秒 |
| uv build | wheel、源码包成功；wheel 包含迁移 006，无真实配置文件 |

两套测试顺序运行，worktree 的 `.venv` 仍为 Python 3.13。现有依赖警告：Starlette 对 httpx TestClient 的弃用提醒，Python 3.12 另有 jieba 的转义警告。仅在 macOS 验证。

## 真实验证

用户补好配置后通过项目加载器确认 low。三个作业顺序执行，源码和迁移在真实运行期间保持不变，之后复核两份评测报告中的源码摘要一致。对话服务商为火山引擎方舟；对话模型 glm-5.3-flash，embedding 为 doubao-embedding-vision。原配置仍只通过 `IRIS_TEST_MODELS` 引用，未修改、复制或显示；端到端评测沿用无明文密钥的临时配置及子进程环境传递。

### 小请求

四个良性请求串行发送，要求输出 `{"ok":true}`，`max_tokens=1024`、`response_format=json_object`，不指定温度；两次使用真实配置的 low，两次仅在内存中把可选字段设为 None。程序核对其余请求字段完全一致，未配置时实际没有发送 reasoning_effort。没有临时更改网关超时或重试。

| 顺序 | 档位 | HTTP 调用秒 | 输出 token（含推理） | 推理 token | 推理字符 | finish_reason | 严格 JSON |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | low | 1.477 | 11 | 0 | 0 | stop | 是 |
| 2 | 未配置 | 2.374 | 87 | 76 | 361 | stop | 是 |
| 3 | low | 1.314 | 14 | 3 | 12 | stop | 是 |
| 4 | 未配置 | 2.857 | 109 | 98 | 429 | stop | 是 |

四次均 HTTP 200，每次输入 76 token，合计已报告 525 token；无重试、截断或拒绝。low 的推理消耗和延迟更低，与已有探测及文档一致；它仍可能返回推理，不能称为关闭推理。

### learning_v3 外部模式

使用产品学习路径、v5 提示词、16000 token 输出上限、含重试／JSON 修正的 180 秒总预算、默认案例并行 4。10 段、136 条消息、15 个最终批次；resumed_cases=0。外部模式只导出材料，没有生成判分文件或质量报告。

| 学习调用 | P50 秒 | P95 秒 | 最大秒 | 超时 | 429 | 放弃批次 | JSON 修正 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 15 | 7.02 | 14.09 | 17.250 | 0 | 0 | 0 | 0 |

15/15 首次响应均严格 JSON，全部 finish_reason=stop、parse_status=direct；>45／60／120／180 秒均为 0。9 次返回推理字段，合计 1460 字符，仅保存计数。分位数按 Gateway.duration_ms 线性插值，分母为全部学习 HTTP 尝试；本轮没有失败尝试。

学习用量：输入 39,041、输出 5,414（含推理 481），合计 44,455 token。另有 61 次 embedding 请求，已报告输入 4,493 token；全链路共 76 次调用，全部成功，合计已报告 48,948 token。embedding 未报告的 completion／reasoning 用量保持未知，不虚构成 0。CLI 墙钟 38.822 秒，评测器内部 elapsed_seconds=35.7 秒，差额包含命令启动及导出等开销。

### E001／E011 外部模式与 U04 时序

使用真实 serve 子进程、HTTP 接收、后台自然学习、强制重启与 prepare，保持默认调度并行 2、180 秒学习预算、900 秒排空等待。两个脚本按顺序运行，5 个学习批次全部 succeeded，每批一次尝试，重启前后批次状态一致，两个检查点的近期消息隔离检查均通过。

| 脚本 | 学习调用／成功批次 | 学习 P50 秒 | 学习 P95 秒 | 学习最大秒 | 对应记忆出现最长秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| E001 | 2／2 | 3.95 | 4.42 | 4.469 | 5.397 |
| E011 | 3／3 | 3.02 | 5.82 | 6.130 | 7.804 |

时序计算仅使用 HTTP 回执、已返回记忆的 sources、以及状态轮询首次观察到新增／修订记忆的时间：E001 的 music/m2 对应记忆 1，为 5.397 秒；E011 的 cleanup-group/g2 对应记忆 2，为 7.804 秒，g4 对应记忆 4，为 7.797 秒。来源入口均为 realtime，三项观察均 <60 秒；轮询约 0.2 秒，数值是观察上界。

本轮不调用 e2e-score、不写 facts／forbidden 判分，也不把来源 ID 匹配当作语义覆盖判定。上述数字确认这次所对应记忆在 U04 的一分钟时限内出现，未据此宣布脚本质量或正式 U04／M1 门槛通过。

| 脚本 | 学习输入 token | 学习输出 token | 其中推理 token | 学习总 token | 含 embedding 的已报告总 token |
| --- | ---: | ---: | ---: | ---: | ---: |
| E001 | 4756 | 277 | 45 | 5033 | 5335 |
| E011 | 7143 | 432 | 6 | 7575 | 8040 |

端到端共 18 次网关调用（5 次学习、13 次 embedding），全部成功；学习响应均 HTTP 200／stop，没有 429、超时、截断、JSON 修正、放弃批次或模型暂停。CLI 总墙钟 21.969 秒，评测器内部 duration_seconds=21.46 秒。

## 产物、复现与问题

这三项验证按以下命令／方式执行，输出目录均全新且在仓库外：

```bash
export IRIS_TEST_MODELS=/Users/cassia/Local/Code/iris-core/test-models.toml
# 小请求由仓库外 small_probe.py 调用 Gateway.chat，low／未配置各两次。
uv run --no-sync iris eval learning --judge-mode external \
  --corpus evals/learning_v3.jsonl --out <全新仓库外目录>/learning-v3
uv run --no-sync iris eval e2e --judge-mode external \
  --script E001 --script E011 --out <全新仓库外目录>/e2e
```

时间为 2026-10-07（Asia/Shanghai）：小请求约 14:57，学习验证 14:59:16—14:59:55，端到端验证 15:00:43—15:01:05。同一时间只运行一个本会话的学习／验证作业；共享账户的其他会话负载未隔离，结果不代表稳定吞吐上限。

| 来源摘要 | 值 |
| --- | --- |
| 学习检查点指纹 | `c7a6826d3eb057b9f99046bb3b07150b4e4fd160af6f47e486a328a1ccedd873` |
| 学习源码 SHA-256 | `2e59bc975fb6bf4b74e50de27932fc80da0faaaede5fc4d4c6c1239d1d878c44` |
| 端到端运行指纹 | `daca0d1805aff0ebcc909549f91497d569363872f83e7fa12d0a8c8b293a0c30` |
| 端到端源码 SHA-256 | `96b2f8b6cd026dc121a16c0bada5904ed0a673ebe395cd281095cac6c6816b49` |

学习和端到端沿用各自的原生源码摘要算法（前者还串接文件名），所以源码 SHA-256 数值不同；均已在三项真实验证结束后重新计算核对。

完整材料、run.json、检查点、逐次调用诊断、小请求脚本与测试日志归档在 `/Users/cassia/Local/Code/iris-glm-adapter-artifacts-20261007`，没有提交。归档包含 49 个数据文件、976,602 字节；SHA256SUMS.json 的 SHA-256 为 `c0aebf9cc50082aab811472490fd78b69ae4affa40d1b26fda9f6f2944ecde4f`。逐文件复制校验及实际密钥／配置端点扫描通过。没有保存推理全文。

遇到的问题与边界：

- 初次实施时配置缺少档位，按用户要求暂停真实调用；用户补好后继续。本次没有网络、限流、超时或排空故障。
- 真实请求未触发内容审核拒绝；拒绝／账户／限流等异常路径依赖本地假 HTTP 测试覆盖，没有构造违规内容。
- 核对 E011 时发现记忆 2 的正文写 2026-10-11，而 event_time 为 2026-10-10；这是直接可见的字段不一致，留给学习提示词任务处理，本次未改学习代码或判分。时序观察不代表内容质量正确。
- 本轮仍使用冻结的 v5，不评估并行会话的 v6；没有寻找或运行隐藏验收集。
- 仅在 macOS 验证；两个 Python 版本只有前述已有依赖警告。
