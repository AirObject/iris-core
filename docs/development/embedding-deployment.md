# Embedding Provider 受控部署配置

适用：W05 候选 Core 0.15.0 / Schema22，[ADR-0049](../adr/0049-embedding-provider-lifecycle.md)。配置、探测与激活内容保存在数据库不可变修订中；本文件只声明部署允许读取哪些租户秘密、允许向哪里发送请求。

## 启用与一致装配

准备属主为 API/Worker 运行用户、权限不宽于 0600 的普通 JSON 文件（最多 64 KiB），使用绝对路径；拒绝符号链接、硬链接及 FIFO。示例里的租户标识须换为真实库中的租户，环境变量名由部署登记，不在文件中保存明文凭据。

```json
{
  "schema_version": 1,
  "secret_references": {
    "tenant-a": ["env:IRIS_EMBEDDING_TENANT_A"]
  },
  "outbound": {
    "allowed_hosts": ["api.example.org"],
    "max_request_bytes": 1048576,
    "max_response_bytes": 8388608,
    "connect_timeout_seconds": 2,
    "read_timeout_seconds": 2,
    "max_total_seconds": 10
  }
}
```

API 与所有 Worker 使用相同文件、数据库、私有向量根目录，以及适用的 Provider 主密钥路径：

```bash
iris-memory-core serve --config /etc/iris/service.toml \
  --provider-config-file /etc/iris/provider.json \
  --vector-root /srv/iris/data/vector

iris-memory-core worker --config /etc/iris/service.toml \
  --provider-config-file /etc/iris/provider.json \
  --vector-root /srv/iris/data/vector
```

同名 TOML `[service]` 字段 `provider_config_file`、`vector_root` 及环境变量 `IRIS_MEMORY_PROVIDER_CONFIG_FILE`、`IRIS_MEMORY_VECTOR_ROOT` 等价；优先级为 CLI > 环境 > TOML。需要强制向量可用的部署配置 `vector_required=true` 或 `--vector-required`。使用 sealed 时同时配置 `secret_key_file` / `IRIS_MEMORY_SECRET_KEY_FILE` / `--secret-key-file`，遵守[独立 Provider 主密钥与离线轮换](provider-secret-rotation.md)。

文件在进程启动时读取；改变引用允许表或出站策略后，更新并重启全部 API/Console/Worker，避免进程使用不同策略。替换 secret_ref 文件内容后，后续绑定读取新材料；已有请求保留自己的凭据实例。环境变量由进程继承，运营者改变启动环境后应重启对应进程。Provider 主密钥轮换必须使用停机、备份和重封装流程，不能仅替换文件。

挂载和独立监听的 Console 使用 API 已构造的运行时；常规 vector.apply/rebuild/cleanup 与 Provider 探测/激活 Worker 使用同一部署解析入口。第一次尚无服务代时，Recall 保留明确 vector_rebuild_pending，Required readiness 不就绪；不会隐式改用 deterministic。

## 允许表与出站边界

每个引用只允许登记给一个租户；支持精确 `env:NAME` 和 `file:/absolute/path`，不支持通配。最多 256 租户、每租户 64 引用、全表 1024 引用。Provider 主密钥、Console 认证密钥和部署 JSON 本身不作为可解析的 Provider 凭据引用。

`outbound` 支持示例中的字段，以及 `allowed_private_networks`（精确 CIDR 数组）和 `allow_loopback`（布尔值）。hostname/CIDR 数组各最多 256 项。省略 hostname 列表表示允许通过地址校验的公网 HTTPS；生产建议登记目标 hostname。私网默认拒绝，显式 CIDR 仍不能放宽链路本地、元数据、组播等固有拒绝规则。

回环 HTTP 需要文件显式 `allow_loopback=true` **并且**服务显式 `development_embedding=true` / `--development-embedding`。这两个开关用于开发隔离服务器；配置部署文件后，开发开关只允许选择开发适配器，不会自动创建 deterministic 服务配置。生产保持关闭。

字节与超时仍受传输层硬上限约束；布尔值不能代替数字、NaN/无限值无效、重复 JSON 键和未知字段拒绝。Console 请求不能修改这些边界。启动错误仅报告配置无效，不回显原文、路径、秘密或 Provider 响应。

## 当前验证与剩余范围

`deployment-001` 已通过 22 项：真实受限文件及格式负例、明确开发开关、环境/TOML 选择，以及实际 CLI Worker 子进程完成固定探测和 FAISS 激活，原有 ASGI 应用随后使用该配置真实查询。更多证据与剩余 HTTP/Console 浏览器及真实允许 Provider 验收见 [W05 报告](../reports/w05-embedding-provider.md)。这些局部验证不代表 W05 已完成。
