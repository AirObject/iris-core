# 互有细节自动合并的细节保全判分 auto_v1

本说明只用于 `resolution=auto_v1` 的外部判分，版本名 `merge_details_auto_v1`。误处理、正确处理、保护和写回损坏由 `auto_score.py` 按规格确定性计算，不需要判分，也不要在这里重复判断。只判一件事：合并后的正文有没有丢掉该保留的细节，或者加进不该有的内容。

只使用本轮材料，不查看另一轮判分。把 `detail-judgment-template` 复制到独立目录，保留其中的 `manifest.json`（它绑定材料指纹），按案例逐个填写同名 JSON 文件。两轮互不可见；分数汇总时任何一轮的不利结论都算不利。

## 判什么

每个案例的 `cases/<编号>.json` 中，`merges` 列出实际发生、且语料标注了 `must_keep`／`must_not_add` 的合并。`before` 是合并前的两条正文，`result_content` 是合并后的正文。

对每个 `must_keep` 片段，按顺序在 `must_keep_present` 中给一个布尔值：

- `true`：合并后的正文表达了这个细节。语义等价可以算保留，例如“每周一次”和“一周一次”；量词、人称写法的小差异不算丢失。
- `false`：细节缺失，或表达被改变了：限定被去掉或放宽、数字或时间不一致、否定不一致、对象或条件变了。只在来源或历史里存在、正文里没有的，算缺失。

对每个 `must_not_add` 片段，按顺序在 `must_not_add_absent` 中给一个布尔值：

- `true`：合并后的正文没有写出这个内容。
- `false`：写出了这个内容，或表达了相同的意思（换一种说法也算）。

`reason` 必填，写清具体判断依据：正文中哪句话对应或缺失哪个细节。不要改写正文，也不要给建议。

## 格式

```json
{"format_version": 1, "case_id": "...",
 "merge_detail_results": [
   {"action_id": "co-12",
    "must_keep_present": [true, false],
    "must_not_add_absent": [true],
    "reason": "..."}
 ]}
```

数组长度和顺序与案例文件一一对应，不得增删或调换；布尔值只接受 `true`／`false`，不接受 null、数字或字符串。`action_id` 和 `case_id` 必须与模板一致。

## 汇总

- 两轮逐字段取“与”，任意一轮判 `false` 即为 `false`，分歧单列。
- 一次合并只要有一个 `must_keep_present` 或 `must_not_add_absent` 为 `false`，就计入“合并丢细节”的分子；分母为这些带标注的实际合并次数，没有合并时为 null。
- 门槛为丢细节 0。未提供判分时该指标为 null，不能据此宣称质量通过。

假模型只验证流程，不能作为真实模型的质量结论。
