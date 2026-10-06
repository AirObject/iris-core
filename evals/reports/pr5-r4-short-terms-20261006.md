# PR #5 第四轮：短词与短名字召回（2026-10-06）

新增短词集 12 条有答案查询全部命中、4 条无答案均为空；trigram 纯全文、混合及默认 jieba 路径都成立。原三集的 Recall 不变，但无关误返率 0.7000 → 0.8667、relevant 标注精确率 0.5479 → 0.2966，是本轮明确的退化，不能只用新增查询抬高的总体 Recall 掩盖。默认四组性能 R10 通过。PR 保持开放，待规划者审查。

## 顺序、范围与验证

新设备收尾已先在 `abc8330` 提交、推送并更新 PR；main 的合并为 `25edc97`。随后在 `830efb6` 单独冻结手写的 [短词语料](../recall_short_terms_v1.json) 和 [notes](../recall_short_terms_v1_notes.md)，再测旧实现、写失败测试、修改实现；修复提交为 `dd3bff1`。新增 16 条查询和 16 条记忆未在看到结果后修改，现有语料及 notes 保持原样。没有寻找或读取隐藏集。

修复前的原三集与新集分别运行，源码都是 `abc8330`／`830efb6` 的同一实现；修复后默认命令将四集一起运行。报告中 144 条前后对照使用相同记忆、查询、标注、split 和缓存向量。最终默认评测新增 embedding 请求为 0，质量变化不能归因于这一轮重新取向量。

- 初始新增测试：旧实现 21 failed、9 passed；修复后新增 30 项全部通过。用低余弦假向量验证全文贡献，覆盖一字／两字、长短词混合、姓名／别名、about／speaker／正文、不在场主体、结构过滤、降级及候选截断。
- `uv run pytest`：Python 3.13.15，245 passed，2.34s，1 warning。
- `uv run --locked --isolated --python 3.12 pytest`：Python 3.12.14，245 passed，3.22s，4 warnings。两个版本顺序运行，.venv 保持 3.13。
- `uv build`：sdist 与 wheel 成功。
- 未改 evaluation.py、cli.py、learning_retrieval.py、迁移、学习材料、提示词或评分；本轮未运行学习评测。学习影响由「test: GLM 学习基线与 PR #5 对照」另行测量。

## 修复方式

查询在 trigram 下保留一字／两字实词，并查已有 jieba 索引；长词仍查 trigram。各路按排名轮流合并、去重，共用 200 条全文候选上限，每条只获得一次 RRF 全文分。结构过滤和点名锚点均先于截断，无正文全表扫描或 LIKE 扫描。

去除已知姓名后没有实词的查询，从涉及人／说话人关系和姓名／别名的 jieba 全文短语生成候选；正文继续按最长标签和词边界校验。这覆盖姓名只在 about 或正文、不在当前参与者中的情况。已有通用停用词跨 jieba 分词单元识别，避免“来着”被拆成“来／着”后产生假话题词；没有新增话题词表或按查询内容特判。学习使用的分词函数与独立检索路径保持原样。

## 标定

命令：`uv run iris eval recall --split dev --calibrate --out evals/reports/pr5-r4-after`。全部 122 条 split=dev 的公开查询参与，v2 的 22 条历史 holdout 不参与选择；最终 144 条全部报告。维度固定 2048，未运行 --compare-embeddings。144 个混合方案加两个全文对照，选择规则没有改变。121 次新 embedding 请求均成功。

全网格最高 Q=0.9761112640；推荐 Q=0.9723761187，差 0.0037351453，在严格小于 0.01 的持平区间内（共 8 个方案）。按 relevant 标注精确率、误返率、Q、网格顺序选出 `jieba_hybrid_d2048_instruction`。

| 参数 | 修复前默认 | 标定后默认 |
| --- | --- | --- |
| tokenizer | trigram | jieba |
| vector_min | 0.35 | 0.35 |
| vector_relative | 0.75 | 0.75 |
| vector_weight／全文权重 | 2／1 | 2／1 |
| query_prefix | 为这个问题检索能回答它的个人记忆： | 不变 |
| 维度／dtype／RRF k | 2048／float32／60 | 不变 |
| fallback_tokenizer | jieba | jieba |

选择已写入 retrieval_defaults.json；只影响新数据库，已有 runtime_settings.retrieval 保留。该配置文件变更仅应用本次检索标定结果。

完整网格：[标定 Markdown](pr5-r4-after/recall-20261006T014131002771Z-dev.md) · [标定 JSON](pr5-r4-after/recall-20261006T014131002771Z-dev.json)。

## 默认召回：同一批查询逐语料、split、类别比较

