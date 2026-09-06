# ADR-0020：Phase 11 Bellis Adapter——进程内插件形态、SDK 分发与人格事实源归属

> 历史决策保留；2026-09-06 对端已接受修订与当前实现差距见 [§13](#13-现状核对2026-09-06)。旧段中的交付目标不能作为当前已完成证明。

- 状态：Accepted（2026-09-05 同日修订并入：§11 交付位置与分发通道、§12 落地复核改判）
- 日期：2026-09-05（初版）/ 2026-09-05（§11 修订）/ 2026-09-05（§12 修订）
- 影响阶段：Phase 11；Phase 12 复用本 ADR 的分发与映射版本化规则，Phase 13/14 复用兼容矩阵
- 基线：§26 Bellis Adapter、§28 SDK 与契约发布、§32.8 Adapter E2E、§36 阶段 11；
  ADR-0006（API 版本与兼容）、ADR-0007（仓库与依赖边界）、ADR-0014（Usage 四阶段）、
  ADR-0018（Phase 9 Persona）、ADR-0019（Phase 10 传输层与 SSE）
- 对端决策：Bellis `docs/adr/0005-memory-provider-seam-and-persona-ownership.md`（Accepted，同日）

## 背景

Phase 10 交付真实 ASGI 传输层后，Core 首次具备被宿主真实调用的完整闭环
（`80468a4`：Core/双 SDK 0.11.0、Schema 11、Contract 1.9.0、OpenAPI 85 路径、
capability 48、fixtures 136、错误码 41）。Phase 11 要在独立仓库交付 Bellis Adapter。

宿主侧的时点决定了本 ADR 的取值空间：Bellis 处于其 Phase 4 Gate 0，
`ContextBlock` / `MemoryQuery` / `MemoryProviderCapabilities` / `MemoryObserveEnvelope`
按其构建指南 §5.3 仍在"待评审、未冻结"清单中；且 Bellis 仓库**没有任何人格实现**
（`persona` / `角色设定` 仅出现在 6 行文档，`packages/` 与 `apps/` 中零代码）。
因此人格归属是一次空地上的分配，而非迁移。

三处需要现在裁决、之后改即为破坏性变更的问题：Adapter 的进程形态、
TypeScript SDK 的跨仓库分发方式、以及人格事实源归谁。

## 决策

### 1. Adapter 形态：宿主进程内插件，独立仓库

Adapter 交付为一个 npm 包 `@iris-memory/bellis-provider`，实现宿主的
`MemoryProvider` 与 `PersonaSource` 两个 Port，内部经 `@iris-memory/sdk` 走 HTTP
访问 Core。这是 §26 三种接入方式中延迟最低的一种，也是唯一能让 Abort 真实传播到
在途请求的一种。

- 代码、Fixture 与发布物只进入独立 `iris-memory-bellis-adapter` 仓库。Core Monorepo
  维持 ADR-0007 的边界：不引入 Bellis 类型、不引入其运行时依赖、不为单一宿主修改
  Canonical Domain。
- Adapter 对宿主的依赖收敛为宿主契约包的 `memory` 子路径（peer dependency），
  不依赖宿主的 runtime / persistence / transport / scene-runtime。
- Lease、Persona 缓存与校验、幂等键、Cursor 对账、有界本地队列与降级矩阵
  **全部封装在插件内部**。宿主只观察到一个健康或不健康的 Provider，
  不需要知道 Core 的存在。
- Adapter 本地只持久化有界重试元数据、Cursor/Commit 对账位置与已验证 Persona
  Cache Key，不形成第二 Canonical Store。

### 2. SDK 分发：先私有 registry，后公开 scope

`@iris-memory/sdk` 此前为 `private: true`，无法被仓库外消费。裁决：

- **阶段一（当前）**：去掉 `private`，发布到**本地私有 registry**（Verdaccio），
  scope `@iris-memory` 通过 `.npmrc` 定向到该 registry，其余包穿透到公共 upstream。
  Adapter 仓库按版本区间依赖，不使用 `file:` 或 git tarball——那会让兼容矩阵失去
  版本语义，也无法验证真实的解析与安装路径。
- **阶段二（Phase 11 收尾调试后）**：同一版本号发布到 npm 公开 scope。
  **只更换 registry 地址，不更换版本语义**，因此兼容矩阵、`peerDependencies` 区间
  与 Consumer Contract 的固定 Fixture 全部不需要重写。
- 版本区间按 ADR-0006 的兼容承诺解释：`schema_version` 与 `contract_version` 的
  additive 变更走 minor，`api_version` 变更走 major。Adapter 声明它支持的
  最小、最大 Core 版本，CI 对**最小、最大与当前** patch 三组组合运行同一批 Fixture。
- 发布顺序恒为：兼容的 Core → Schema/Contract → SDK → Adapter。
  新增 Core 可选字段或 capability 一律先协商后启用。

### 3. `RecallCandidate` → `ContextBlock` 的规范映射

映射是 Adapter 的公开契约，版本化为 `mappingVersion`，与 `RECALL_RANKER_VERSION`
独立演进。Core 侧不新增字段来迁就宿主形态；宿主侧按其 ADR 0005 扩展 `ContextBlock`。

| 目标字段 | 来源 | 规则 |
| --- | --- | --- |
| `id` | `candidate_id` | 直传 |
| `revision` | `resource_ref.revision` | 整数转十进制字符串；只做相等与单调判定 |
| `contentHash` | `content_hash` | 直传，且**只对 Core 返回的原始字节校验** |
| `text` | `text` | 直传 |
| `category` | `resource_ref.resource_type` + `category` | 见 §12.1：以 `resource_type` 为主键推导宿主闭枚举，`claim`+`relationship` 单独分流；未知 `resource_type` **fail closed**（丢弃并审计），绝不从文本猜测 |
| `providerCategory` | `category` | 保留 Core 原值 |
| `placement` | `placement` | 直传 |
| `priority` | 响应中的**顺序** | 由 §18.6 排序位次确定性推导，记 `priorityDerivationVersion`。**禁止用 `final_score` 冒充** |
| `confidence` | — | 不填。`final_score` 是相关性不是置信度，混用会污染宿主排序语义 |
| `tokenEstimate` | `token_estimate` | 直传 |
| `expiresAt` | `expires_at` 与 `cache_until` | 取**较早**值 |
| `privacyScope` | `MemoryQuery.privacyScope` | 回填宿主本次请求自带的值（宿主 Schema 要求 `min(1)`，不可省略）。Adapter 不产生、不推导、不覆盖该值 |
| `privacyLabels` | `privacy_labels` | 直传数组，**禁止压成单值** |
| `conflictHint` | `conflict_state` | 直传，作为宿主冲突分组的输入而非结论 |
| `sourceRefs` | `source_refs` | 可逆 URN `iris:<resource_type>:<resource_id>@<revision>` |
| 不映射 | `scores` / `final_score` / `scope` / `subject_entity_id` | 只进宿主 Manifest Audit，不进模型可见正文 |

`contentHash` 的定位是**来源存证**而非规范化摘要：Core 的哈希算在其 canonical 正文上，
宿主若在文本规范化后重算必然不一致。宿主去重另行计算，两者不混用。

### 4. Persona：Core 是人格事实源，输出结构化数据

Core 接管人格的**事实**，宿主保留人格的**表达**。

- Core 拥有：人格身份/性格/叙事（不可变发布版本 + `content_hash` +
  `published|superseded|revoked`）、可衰减的瞬时状态（到期归位 baseline）、
  演化策略（`locked|manual|bounded_auto` 与可改/敏感字段集）、
  证据驱动的演化提案、人工评审、回滚与历史。
- 宿主拥有：把结构化人格渲染成稳定前缀文本、安全规则优先级、
  以及"取不到人格能否开播"的就绪策略。
- **Adapter 只输出结构化 JSON，永不输出 prompt 文本。** 这是硬约束：
  它同时保证 Core 无法向宿主注入指令、宿主保持 Prompt 单一所有权、
  人格版本与渲染模板成为两个独立演进维度。
- 人格**不走 Recall 通道**。Adapter 导出独立的 `PersonaSource`，由宿主 Runtime
  配置拥有，不参与记忆的前台 Deadline。
- 失效经 Phase 10 的 SSE 事件面推送（`persona.revised.v1`、
  `revision.invalidated.v1`，携带游标可断线续传）。Adapter 收到后后台重拉并原子换入。
- 每个 Recall 响应携带的 `persona_revision` / `persona_content_hash` 是**二次校验源**：
  与已缓存不一致时立即作废缓存，但不修改进行中的宿主 Cycle。
- 发布版本状态为 `revoked` 时 Adapter **fail closed**，不返回该人格。
- 演化提案由 Phase 10 Reflection 从已提交 Observation 生成，评审与发布在 Core 一侧；
  Adapter 不暴露任何人格写入路径，宿主不注册人格写工具。

### 5. Usage 回传

Adapter 接收宿主在 Cycle adoption 后异步投递的使用报告，映射为
`RecallUsageReportRequest` 的 `returned_candidate_ids` / `host_selected_candidate_ids` /
`model_visible_candidate_ids`，满足 ADR-0014 的四阶段语义。

- `model_visible` 必须是宿主实际渲染进模型请求的集合，且恒为 `host_selected` 的子集，
  `host_selected` 恒为 `returned` 的子集；Adapter 在发送前校验子集关系，不满足即拒绝上报。
- 上报失败进入 Adapter 的**有界**本地队列重试，绝不阻塞宿主当前回复。
- 队列满时 fail closed 并记录低敏诊断，不静默丢弃。

### 6. Observe 边界：只接受实际生效的输出

- 宿主 Scene `completed` 且输出已开始生效 → Assistant Observation，正文为**已确认生效的片段**。
- 部分输出 → 只提交已确认 segment 范围。
- `cancelled` / `failed` / 未播放 / 策略阻断 → **不提交**（Phase 11 量化门禁要求该计数为 0）。
- 宿主 `outboxId` → Core `Idempotency-Key`；宿主业务幂等键 → Core 稳定 event id。
- Adapter 崩溃后按 Core Cursor 与宿主 Commit Log 双向对账，重复与漏交均为 0。

### 7. Active Surface Lease 封装在插件内

`off | advisory | required` 由 Adapter 配置决定，宿主不感知 lease 概念：
`required` 模式下申请失败或 Fencing 失效时，Adapter 在 `capabilities()` 报告不健康，
宿主按其 Gateway 规则短路该 Provider，请求不进入回复链路。
`holder_app_instance_id` 取宿主 runtime 实例标识；心跳与续约挂在 `start`/`stop` 生命周期上。

### 8. 降级一律经稳定契约暴露

Core 超时、Vector Route 降级、Persona 不可得、版本协商失败各自映射为宿主可读的
显式降级信号与低敏诊断，不伪造长期记忆、不猜测映射、不静默退化。
未知的**必需** Schema、Persona Hash 不符或 Required Lease 无效时一律 fail closed。

### 11. 修订（2026-09-05）：交付位置改为宿主 Monorepo，分发通道分两段

初版决策 1 把 Adapter 定为独立仓库 `iris-memory-bellis-adapter`，决策 2 把分发定为
"私有 registry → 公开 npm"两段。落地前的核查推翻了前者，并给后者补了一个中间态。

**11.1 交付位置：宿主 Monorepo 的 `providers/memory-iris/`（取代决策 1 的独立仓库）**

三个初版未核查的事实推翻了独立仓库：

1. `@bellis/testkit` 与 `@bellis/contracts` 全部 `private: true`（宿主 10 个包无一例外）。
   决策 1.5 的 Conformance Harness 在 testkit 里，跨仓库消费必须把宿主的发布姿态整体
   翻转——为一个消费者改宿主全部包的发布策略，代价不成比例。
2. 跨仓库要求 `@bellis/contracts` 先发布才能让 Adapter 编译，把交付切成"先发契约包、
   再建 Adapter"两阶段；同 Monorepo 内 pnpm workspace 直接软链，两阶段塌缩为一次改动。
3. 宿主 Phase 4 P0 期间 `ContextBlock` 与 `MemoryProvider` 仍在迭代。同仓库内契约改动
   与消费方更新是一个 commit、一次 CI；跨仓库每轮都要 publish → bump → install。

许可证一度被认为支持分仓，核查后判定**与仓库布局无关**：AGPL 的义务触发在分发与网络
交互，不在源码所在的 git 仓库；无论如何布局，宿主进程都在运行时加载该 Adapter。

**§26 的承重约束仍然满足**：其要求的实质是"Core Monorepo 不引入 Bellis 类型或运行时
依赖"，Adapter 放进宿主仓库对此毫无影响。初版决策 1 的其余全部内容——两个 Port、
依赖只到 `@bellis/contracts` 的 `memory` 子路径、禁止依赖宿主 runtime/persistence/
transport/scene-runtime、Lease 与 Persona 缓存封装在插件内、不形成第二 Canonical
Store——**原样保留**。

位置定为 `providers/memory-iris/` 而非 `packages/`：它是可插拔 Provider，不是宿主核心包，
且该路径当前不在 `pnpm-workspace.yaml` 的 glob 内，恰好承载 §11.2 的中间态。

**11.2 分发：新增"本地 registry + 暂缓接入宿主 CI"的中间态**

决策 2 的两段式（私有 registry → 公开 npm）在宿主 CI 上有一个初版未发现的断点：
宿主 CI 在 GitHub Actions 上执行 `pnpm install --frozen-lockfile`，**够不到本机
Verdaccio**。评估过的 GitHub Packages 也不可行——其 npm registry 要求包 scope 等于
仓库 owner，Core 位于 `AirObject` 组织，`@iris-memory/sdk` 这个名字无法在该 owner 下
发布；且宿主仓库位于另一个 owner，其默认 `GITHUB_TOKEN` 读不到跨 owner 的包。

因此分发改为三段：

| 阶段 | SDK 通道 | Adapter 在宿主 workspace 中 | 宿主 CI |
| --- | --- | --- | --- |
| **当前** | 本地 Verdaccio | **否**——`providers/*` 不列入 `pnpm-workspace.yaml`，依赖经 `link:` 指向 `packages/contracts` | 不变，保持离线可跑与全绿 |
| 过渡 | 公开 npm | 是——`providers/*` 并入 workspace，`link:` 改 `workspace:*` | 增加 Adapter 的 lint/typecheck/test |
| 稳态 | 公开 npm | 是 | 含 Adapter 的完整门禁 |

`link:` 与 `workspace:*` 都是符号链接，契约共演化在中间态即已成立；切换是一行 glob
加一处协议前缀替换，不改任何代码。

**代价（明确记录）**：中间态下 Adapter 不在宿主 CI 覆盖内，其门禁只在本地执行。
这是有意接受的时间盒，必须在 Phase 11 退出门禁前关闭——SDK 转公开 npm 与
`providers/*` 并入 workspace 是退出门禁的一部分，不得带着这个缺口宣布阶段完成。

决策 2 的其余内容——版本区间按 ADR-0006 解释、CI 对最小/最大/当前三组组合跑同一批
Fixture、发布顺序 Core → Schema/Contract → SDK → Adapter、不使用 `file:`/git tarball
作为**跨仓库**依赖手段——原样保留。中间态的 `link:` 是同仓库内的工作区链接，
不是跨仓库依赖，不违反该条。

## 12. 修订（2026-09-05，落地复核后）

实现批次 1 完成后的独立复核推翻了决策 3 的两处字段裁决，并暴露了一处分发环节的
执行错误。三项都在本次修订中关闭。

**12.1 `category` 的映射主键从 `category` 改为 `resource_type`**

初版按 `RecallCandidate.category` 建映射表。核查 Core 实现后发现该字段不是一个词表：
`application/recall.py` 的 `_rank_and_trim` 发出的是 `candidate.category or candidate.route`，
所以同一个字段里混着四套互不相关的 Core 词表——

| 来源 | 取值 |
| --- | --- |
| Claim 类别（`ClaimView.category`） | `identity`、`preference`、`relationship`、`fact`、`community`、`procedure`、`self_narrative` |
| Focus kind（`FocusView.kind`） | `goal`、`question`、`entity`、`clue`、`concern`、`affect`、`pending_input` |
| Note kind（`NoteView.kind`） | `important`、`idea`、`follow_up`、`promise`、`question`、`observation` |
| 路由字面量与路由名回退 | `task`、`relationship`、`episode`、`recent_context`（RecentContextRoute 未设 category）、`state`（StateRoute 未设 category） |

合计 23 个取值，而且 `question` 同时是 Focus kind 和 Note kind——**单凭 `category` 无法消歧**。
按初版裁决建出来的表只命中其中 5 个，其余 18 个会走"未知值 fail closed"被静默丢弃，
等于把大部分召回内容丢在 Adapter 里。

改判：映射主键改为 `resource_ref.resource_type`（`claim`、`episode`、`focus_item`、`note`、
`observation`、`relation`、`state_record`、`task` 共 8 个），它是结构化的、稳定的，
并且天然消解了 `question` 的碰撞。

| `resource_type` | 宿主 `category` |
| --- | --- |
| `observation`、`episode` | `episode` |
| `task` | `task` |
| `relation` | `relationship` |
| `claim` | `relationship`（当 `category == "relationship"`），否则 `fact` |
| `claim`、`focus_item`、`note`、`state_record` 其余 | `fact` |

- `viewer` **恒不产生**。判定一个 Block 是否"关于当前 viewer"需要把 `subject_entity_id`
  解到宿主身份域，而 §3 把该字段留在审计边界、宿主 ADR 0005 §2.4 把身份域判定保留给宿主。
  从 claim 类别猜测会把关于第三方的 identity 事实误标成关于对话者的，因此这一步交给宿主细化。
  `capabilities().categories` 由映射推导而非硬编码，因此也不再虚报 `viewer`。
- **Fail closed 的边界随之收窄到 `resource_type`**：未知 `resource_type` 丢弃并审计；
  已知 `resource_type` 下的未知 `category` **仍然映射**，只在 `audit.unknownProviderCategories`
  标记并记一条低敏诊断。Core 的 category 是增量新增的，丢弃它们等于用向前兼容换掉召回内容。
- 原始 `category` 一律进 `providerCategory`，宿主语义零损失。运营方可用 `categoryMap`
  按 `category` 覆盖单个取值（例如把 `follow_up` 提成 `task`），不必改代码。
- `mappingVersion` 保持 `1`：Adapter 0.1.0 从未发布，v1 没有在外部固化过，
  这是同一版映射的定稿而非演进。§3 关于"宿主枚举扩展或 Core 新增 category 时递增
  `mappingVersion`"的规则从本次定稿之后开始适用。

**12.2 `privacyScope` 由"不填"改为回填宿主请求值**

初版写"不填"。宿主 `ContextBlockSchema` 的 `privacyScope` 是 `z.string().min(1)` 必填，
"不填"在宿主 Schema 下无法表达。改判为回填 `MemoryQuery.privacyScope`——那是宿主本次
请求自带的值，原样送回不构成 Adapter 对身份域的判定，与宿主 ADR 0005 §2.4
"Provider 不得反向决定 `privacyScope`" 一致。

**12.3 SDK 分发：发布物必须与被消费的源码是同一份**

复核发现本地 Verdaccio 上的 `@iris-memory/sdk@0.11.0` 是 Phase 11 SDK 改动**之前**的构建，
不含 `CoreEvent`、`PersonaCurrentResponse`、`SourceCursorEnvelope`、`sourceCursor()`、`events()`。
Adapter 之所以还能编译，是因为它的 `tsconfig.json` 与 `vitest.config.ts` 把
`@iris-memory/sdk` alias 到了隔壁 checkout 的源码目录，绕过了已安装的包。

这恰好是决策 2 否决 `file:`/git tarball 时所说的"无法验证真实解析与安装路径"，
只是以 alias 的形式复发。裁决：

- **禁止对 `@iris-memory/sdk` 设置任何构建期或测试期 alias。** 它是跨仓库依赖，
  必须经 registry 安装路径解析，Adapter 的 typecheck/test 才算验证过真实消费姿态。
  `@bellis/*` 的 alias 不在此列——它们是同仓库 `link:` 依赖，指向源码正是 §11.2
  中间态要保住的契约共演化。
- SDK 版本推进到 **0.11.1** 并重新发布。0.11.0 在本地 registry 上已被占用，
  且转公开 npm 后版本不可变，覆盖发布不是可迁移的做法。0.11.1 落在兼容矩阵的
  `0.11.x` 区间内，矩阵的 `typescriptSdk.minimum/current` 同步更新为 0.11.1。
- Adapter 必须能**独立安装并跑完自己的门禁**（`pnpm install --ignore-workspace` 后
  `lint`/`format:check`/`typecheck`/`test`/`build` 全部由包内声明的依赖驱动）。
  中间态下它不在宿主 workspace 里，靠根 `node_modules` 提供工具链会让声明的脚本跑不起来——
  这会在 `providers/*` 并入宿主 CI 的那一刻变成 CI 失败。

### 13. 现状核对（2026-09-06）

本节记录对端已经接受的边界修订及本轮只读代码核查，不将实现缺口改成新的允许行为。对端依据是 Bellis 仓库 `docs/adr/0006-documentation-and-delivery-boundaries.md`（Accepted，2026-09-06）§3–6；本仓库的当前证据统一见 [Phase 11 报告](../reports/phase-11-verification.md)。

| 原段落 | 当前口径或未关闭差距 |
| --- | --- |
| §1、§5 Adapter 本地重试队列 | Bellis ADR 0006 §4 明确由宿主后台 Outbox 保留重试责任，Observe/Usage 只有远端持久接收或幂等确认才成功。当前 Provider 已等待远端结果并抛回错误，legacy pending 仅保留恢复/对账；宿主可靠 Outbox 仍待接线，内存入队不算 ACK。 |
| §6 Scene completed、取消/失败不提交 | 对端 §3 取代以 Scene Commit 判定输出生效的要求。Scene Commit 是播放前调度意图；独立幂等 effect/progress 事实绑定 Session、连接代际、Scene/Cue 和 segment 范围。completed/cancelled/failed 均只记录实际确认片段，零确认才不产生 Assistant 内容，部分播放后取消仍保留已确认前缀。该宿主协议尚待交付。 |
| §6 Observe 幂等键 | 当前批次键为 `observe:` 加排序后的 outbox IDs；单条 `idempotency_key` 与 `source_event_id` 使用稳定 `eventId`。Usage 使用 `outboxId`；不能把三层键概括成一条直接映射。 |
| §6 双向对账无重漏 | 当前 `reconcile()` 只发出远端 Cursor 落后诊断，不拉取宿主确认事实补投；无真实端到端证据，仍是退出门禁。 |
| §3 Audit 与 contentHash | Provider 原样透传 contentHash；宿主 Builder 的原文字节校验未闭环。scores/final_score/scope/subject_entity_id 的 Manifest Audit 仍是目标，当前返回的 Audit 未包含全部字段。 |
| §12.1 未知类型拒绝、viewer 不产生 | 默认映射遵守该边界，但自定义 `categoryMap` 先于类型检查，可能绕过未知类型拒绝并配置 viewer；需修复或正式裁决后验收，不能用默认映射测试证明全部配置安全。 |
| §4 Persona 撤销、失效与恢复 | `loadInitialPersona()` 对刷新错误统一回退旧缓存，事件 Cursor 也可能在重拉成功前推进；撤销/Hash 失败不能被离线回退掩盖，须补失败关闭、重试及重启追平证据。 |
| §7 Required Lease 与宿主短路 | 插件的 Lease 实现存在，但宿主 Gateway 生命周期和真实 Required 行为尚未完成；还须关闭 Core 在线入口的 Required Surface 覆盖缺口。 |
| §11/12 分发与兼容 | SDK 已按 registry 包消费 0.11.1，无源码 alias；公开 npm、宿主 workspace/CI 仍待完成。插件默认最多接受 Schema 11，当前 Core Schema 14 会拒绝；分类矩阵亦与代码有漂移，必须重测支持范围。 |

后果段中“Core 唯一代码影响为 package.json”是初版时点判断：后续 TS SDK 已增加 Abort、typed 响应、SSE 与测试。本节不更改既有 `/v1` 语义；这些跨仓交付差距由 [Phase 14.0](../development/phase-14-hardening-release.md#140-前置阶段闭环与候选范围冻结) 组织关闭并回写 Phase 11 证据。

## 否决的替代方案

1. **Sidecar 进程 / HTTP 本地服务形态的 Adapter。** 否决：多一跳进程边界与序列化，
   且 Abort 无法真实传播到在途 Core 请求，违背宿主"取消即真正停止"的要求。
2. **Adapter 返回渲染好的 system prompt 文本。** 否决：等价于允许 Core 向宿主注入指令，
   破坏宿主的 Prompt 单一所有权。
3. **人格作为普通 Recall Candidate 返回。** 否决：把授信内容与不可信召回混入同一通道，
   且人格需要进入稳定前缀，按 Candidate 处理会每 Cycle 重复计费并击穿前缀缓存。
4. **SDK 用 `file:` 或 git tarball 跨仓库引用。** 否决：兼容矩阵失去版本语义，
   无法验证真实解析与安装路径，转公开发布时需要重写全部依赖声明。
   （§11.2 的 `link:` 是同一仓库内的工作区链接，不属此列。）
5. **为迁就宿主 `ContextBlock` 形态修改 `RecallCandidate`。** 否决：违反 §26
   "不为单一宿主修改 Canonical Domain"；映射的有损处应由宿主扩展字段解决，
   而非 Core 让步。
6. **等宿主 Phase 4 完成后再对齐契约。** 否决：宿主 `ContextBlock` 一旦冻结，
   本 ADR 决策 3 的六处字段裁决全部变成破坏性变更；当前它们仍在其未冻结清单中。

## 后果与迁移影响

- `sdk/typescript/package.json` 去掉 `private: true` 并具备可发布元数据。
  这是 Core 仓库本 ADR 唯一的代码影响面；Schema、Contract、Migration 与
  Canonical Domain 均不变，Contract 版本不因本 ADR 递增。
- Core 新增一条运维前置：私有 registry 的可用性成为 Adapter 仓库 CI 的依赖。
  Core 自身的 `make ci` 不受影响，仍然离线可跑。
- 兼容矩阵成为长期维护物：每次 Core 发布都需要确认仍在 Adapter 声明的支持区间内。
  Core 回退不得早于仍被 Adapter 使用的最小版本。
- 人格事实源移入 Core 后，Core 的可用性直接影响宿主的开播就绪性。
  宿主保留静态兜底配置以规避该耦合；Core 侧不为此降低 `revoked` 的 fail closed 强度。
- 映射表与 `mappingVersion` 进入 Adapter 的公开契约。宿主枚举扩展或 Core 新增
  category 时递增 `mappingVersion`，并对新旧两版各跑一次 Consumer Contract。
- 本 ADR 不改变任何 v1 已发布语义，不新增错误码，不影响 Phase 12 AstrBot Bridge
  的独立性——决策 2、3、5、7 的版本化与封装规则对其同样适用。
