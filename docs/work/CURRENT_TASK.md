# 当前任务

**梦境与长期维护已通过监督技术验收，按用户明确授权提交本阶段。** 2026-09-17集中复核未发现新的阻塞问题；自动冷却续办、持久退出及原键回流两项阻塞已关闭。批准正文见[梦境与长期维护](../architecture/dream-and-long-term-maintenance.md)，真实限制见[暂缓记录](DEFERRED_ISSUES.md#dream-real-validation)。

提交前实际基线：main／HEAD `abd7717393b713d4369f1b3ac4d5245d08dea0e3`，暂存原为空。拟提交本阶段164份工程变更及必要契约／验收记录；下一阶段计划与导航独立保留，不纳入本次提交。标题：`feat(dream): 实现梦境调度、长期维护与周期persona`。不推送、合并或部署。

完成范围：同一日常宿主的自动／手动梦境、增量来源影响、时间衰减／遗忘及到期删除、独立监管后周期persona原子发布、暂停／续办／中止、专注FIFO回流、权限门控、HTTP观察及原键恢复。真实正常冷却不再使调度停用；退出已提交的DRAINING按原事实本地收尾，不以新revision重放旧退出，不重新授予模型权限。

最近验证由执行者在Docker Linux arm64执行，监督未运行项目、测试、容器或模型。[交付索引](/private/tmp/iris-dream-repair-ydi3692c/index.json)、[版本复用范围](/private/tmp/iris-dream-repair-ydi3692c/version-reuse.json)、[监督核对](/private/tmp/iris-dream-acceptance-70r3s6oi/review.json)：37项具名成功检查，覆盖原生连续两轮persona及实际30秒间隔、1002输入真实SQLite容量／FIFO、四种新进程退出恢复、控制竞争／期限／UNKNOWN和旧库兼容。中间失败保留并逐项对应复验，不把整体失败命令标为通过。未重跑全量unittest。

最终锁定Pyright1.1.413覆盖867份Python，错误／警告／信息均0。876份工程文件聚合`a77fe11f9cf1c538f7f81702a69bd5ce89a977fe61b788d1c8af8eadb55dd59f`，监督核对1061份证据摘要、全部工程和58份原保护文档一致后仅整理文档；Pylance未验证。未变领域的容量、衰减数学、来源权限及存储故障证据按原版本复用。

真实包仍为11次历史发送：两轮persona发布至第3版，3条记忆保留50→36、相信50；学习身份／来源违规整份拒绝并持久停发。其余21槽不复用，DOCUMENT／QUERY未执行，HTTP为词法降级。usage 48,883 tokens，费用未知；原34份回执及新解释器恢复零新增发送，无UNKNOWN或待清理请求。本轮新增真实请求0，不以工程通过宣称真实效果全部通过。

停止点：本次指定提交完成后，核对提交树与受测工程完全一致，再由监督更新实际提交和下一完整阶段计划，用户手动转交执行prompt。执行者不修改docs或AGENTS，不自动开展下一项实现。已验收里程碑与剩余验证分别见[STATUS](STATUS.md)和[暂缓事项](DEFERRED_ISSUES.md)。