下表为修复前默认 trigram_hybrid → 修复后默认 jieba_hybrid；单值表示完全相同，— 表示分母为零。类别可重叠。Recall／nDCG 计全部返回，误返只计无答案查询的 relevant 返回，relevant 精确率跨查询合并计数。原三集单列，避免新增短词样本改变分母。

| 语料／分组／类别 | 查询数 | Recall@8 | nDCG@8 | 无关误返率 | relevant 标注精确率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 全部 / all | 144 | 0.9106 → 0.9833 | 0.8882 → 0.9661 | 0.6176 → 0.7647 | 0.5573 → 0.3197 |
| 原三集 / all | 128 | 0.9813 | 0.9561 → 0.9619 | 0.7000 → 0.8667 | 0.5479 → 0.2966 |
| recall_v1 / all | 38 | 0.9833 | 0.9759 → 0.9740 | 0.7500 | 0.6102 → 0.4286 |
| recall_v1 / dev / 全部 | 38 | 0.9833 | 0.9759 → 0.9740 | 0.7500 | 0.6102 → 0.4286 |
| recall_v2 / all | 66 | 0.9933 | 0.9616 → 0.9816 | 0.8125 → 0.9375 | 0.4623 → 0.3036 |
| recall_v2 / dev / 全部 | 44 | 0.9902 | 0.9838 | 0.7000 → 0.9000 | 0.4533 → 0.2906 |
| recall_v2 / dev / 别名 | 8 | 1.0000 | 1.0000 | 1.0000 | 0.3529 → 0.3158 |
| recall_v2 / dev / 口语 | 12 | 1.0000 | 1.0000 | — | 0.5652 → 0.3023 |
| recall_v2 / dev / 按参与者召回 | 5 | 0.9167 | 0.8622 | 0.0000 → 1.0000 | 1.0000 → 0.3333 |
| recall_v2 / dev / 换个说法 | 12 | 1.0000 | 1.0000 | — | 0.6500 → 0.3824 |
| recall_v2 / dev / 无答案 | 10 | — | — | 0.7000 → 0.9000 | 0.0000 |
| recall_v2 / dev / 近期消息去冗余 | 3 | 1.0000 | 1.0000 | — | 0.8000 → 0.4444 |
| recall_v2 / dev / 问已知的人的未知属性 | 5 | — | — | 0.8000 → 1.0000 | 0.0000 |
| recall_v2 / 历史 holdout / 全部 | 22 | 1.0000 | 0.9144 → 0.9769 | 1.0000 | 0.4839 → 0.3333 |
| recall_v2 / 历史 holdout / 别名 | 4 | 1.0000 | 0.7103 → 0.8770 | 1.0000 | 0.3333 |
| recall_v2 / 历史 holdout / 口语 | 5 | 1.0000 | 0.9262 | — | 0.7143 |
| recall_v2 / 历史 holdout / 按参与者召回 | 4 | 1.0000 | 0.6667 → 1.0000 | 1.0000 | 0.5000 → 0.2308 |
| recall_v2 / 历史 holdout / 换个说法 | 6 | 1.0000 | 1.0000 | — | 0.8571 → 0.6000 |
| recall_v2 / 历史 holdout / 无答案 | 6 | — | — | 1.0000 | 0.0000 |
| recall_v2 / 历史 holdout / 近期消息去冗余 | 2 | 1.0000 | 1.0000 | — | 1.0000 → 0.7500 |
| recall_v2 / 历史 holdout / 问已知的人的未知属性 | 3 | — | — | 1.0000 | 0.0000 |
| recall_conversation_v1 / all | 24 | 0.9444 | 0.9078 → 0.8873 | 0.3333 → 0.8333 | 0.7826 → 0.1765 |
| recall_conversation_v1 / dev / 全部 | 24 | 0.9444 | 0.9078 → 0.8873 | 0.3333 → 0.8333 | 0.7826 → 0.1765 |
| recall_conversation_v1 / dev / 不在场的人 | 3 | 0.6667 | 0.6667 | — | 1.0000 → 0.1053 |
| recall_conversation_v1 / dev / 别名 | 4 | 1.0000 | 0.9033 | 1.0000 | 0.8000 → 0.2500 |
| recall_conversation_v1 / dev / 对话中准备 | 24 | 0.9444 | 0.9078 → 0.8873 | 0.3333 → 0.8333 | 0.7826 → 0.1765 |
| recall_conversation_v1 / dev / 按参与者召回 | 1 | 1.0000 | 1.0000 | — | 0.5000 → 0.1667 |
| recall_conversation_v1 / dev / 换个说法 | 4 | 1.0000 | 1.0000 | — | 1.0000 → 0.2105 |
| recall_conversation_v1 / dev / 无答案 | 6 | — | — | 0.3333 → 0.8333 | 0.0000 |
| recall_conversation_v1 / dev / 正文提及 | 1 | 1.0000 | 0.7098 | — | 1.0000 |
| recall_conversation_v1 / dev / 点名锚点 | 3 | 1.0000 | 0.7802 | — | 0.8000 |
| recall_conversation_v1 / dev / 近期消息去冗余 | 3 | 1.0000 | 1.0000 → 0.8155 | 0.0000 | 1.0000 → 0.2222 |
| recall_conversation_v1 / dev / 问已知的人的未知属性 | 2 | — | — | 0.5000 → 1.0000 | 0.0000 |
| recall_conversation_v1 / dev / 问我自己 | 9 | 1.0000 | 1.0000 | 0.0000 → 1.0000 | 1.0000 → 0.1905 |
| recall_short_terms_v1 / all | 16 | 0.3333 → 1.0000 | 0.3333 → 1.0000 | 0.0000 | 1.0000 |
| recall_short_terms_v1 / dev / 全部 | 16 | 0.3333 → 1.0000 | 0.3333 → 1.0000 | 0.0000 | 1.0000 |
| recall_short_terms_v1 / dev / 不在场的人 | 2 | 0.5000 → 1.0000 | 0.5000 → 1.0000 | — | 1.0000 |
| recall_short_terms_v1 / dev / 两字关键词 | 10 | 0.2500 → 1.0000 | 0.2500 → 1.0000 | 0.0000 | 1.0000 |
| recall_short_terms_v1 / dev / 两字名字 | 6 | 0.5000 → 1.0000 | 0.5000 → 1.0000 | 0.0000 | 1.0000 |
| recall_short_terms_v1 / dev / 人物筛选 | 5 | 0.2500 → 1.0000 | 0.2500 → 1.0000 | 0.0000 | 1.0000 |
| recall_short_terms_v1 / dev / 仅正文提及 | 2 | 1.0000 | 1.0000 | — | 1.0000 |
| recall_short_terms_v1 / dev / 仅涉及人 | 2 | 0.0000 → 1.0000 | 0.0000 → 1.0000 | — | — → 1.0000 |
| recall_short_terms_v1 / dev / 无人物筛选 | 5 | 0.2500 → 1.0000 | 0.2500 → 1.0000 | 0.0000 | 1.0000 |
| recall_short_terms_v1 / dev / 无答案 | 4 | — | — | 0.0000 | — |

