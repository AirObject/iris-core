# EF-22 提示词冻结（第一步，未运行真实模型）

基线：main `fc459cdda0ba297cb2854457f8e00b1979d644cc`。默认改为 observe_v8／admit_v8，旧文件保留；没有合入 #116，observe_v8 从 main 的主题观察方式演进，不整包采用 observe_v6。

| 文件 | SHA-256 |
| --- | --- |
| entry_profile_observe_v8.md | `70f89813195976f3ca9aaf9d4886cc8942955ceb6b202546b0c49ccbb0126c9a` |
| entry_profile_admit_v8.md | `e9ca65ea50a863101a81f7b3ee634e53b8a5628b7b152ebc4b17163ade12c690` |

观察保留有依据的共同维度，支持逐步确认／修订，所有类别的拒绝反馈仅作诊断；说法正文由字段构造，摘录可选择原文连续分段区间。

准入使用 semantic_v8：话题逐组独立性与逐点支持，说法逐来源实际使用和释义绑定，群规正向范围／持续性／公告或多人认可。map 页只提取局部支持，final 必须证明完整正文；缩引后重算，不把分页分析或语义 accept 当成实际写回。

预先登记的真实评测尚未开始：后续须规划者通知，daily_plus_final_flush，scoring_v3，独立双判，有依据 ≥90%、违规与禁止内容 0，覆盖不低于同流程 main 实测基线（既有公开报告14/40）。本提交不宣称质量提升或门槛通过。
