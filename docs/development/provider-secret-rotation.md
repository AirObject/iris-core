# Provider sealed 主密钥离线轮换

适用：W05 候选 Core 0.15.0 / Schema22，ADR-0049。仅轮换 Provider 的 AES-256-GCM 封装；Console 认证主密钥独立。

## 前置条件

1. 停止所有读取该库的 API、Console 和 Worker 进程；`--allow-offline` 是运营者对停机的明确声明，不会替运营者终止进程。
2. 准备不同的旧、新 Provider 主密钥文件：32 原始随机字节，绝对路径、普通文件、属主为运行用户，权限不宽于 0600，拒绝符号链接和硬链接。新密钥不能复用 `console-auth.key` 的路径或材料。
3. 为本次备份选择新的私有目录，保留当前 `IRIS_MEMORY_SECRET_KEY_FILE` 指向旧密钥，直到轮换成功。

## 执行与发布

```bash
iris-memory-core provider rotate-master-key \
  --database /srv/iris/data/canonical.sqlite3 \
  --old-key-file /srv/iris/secrets/provider-old.key \
  --new-key-file /srv/iris/secrets/provider-new.key \
  --with-backup /srv/iris/backups/before-provider-key-rotation \
  --allow-offline
```

需要备份真实性认证时附加 `--backup-key-file`，使用独立的运营者备份签名密钥。运行时继续执行既有 SQLite 允许版本检查；非允许版本的本地测试环境才使用 `--allow-local-sqlite`。

命令先检查全部 sealed 修订可由旧密钥解密，再创建实际 SQLite 备份并验证，最后在一个事务内重封装全部租户及历史修订。密文逐行重新认证后更新，审计只记录配置与修订标识；配置内容摘要及生命周期不变，secret_ref 不参与轮换。任何行失败都回滚该事务中已更新的行和审计。

输出 `status=completed` 后，将 API 和 Worker 的 `IRIS_MEMORY_SECRET_KEY_FILE` 一并切换到新文件，再启动服务。命令不修改环境变量或覆盖主密钥文件。启动会验证全部 sealed 历史，错误或缺失主密钥会阻止启动；不要只更新其中一个进程的配置。

## 失败恢复

非零退出时保持进程停止，检查退出状态及日志；命令不输出密钥、密文或底层异常内容。事务失败会保留旧封装。若终端输出中断导致提交结果不确定，可以在停机状态下使用预期新密钥执行启动校验；不能把输出缺失推断为未提交并直接重试。

需要回退时，使用既有 `restore` / `recover-switch` 流程恢复本次已验证备份，再将 Provider 主密钥指回旧文件。备份保留当时的 sealed 密文，恢复它需要对应旧密钥；旧密钥应与其历史备份一起纳入受限恢复保管。不得改写历史迁移或删除配置修订来绕过解密校验。

## 当前证据与范围

见 [W05 报告](../reports/w05-embedding-provider.md) 和原始 `rotation-001` 批次：49 项通过，包括监听前拒绝、跨批次重封装、后行损坏导致完整事务回滚、真实备份恢复。该命令及启动校验不代表 W05 的 Console 探测/激活、Generation 切换或真实外部 Provider 门禁已完成。