## 四种方案与归因

下面按每个方案自身的相同参数比较全部 144 条；修复后 trigram_hybrid 与最终 jieba_hybrid 的四项质量数值相同，说明旧集误返增加在短词补查的 trigram 路径上也存在，不能只归因于默认 tokenizer 切换。短词词项使更多记忆成为全文候选；尚未解决的无答案拒绝与近期消息焦点问题会放大过量 relevant 返回。这个解释来自公开返回差异，不推测隐藏集内容。

| 方案 | 返回／reason 变化查询 | Recall@8 | nDCG@8 | 无关误返率 | relevant 标注精确率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| jieba_fts | 4 | 0.7742 → 0.8106 | 0.7315 → 0.7679 | 0.4412 | 0.3217 → 0.3310 |
| trigram_fts | 93 | 0.3197 → 0.8106 | 0.2930 → 0.7673 | 0.2647 → 0.4412 | 0.5593 → 0.3310 |
| jieba_hybrid | 1 | 0.9742 → 0.9833 | 0.9570 → 0.9661 | 0.7647 | 0.3178 → 0.3197 |
| trigram_hybrid | 58 | 0.9106 → 0.9833 | 0.8882 → 0.9661 | 0.6176 → 0.7647 | 0.5573 → 0.3197 |

## 新 dev 查询逐条结果

R 表示 reason=relevant，H 表示 person_highlight；空表示没有返回。标注未变化。

