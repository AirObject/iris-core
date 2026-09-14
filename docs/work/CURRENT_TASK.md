# 当前任务

上阶段为MiniMax／DeepSeek真实文本适配与受控试验，本次按用户明确授权由监督者完成提交收尾。学习质量未达到门槛，按[已批准暂缓决定](DEFERRED_ISSUES.md#text-learning-quality)不阻塞后续计划；提交不改变质量结论。

提交基线为main／`c2be9327b80be3b3ccb12262af7ff15c4539ea2d`。只提交本阶段源码、测试、所属契约、时区产品决定、质量暂缓及验证规则记录；后续语义检索草案和实施计划不纳入。密钥、准备文件、原试验库／响应和外层授权日志均不提交或改写。

交付包括受控主体登记、Provider凭据解析、MiniMax独立协议及usage-only、DeepSeek协议与计量、试验材料／授权／执行工具、persona审核发布和学习结果／恢复核验。DeepSeek两平台14槽已用尽，无备用；正式记忆分别5／12条。费用估算与外层责任分别见[集中报告](/private/tmp/iris-deepseek-live-jve6u6qk/continuation/report-complete.md)，不冒充供应商账单；旧MiniMax UNKNOWN保持，不新增发送。

最近有效验证由执行者完成：最终macOS／Linux各17项关联测试通过，全量锁定Pyright1.1.413零诊断，编译及差异检查通过。未机械重跑全量unittest；JS／锁文件未改。完整命令、退出码和原失败保留于[证据索引](/private/tmp/iris-deepseek-live-jve6u6qk/continuation/checks-index-complete.json)。本轮监督者未运行项目、测试或模型，核对文件一致后复用该版本证据。

最终578份受测文件／570份Python指纹为`16b7d88d466e9549d800604140a9d9de353b2534563628492a331b1139de0582`，算法为相对路径排序后逐项“路径＋NUL＋文件SHA256十六进制＋LF”再取SHA256。[完整清单](/private/tmp/iris-deepseek-live-jve6u6qk/continuation/tested-files-corrected-final.json)不含密钥及准备文件；[本轮提交范围与复核证据](/private/tmp/iris-supervisor-planning-3ayat7pi/baseline.json)保留初始73份变更身份。真实请求分别使用报告记录的几个版本，不将最终指纹倒推至全部历史请求。

限制：学习质量未通过；生产环境、最大容量／长期负载及Pylance未验证；默认时区仅已批准产品规则，尚未实施。历史原始报告与已确认评分保持，不能用后续结果覆盖。

停止点：完成用户授权的本地提交后，监督者另行编写下一阶段计划；本次提交不授权下一阶段实现、真实调用、推送、合并或部署。后续统一Docker Linux验证，规则只维护于[代码规范](../CODING_STANDARDS.md#validation-environment)。
