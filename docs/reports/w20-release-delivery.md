# W20 发布验收与交付

状态：Preparation in progress。用户于2026-09-08明确将全部生产输入标为待定，暂不实现生产验收。W01–W19依赖及W20原退出条件未满足，不能标Completed；Phase11/12保持Deferred。

## 独立完成的发布选择修正

基线2983255，行为提交a088c5f：发布工作流一次仅选择Core或Python SDK，默认Core。上传必须给出与所选pyproject一致的expected_version；两个产物仍共同构建和安装验证，但具有独立上传路径、pypi-core/pypi-sdk环境和启用变量，Core上传不连带SDK。Console生产构建作为独立console-static附件交付。

本地release-selection-001通过版本选择、独立版本、缺失/错误版本及未知选择负例。js-yaml解析实际工作流通过，检查了独立上传路径和环境。文档检查通过；首次格式检查指出测试格式，已用Ruff修正。未运行GitHub、OIDC或PyPI上传，没有外部发布副作用。完整make ci尚未在本包最终候选运行。

## 保留的完整门禁

不可变Release Manifest与架构§38双向追踪、同一RC的全量CI/平台/安全/W18/W19证据、真实GitHub环境保护与Trusted Publisher配置、发布评审及授权上传、注册表安装回读均未完成。旧共享pypi配置不会启用新上传，外部环境迁移待明确配置后执行。

生产OS/架构/Python/SQLite、硬件与规模、Console/导入/导出预算、RPO/RTO、真实Provider/model/version/成本及凭据引用、操作人与发布配置均为TBD；不从本机结果倒填，不启动生产验收。参见[发布运行说明](../operations/publishing.md)及[工作队列](../development/work-packages.md)。