| 查询 | 类别 | 标注相关 | 修复前默认 | 修复后默认 | 修复后 trigram 全文 |
| --- | --- | --- | --- | --- | --- |
| ST01 | 两字关键词、人物筛选 | S01 | 空 | S01 [R] | S01 [R] |
| ST02 | 两字关键词、人物筛选 | S02 | 空 | S02 [R] | S02 [R] |
| ST03 | 两字关键词、人物筛选 | S03 | S03 [R] | S03 [R] | S03 [R] |
| ST04 | 两字关键词、人物筛选 | S04 | 空 | S04 [R] | S04 [R] |
| ST05 | 两字关键词、无人物筛选 | S05 | S05 [R] | S05 [R] | S05 [R] |
| ST06 | 两字关键词、无人物筛选 | S06 | 空 | S06 [R] | S06 [R] |
| ST07 | 两字关键词、无人物筛选 | S07 | 空 | S07 [R] | S07 [R] |
| ST08 | 两字关键词、无人物筛选 | S08 | 空 | S08 [R] | S08 [R] |
| ST09 | 两字名字、仅涉及人 | S09 | 空 | S09 [R] | S09 [R] |
| ST10 | 两字名字、仅正文提及 | S10 | S10 [R] | S10 [R] | S10 [R] |
| ST11 | 两字名字、仅涉及人、不在场的人 | S11 | 空 | S11 [R] | S11 [R] |
| ST12 | 两字名字、仅正文提及、不在场的人 | S12 | S12 [R] | S12 [R] | S12 [R] |
| ST13 | 两字关键词、人物筛选、无答案 | 无答案 | 空 | 空 | 空 |
| ST14 | 两字关键词、无人物筛选、无答案 | 无答案 | 空 | 空 | 空 |
| ST15 | 两字名字、无答案 | 无答案 | 空 | 空 | 空 |
| ST16 | 两字名字、无答案 | 无答案 | 空 | 空 | 空 |

## 默认方案全部 144 条返回对照

完整机器对照（含四方案逐条变化、未四舍五入的指标）见 [pr5-r4-short-terms-comparison-20261006.json](pr5-r4-short-terms-comparison-20261006.json)。

