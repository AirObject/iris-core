# 当前任务

任务：**文档唯一正文迁移，已完成**。用户明确授权监督对话仅本轮执行文档迁移；不授权业务实现、日志契约批准、提交或部署。迁移交付后本对话恢复只读监督。

## 基线与范围

2026-09-09，目录及Git根为`/Users/cassia/Local/Code/iris_memory_core`，分支`main`，HEAD为`3337b2daa9ace5803f7e5b1c7a211bfac7cb8266`。起点有七份未提交文档修改，暂存区及未跟踪文件为空，均作为迁移输入保留。

三份根原文原样移入[reference](../reference/README.md)并冻结；既有product／modules／architecture正文转为唯一维护版本，代码规范在[现行文件](../CODING_STANDARDS.md)维护。更新[指引](../../AGENTS.md)、[索引](../INDEX.md)、模板及相关链接。既有批准规则、建议及待批准状态不变；源码、测试、工程配置和依赖不在本轮范围内。

## 保留的待审事项

- [日志草案](../architecture/logging.md#runtime-diagnostics-contract)及[四组决定](../architecture/logging.md#runtime-diagnostics-decisions)仍待批准；新参数和默认值未批准。
- [配置前置缺口](../architecture/logging.md#runtime-diagnostics-configuration)仍存在：现有解析器拒绝非空validator／dependencies，不因迁移获得对象字段、路径或跨参数校验能力；不得删除必要声明绕过限制。
- [文件故障恢复](../architecture/logging.md#runtime-diagnostics-output)与[独立flush超时](../architecture/logging.md#runtime-diagnostics-delivery)已有草案修订，仍待监督审查及用户批准；合成例子未执行，本轮不决定行为。
- 两个配置切片的验收、提交及历史验证定位保持在[STATUS](STATUS.md)。迁移前文档减负轮次报告224个受影响链接及差异检查通过，日志形成／修订轮次报告72个链接通过；均为历史文档检查，不代表业务验证。

## 本轮验证与停止点

实际标准库文档检查通过：三份冻结文件SHA-256与迁移前一致；日志草案正文原样保留，配置契约仅调整历史状态说明及导航，代码规范条款完整保留；39份主题正文的差异已聚焦核对，整理报告仅适配链接和历史声明。50份Markdown中1915处本地链接及对应锚点有效，无重复显式锚点或现行文件中的旧权威指令。

22份已跟踪非Markdown文件与起点字节一致，无新增非文档文件；HEAD及空暂存区未变。首次差异检查发现一处新增行尾空格，修正后`git diff --check`通过。新增现行规范与参考说明的空白检查通过；冻结文件按原样保留。未运行项目代码、测试或验收例子，未安装依赖、暂存、提交、推送或部署。

交付后恢复只读监督；下一步仅审查已有日志修订，不自动批准或实现配置补充、日志或其他模块。
