# 当前任务

**2026-09-27：初始化完整重构已完成本地工程交付；默认资料＋两组真实API key即可创建可用本地工作区，8080已更新并保留原数据。真实供应商连接与模型效果未验证。**

工作目录`C:\Users\leaf\Projects\iris-core`，`main@523c1d9cc2b3078ea45895a13295547dddc7b49d`，本次交付由用户授权本地提交；提交记录以Git历史为准。按用户2026-09-26“完整重构、不再以旧文档约束、其他资料默认”的最新授权实施；继续本会话总控、子代理与文档维护例外，实际项目仅总控串行写入，子代理使用各自临时副本。38份起点已有文件及旧记录已逐项保全于[基线](C:/Users/leaf/AppData/Local/Temp/iris-onboarding-refactor-20260926-194034/baseline-files.json)。

## 要求、交付与剩余边界

| 批准要求 | 现状／缺口 | 负责人 | 验证证据 |
| --- | --- | --- | --- |
| 只填必要资料 | 已交付：Iris、中性可选背景、自动时区；两张Provider卡只填API key，其他用[统一默认](../architecture/configuration.md#managed-setup-defaults)；旧草稿明确确认后替换 | 总控／凭据代理 | 新向导实际浏览器；最终初始化及诊断定点 |
| 普通使用配置 | 已交付：USAGE_ONLY，无本地累计金额或终身试用次数上限；usage实际记录，未知金额保持null | Provider代理／总控 | 原生33次回环HTTP，32次已知失败后第33次成功，重开原确认新增请求0 |
| 直接进入本地工作区 | 已交付：原子本地角色发布、真实本地管理绑定、状态／目标／查询可用；不要求先模型生成审核，不授予外部宿主权限 | 角色代理／总控 | 12项最终关联，含收尾中断原键恢复、严格HTTP协议；QA实际状态／目标及重启 |
| 私有密钥及创建后更换 | 已交付：独立0700／0600卷、不可变引用、不回显、不进入浏览器恢复记录；双连接原生配置激活，旧请求保留原连接 | 凭据代理／总控 | 实际SQLite轮换／原确认、浏览器双密钥激活、重启摘要及数据卷无原始key核验 |
| 旧实例与8080数据保留 | 已交付：仅未开始业务的DRAFT／VALIDATED支持显式格式升级；已有业务旧格式按原声明读取。8080保留管理员、会话、草稿及四个身份 | Provider代理／总控 | 停机6文件备份完全相同；升级ACTIVE，前后记录摘要一致 |
| 实际用户连接与真实效果 | 用户仍需提供真实API key；8080原VALIDATED草稿保持，当前CONFIGURATION_REQUIRED，不写成用户业务已就绪 | 用户资料／独立授权 | QA使用合成密钥且请求／尝试均0；未请求真实供应商 |
| 质量、音视频、新增真实调用及费用 | 继续暂缓／待批准；不因本次重构恢复 | 用户决定 | [暂缓事项](DEFERRED_ISSUES.md) |

## 对应版本与检查

最终1067份工程文件集合SHA256：`7e2cd5349f3fa8ef5f1b72113ac2052c0847dfaa5775aecc1dfaba89109ace83`。算法、全部文件指纹、命令参数、退出码、原始日志、失败和委派证据见[交付清单](C:/Users/leaf/AppData/Local/Temp/iris-onboarding-refactor-20260926-194034/delivery-manifest.json)与[版本映射](C:/Users/leaf/AppData/Local/Temp/iris-onboarding-refactor-20260926-194034/final-version-mapping.json)。源码／协议／客户端／Web产物547份与最终镜像逐项一致。

Docker Linux amd64离线检查：首轮75项429.772秒，73通过、2失败，原日志保留。旧诊断断言已改为实际必填密钥；Provider版本测试暴露后台通信时钟争单写者，最终按既有fixture方式经公开close排净无关定时器，保留全部profile／secret／usage／回滚／重开断言。最后仅4份文件相对首轮变化：本地发布收尾及新增故障测试由12项69.464秒验证；诊断模块9项2.966秒；最终Provider版本1项99.422秒，均退出0。未变检查按精确文件及调用范围映射复用，不宣称单次最终全套全绿，也不把隔离测试外推为并发负载稳定。

最终锁定Pyright1.1.413覆盖五目录1022份Python，退出0、0错误／0警告；环境无项目`.venv`的提示保留。最终`npm run check`、`npm run check:browser`、`npm run build`、普通／TLS Compose静态合并及`git diff --check`均退出0。实际浏览器验证默认资料创建到READY、状态与目标保存、双密钥更换、重启持久读取；QA请求／尝试账本0、秘密权限及无数据卷泄漏已核验，QA容器停止保留。未运行全仓unittest、完整Playwright故障脚本、Pylance、当前arm64或真实供应商／质量验证。

## 本地运行与交付点

`iris-local-test-20260926`运行`iris-local-test:20260926-product-final`，镜像ID`sha256:f7659150f8a59354e0ebd4dc2be09c32c62a801732e98e6f7b465cc3f2aa9a8f`，健康；桥接仅发布`127.0.0.1:8080`。显式升级为MANAGED_PRODUCT_V1，装配摘要`a1b9b94cefef19b12a73d2950cdf506cbbc4f2d63ae03c28edc0253f5d3426eb`。原数据卷及只读部署秘密卷保留，新增私有Provider卷；停机备份`iris-local-test-backup-20260926-product`及旧容器`iris-local-test-20260926-before-product`保留且旧容器停止。18080／18081未改。浏览器返回8080原管理员登录页，未代用户替换草稿、填写真实密钥或启用模型。

本轮仅本地提交；未安装依赖、推送、合并、正式部署或产生真实供应商请求。后续真实模型验证、费用及部署仍须分别明确授权；本次结论仅为上述本地工程交付，不宣称整个项目或真实模型能力全部完成。