| 语料／查询 | 修复前返回 | 修复后返回 |
| --- | --- | --- |
| recall_v1 / Q01 | M01 [R], M02 [R], M04 [R] | M01 [R], M02 [R], M04 [R] |
| recall_v1 / Q02 | M02 [R] | M02 [R] |
| recall_v1 / Q03 | M03 [R], M04 [R] | M03 [R], M04 [R] |
| recall_v1 / Q04 | M04 [R] | M04 [R] |
| recall_v1 / Q05 | M06 [R], M05 [R], M07 [R], M08 [R] | M06 [R], M05 [R], M07 [R], M08 [R] |
| recall_v1 / Q06 | M05 [R] | M05 [R], M07 [R], M13 [R], M25 [R], M06 [R], M26 [R], M08 [R], M37 [R] |
| recall_v1 / Q07 | M08 [R], M07 [R], M05 [R], M06 [R] | M08 [R], M07 [R], M05 [R], M06 [R], M19 [R] |
| recall_v1 / Q08 | M08 [R], M06 [R], M07 [R] | M08 [R], M07 [R], M06 [R], M22 [R], M21 [R] |
| recall_v1 / Q09 | M09 [R] | M09 [R] |
| recall_v1 / Q10 | M10 [R] | M10 [R] |
| recall_v1 / Q11 | M13 [R], M11 [R] | M13 [R], M11 [R], M30 [R] |
| recall_v1 / Q12 | M12 [R] | M12 [R] |
| recall_v1 / Q13 | M14 [R] | M14 [R] |
| recall_v1 / Q14 | M15 [R] | M15 [R] |
| recall_v1 / Q15 | M16 [R] | M16 [R] |
| recall_v1 / Q16 | M18 [R] | M18 [R] |
| recall_v1 / Q17 | M19 [R] | M19 [R], M08 [R] |
| recall_v1 / Q18 | M20 [R] | M20 [R], M34 [R], M11 [R] |
| recall_v1 / Q19 | M22 [R] | M22 [R], M21 [R] |
| recall_v1 / Q20 | M23 [R] | M23 [R], M24 [R] |
| recall_v1 / Q21 | M26 [R] | M26 [R] |
| recall_v1 / Q22 | M29 [R], M28 [R] | M28 [R], M29 [R] |
| recall_v1 / Q23 | M32 [R], M31 [R] | M32 [R], M31 [R], M22 [R], M07 [R], M05 [R], M26 [R], M08 [R], M24 [R] |
| recall_v1 / Q24 | M33 [R], M34 [R], M35 [R] | M33 [R], M34 [R], M35 [R] |
| recall_v1 / Q25 | M34 [R], M33 [R], M35 [R] | M34 [R], M33 [R], M35 [R] |
| recall_v1 / Q26 | M37 [R] | M37 [R] |
| recall_v1 / Q27 | M36 [R] | M36 [R] |
| recall_v1 / Q28 | M32 [R] | M32 [R], M06 [R] |
| recall_v1 / Q29 | M04 [R] | M04 [R] |
| recall_v1 / Q30 | M09 [R] | M09 [R] |
| recall_v1 / Q31 | 空 | 空 |
| recall_v1 / Q32 | M16 [R] | M16 [R] |
| recall_v1 / Q33 | 空 | 空 |
| recall_v1 / Q34 | M40 [R], M07 [R] | M40 [R], M07 [R] |
| recall_v1 / Q35 | M33 [R], M35 [R], M34 [R] | M33 [R], M35 [R], M34 [R] |
| recall_v1 / Q36 | M05 [R], M07 [R] | M05 [R], M07 [R], M12 [R], M08 [R] |
| recall_v1 / Q37 | M38 [R], M37 [R] | M38 [R], M37 [R] |
| recall_v1 / Q38 | M24 [R] | M24 [R] |
| recall_v2 / D01 | A01 [R], A05 [R], A03 [R], A29 [R], A04 [R] | A01 [R], A05 [R], A03 [R], A29 [R], A04 [R] |
| recall_v2 / D02 | A03 [R] | A03 [R] |
| recall_v2 / D03 | A04 [R], A03 [R] | A04 [R], A03 [R] |
| recall_v2 / D04 | A05 [R], A03 [H], A04 [H], A01 [H] | A05 [R], A06 [R], A09 [R], A07 [R], A28 [R], A20 [R], A08 [R], A03 [H] |
| recall_v2 / D05 | A01 [R], A05 [R], A03 [R], A29 [R], A04 [R] | A01 [R], A05 [R], A03 [R], A29 [R], A04 [R] |
| recall_v2 / D06 | A07 [R] | A07 [R], A28 [R], A20 [R] |
| recall_v2 / D07 | A08 [R] | A08 [R], A17 [R] |
| recall_v2 / D08 | A09 [R] | A09 [R] |
| recall_v2 / D09 | A10 [R], A11 [R], A31 [R] | A10 [R], A11 [R], A31 [R] |
| recall_v2 / D10 | A11 [R] | A11 [R], A31 [R] |
| recall_v2 / D11 | A12 [R], A10 [R], A11 [R] | A12 [R], A10 [R], A11 [R] |
| recall_v2 / D12 | A13 [R] | A13 [R] |
| recall_v2 / D13 | A14 [R], A16 [R], A15 [R], A26 [R] | A14 [R], A16 [R], A15 [R], A26 [R] |
| recall_v2 / D14 | A15 [R], A16 [R], A26 [R] | A15 [R], A16 [R], A26 [R] |
| recall_v2 / D15 | A16 [R] | A16 [R] |
| recall_v2 / D16 | A18 [R] | A18 [R], A09 [R], A05 [R], A22 [R], A17 [R], A19 [R], A31 [R], B08 [R] |
| recall_v2 / D17 | A19 [R], A17 [R], A18 [R], A20 [R] | A19 [R], A18 [R], A17 [R], A20 [R], A27 [R], A09 [R], A05 [R] |
| recall_v2 / D18 | A19 [R], A18 [R], A20 [R] | A19 [R], A18 [R], A20 [R] |
| recall_v2 / D19 | A17 [R] | A17 [R], A11 [R], A14 [R], A18 [R], A19 [R] |
| recall_v2 / D20 | A21 [R] | A21 [R] |
| recall_v2 / D21 | A22 [R] | A22 [R] |
| recall_v2 / D22 | A23 [R], A22 [R] | A23 [R], A22 [R] |
| recall_v2 / D23 | A25 [R] | A25 [R], B12 [R], A09 [R], A28 [R], A05 [R], A22 [R], A26 [R], A31 [R] |
| recall_v2 / D24 | A11 [H], A10 [H], A12 [H] | A11 [H], A10 [H], A12 [H] |
| recall_v2 / D25 | A03 [H], A04 [H], A01 [H] | A03 [H], A04 [H], A01 [H] |
| recall_v2 / D26 | A14 [H], A15 [H], A16 [H] | A14 [H], A15 [H], A16 [H] |
| recall_v2 / D27 | A26 [R], A16 [R], A15 [R] | A26 [R], A16 [R], A15 [R], A30 [R], A20 [R], A19 [R] |
| recall_v2 / D28 | A27 [R] | A27 [R], A09 [R], A14 [R], B10 [R], A29 [R] |
| recall_v2 / D29 | A28 [R] | A28 [R] |
| recall_v2 / D30 | A29 [R], A30 [R] | A29 [R], A30 [R] |
| recall_v2 / D31 | A31 [R] | A31 [R] |
| recall_v2 / D32 | A04 [R] | A04 [R] |
| recall_v2 / D33 | A32 [R] | A32 [R] |
| recall_v2 / D34 | A07 [R], A06 [H], A08 [H], A17 [H] | A07 [R], A06 [H], A08 [H], A17 [H] |
| recall_v2 / D35 | A05 [R], A03 [R], A01 [R], A04 [R] | A05 [R], A03 [R], A01 [R], A04 [R] |
| recall_v2 / D36 | A08 [R], A06 [R], A07 [R] | A08 [R], A06 [R], A07 [R] |
| recall_v2 / D37 | A10 [R], A11 [R], A31 [R] | A10 [R], A11 [R], A31 [R] |
| recall_v2 / D38 | A14 [H], A15 [H], A16 [H] | B04 [R], A03 [R], A14 [H], A15 [H], A16 [H] |
| recall_v2 / D39 | A17 [R], A19 [R], A18 [R], A20 [R] | A17 [R], A19 [R], A18 [R], A20 [R] |
| recall_v2 / D40 | A25 [R] | A25 [R] |
| recall_v2 / D41 | A01 [R] | A01 [R], B07 [R] |
| recall_v2 / D42 | 空 | 空 |
| recall_v2 / D43 | A24 [R] | A24 [R] |
| recall_v2 / D44 | 空 | A15 [R] |
| recall_v2 / H01 | B03 [R], B01 [R], B04 [R] | B03 [R], B01 [R], B04 [R] |
| recall_v2 / H02 | B03 [R] | B03 [R] |
| recall_v2 / H03 | B04 [R] | B04 [R] |
| recall_v2 / H04 | B05 [R] | B05 [R] |
| recall_v2 / H05 | B06 [R] | B06 [R] |
| recall_v2 / H06 | B07 [R] | B07 [R] |
| recall_v2 / H07 | B08 [R], B05 [H], B06 [H], B12 [H] | B08 [R], A31 [R], A14 [R], A09 [R], A05 [R], A22 [R], B05 [H], B06 [H] |
| recall_v2 / H08 | B09 [R] | B09 [R] |
| recall_v2 / H09 | B10 [R] | B10 [R] |
| recall_v2 / H10 | B09 [H], B10 [H], B11 [H] | B11 [R], A09 [R], B18 [R], B09 [H], B10 [H], B15 [H] |
| recall_v2 / H11 | B14 [R], B13 [R] | B14 [R], B13 [R], A20 [R] |
| recall_v2 / H12 | B14 [R] | B14 [R] |
| recall_v2 / H13 | B15 [R], B11 [R] | B15 [R], B11 [R] |
| recall_v2 / H14 | B17 [R] | B17 [R] |
| recall_v2 / H15 | B18 [R] | B18 [R], B10 [R], A09 [R], B11 [R] |
| recall_v2 / H16 | B03 [H], B01 [H], B04 [H] | B04 [R], B07 [R], A22 [R], B03 [H], B01 [H], B12 [H] |
| recall_v2 / H17 | B04 [R], B03 [R] | B04 [R], B03 [R] |
| recall_v2 / H18 | B06 [R], B05 [R], B08 [R], B17 [R], B13 [R] | B06 [R], B05 [R], B08 [R], B17 [R], B13 [R] |
| recall_v2 / H19 | A08 [R], B09 [H], B10 [H], B11 [H] | A08 [R], B09 [H], B10 [H], B11 [H] |
| recall_v2 / H20 | B12 [R], B13 [R], B14 [R] | B12 [R], B13 [R], B14 [R], A28 [R] |
| recall_v2 / H21 | B18 [R] | B18 [R] |
| recall_v2 / H22 | B08 [R] | B08 [R], A22 [R], A09 [R], A05 [R], A31 [R] |
| recall_conversation_v1 / T01 | C01 [R], C13 [H], C14 [H] | C01 [R], C28 [R], C24 [R], C17 [R], C13 [H], C14 [H] |
| recall_conversation_v1 / T02 | C02 [R], C15 [H], C16 [H] | C02 [R], C17 [R], C12 [R], C15 [R], C22 [R], C29 [R], C08 [R], C33 [R] |
| recall_conversation_v1 / T03 | C06 [R] | C06 [R], C04 [R], C17 [R], C19 [R] |
| recall_conversation_v1 / T04 | C30 [R], C17 [H], C15 [H], C18 [H] | C30 [R], C05 [R], C03 [R], C22 [R], C15 [R], C14 [R], C17 [H], C18 [H] |
| recall_conversation_v1 / T05 | C04 [R], C13 [H], C14 [H] | C04 [R], C22 [R], C20 [R], C18 [R], C27 [R], C16 [R], C17 [R], C11 [R] |
| recall_conversation_v1 / T06 | C07 [R], C17 [H], C15 [H], C18 [H] | C07 [R], C17 [R], C15 [R], C24 [R], C02 [R], C16 [R], C21 [R], C31 [R] |
| recall_conversation_v1 / T07 | C15 [H], C13 [H], C16 [H] | C21 [R], C31 [R], C17 [R], C15 [H], C13 [H], C16 [H] |
| recall_conversation_v1 / T08 | C11 [R] | C11 [R], C10 [R], C12 [R], C27 [R], C22 [R], C16 [R], C17 [R], C29 [R] |
| recall_conversation_v1 / T09 | C33 [R], C07 [R] | C33 [R], C07 [R] |
| recall_conversation_v1 / T10 | C27 [R] | C27 [R] |
| recall_conversation_v1 / T11 | C09 [R], C10 [R] | C09 [R], C10 [R] |
| recall_conversation_v1 / T12 | C24 [R], C13 [H], C17 [H], C14 [H] | C17 [R], C24 [R], C05 [R], C28 [R], C13 [H], C14 [H], C18 [H] |
| recall_conversation_v1 / T13 | C31 [R], C15 [H], C16 [H] | C31 [R], C12 [R], C17 [R], C22 [R], C29 [R], C15 [H], C16 [H] |
| recall_conversation_v1 / T14 | C22 [R] | C22 [R], C18 [R], C32 [R], C12 [R], C02 [R], C29 [R] |
| recall_conversation_v1 / T15 | C26 [R], C20 [R], C17 [H], C15 [H], C13 [H] | C26 [R], C20 [R], C19 [R], C12 [R], C22 [R], C29 [R], C17 [H], C15 [H] |
| recall_conversation_v1 / T16 | C05 [R], C13 [H], C14 [H] | C05 [R], C13 [H], C14 [H] |
| recall_conversation_v1 / T17 | C32 [R] | C32 [R], C02 [R], C22 [R] |
| recall_conversation_v1 / T18 | C29 [R], C17 [H], C15 [H], C18 [H] | C29 [R], C03 [R], C15 [R], C30 [R], C14 [R], C17 [H], C18 [H], C16 [H] |
| recall_conversation_v1 / T19 | C15 [H], C16 [H] | C26 [R], C25 [R], C19 [R], C20 [R], C15 [H], C16 [H] |
| recall_conversation_v1 / T20 | C20 [R], C26 [R], C17 [H], C13 [H], C18 [H] | C20 [R], C26 [R], C15 [R], C19 [R], C08 [R], C33 [R], C10 [R], C17 [H] |
| recall_conversation_v1 / T21 | 空 | C06 [R], C04 [R], C02 [R] |
| recall_conversation_v1 / T22 | C33 [R] | C33 [R] |
| recall_conversation_v1 / T23 | C17 [H], C15 [H], C18 [H] | C16 [R], C25 [R], C24 [R], C17 [H], C15 [H], C18 [H] |
| recall_conversation_v1 / T24 | C13 [H], C14 [H] | C13 [H], C14 [H] |
| recall_short_terms_v1 / ST01 | 空 | S01 [R] |
| recall_short_terms_v1 / ST02 | 空 | S02 [R] |
| recall_short_terms_v1 / ST03 | S03 [R] | S03 [R] |
| recall_short_terms_v1 / ST04 | 空 | S04 [R] |
| recall_short_terms_v1 / ST05 | S05 [R] | S05 [R] |
| recall_short_terms_v1 / ST06 | 空 | S06 [R] |
| recall_short_terms_v1 / ST07 | 空 | S07 [R] |
| recall_short_terms_v1 / ST08 | 空 | S08 [R] |
| recall_short_terms_v1 / ST09 | 空 | S09 [R] |
| recall_short_terms_v1 / ST10 | S10 [R] | S10 [R] |
| recall_short_terms_v1 / ST11 | 空 | S11 [R] |
| recall_short_terms_v1 / ST12 | S12 [R] | S12 [R] |
| recall_short_terms_v1 / ST13 | 空 | 空 |
| recall_short_terms_v1 / ST14 | 空 | 空 |
| recall_short_terms_v1 / ST15 | 空 | 空 |
| recall_short_terms_v1 / ST16 | 空 | 空 |

## 性能与内存

Apple M4／macOS 26.6.2 arm64／10 逻辑 CPU／16 GiB，Python 3.13.15、SQLite 3.53.1、NumPy 2.5.3。命令 `uv run python evals/benchmark_retrieval.py --default-config --out evals/reports/pr5-r4-after`。前值来自同一设备上 `abc8330` 收尾的默认配置报告；后值为本轮标定后配置。不是同一分钟的受控测量，不能排除系统负载差异。没有重跑挑选较快结果。

每个规模新进程，点名／不点名各预热 5 次、采样 60 次；合成记忆和预生成查询向量，包含检索、去冗余、JSON 和召回记录，不含外部 embedding 网络。HTTP 是进程内 ASGI TestClient。

| 记忆数／查询 | prepare P50 ms | prepare P95 ms | HTTP P95 ms | 向量 P95 ms |
| --- | ---: | ---: | ---: | ---: |
| 5000／不点名 | 13.2 → 13.8 | 14.5 → 14.2 | 15.4 → 15.5 | 3.0 → 2.9 |
| 5000／点名 | 24.7 → 24.8 | 25.9 → 27.7 | 27.6 → 27.8 | 3.0 → 3.0 |
| 50000／不点名 | 75.5 → 78.2 | 83.2 → 83.4 | 84.6 → 87.6 | 30.0 → 29.8 |
| 50000／点名 | 198.7 → 293.5 | 208.4 → 304.5 | 210.6 → 303.1 | 29.3 → 29.1 |

R10 四组均通过（prepare P95≤500ms）。5 万条点名 P95 从 208.4ms 升到 304.5ms，性能有退化；向量评分 P95 基本不变，增加的全文候选处理是可能原因，未做剖析证明。

| 记忆数 | 索引块 MiB | 载入 RSS 增量 MiB | 查询后 RSS MiB | 载入秒 |
| ---: | ---: | ---: | ---: | ---: |
| 5000 | 40.1 → 40.1 | 46.5 → 46.5 | 183.1 → 183.0 | 0.150 → 0.194 |
| 50000 | 392.8 → 392.8 | 225.8 → 194.4 | 536.0 → 538.2 | 1.403 → 1.555 |

macOS 脚本的 peak_working_set_mib 回退为末尾 RSS，不是真正峰值；报告没有把它当峰值。保留原脚本，不修改评测方法。输出文件的脚本固定名称含 r3，已仅重命名本轮 JSON 为 r4，内容未改变。原始 [修复前性能 JSON](retrieval-performance-macos-default-20261005.json) · [修复后性能 JSON](pr5-r4-after/retrieval-performance-macos-r4-20261006.json)。

## 已知问题和异常

- 原三集无答案误返 21/30 → 26/30，relevant 返回 188 → 354，相关命中 103 → 105；精确率下降不能作为无异常通过。全部 144 条误返 26/34，也未达到 M2 的 ≤0.10 参考值。
- 规划者指出：点名的人没有相关答案时，返回这个人的大量记忆（最多 8 条，全部标为 relevant），比 main 多；本轮不修复。
- 规划者指出：对话准备中用“我姐”“我妈”等亲属称呼提及不在场的人，其相关记忆未召回；本轮不修复。
- 规划者指出：合并多条近期消息时焦点被稀释，返回较多弱相关记忆；本轮不修复。
- 测试／性能警告：Starlette TestClient/httpx 弃用提示；隔离 Python 3.12 的 jieba 有三条无效转义 SyntaxWarning。模型请求没有失败或超时；最终两版本全量测试与构建均成功。
- 新设备第一阶段的旧报告源码指纹不一致、重新取 embedding 的返回差异、扩展压力对照三组超过 500ms 均保留在 [收尾记录](pr5-macos-closeout-20261005.md)，第四轮不把它们改写成已解决。规划者本轮已明确授权重新标定。

本轮证据只覆盖公开 dev 和历史回归分组，不据此宣称隐藏验收或 M1 最终通过。

## 原始证据

修复前原三集：[Markdown](pr5-r4-before/recall-20261006T012947905586Z-all.md) · [JSON](pr5-r4-before/recall-20261006T012947905586Z-all.json)；修复前短词集：[Markdown](pr5-r4-before/recall-20261006T013028341968Z-all.md) · [JSON](pr5-r4-before/recall-20261006T013028341968Z-all.json)。

修复后全部公开集：[Markdown](pr5-r4-after/recall-20261006T014233630764Z-all.md) · [JSON](pr5-r4-after/recall-20261006T014233630764Z-all.json)。

修复前源码 SHA-256：`f1e4af4665af28d28cef7cef8ee8d7d30fff56def39ed471b31ff9e9f18c5527`；标定实现（尚未切换默认 tokenizer）：`151ef0bc39b4543a2d5d77333caea6f6633558e1bc8b3391e7d1d3159105dc33`；最终实现：`8f9d255f6ffbd0ca5eaa631c2cc5d510d35efc1a39de1179d241ddd3c556454d`。全部四份语料的输入指纹逐一一致。
