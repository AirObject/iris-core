# Iris M1：学习、召回与宿主接口

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.12 及以上。开发设备为 macOS，仓库内 `.venv` 使用 Python 3.13；以下命令也适用于 Linux 和 Windows PowerShell：

```bash
uv sync --python 3.13
uv run pytest
uv run --locked --isolated --python 3.12 pytest
```

两个版本的测试依次运行，不同时运行；3.12 使用临时环境，保持仓库 `.venv` 为 3.13。

可用 `uv run iris setup --name Iris` 创建初始角色。学习命令行可用 `uv run iris ingest messages.jsonl` 接收 UTF-8 消息，再执行 `uv run iris learn <入口标识> --force`。JSONL 每行至少包含 `entry_id`、`platform`、`sender`、`content`、带时区的 ISO `occurred_at` 和入口内唯一的 `dedupe_key`；可选 `kind` 为 `message`、`self_output`、`action_result` 或 `event`。引用可带 `quote_author`、`quote_author_account_id` 和 `quote_content`；场景事件有固定的“场景”主体，行动结果属于“我”。

把 `test-models.example.toml` 复制为仓库根目录的 `test-models.toml` 并填写测试模型配置；该文件被 Git 忽略。在其他 worktree 中设置 `IRIS_TEST_MODELS` 指向同一份配置的绝对路径，不复制密钥文件。2026-10-05 起对话模型为 glm-5.3-flash，embedding 为 doubao-embedding-vision。然后运行：

```bash
uv run iris models check
uv run iris eval learning
```

## 本机服务

```bash
uv run iris setup --name Iris
uv run iris serve
# 也可指定数据库和本机端口：
uv run iris --db data/iris.db serve --host 127.0.0.1 --port 8080
```

默认地址为 `http://127.0.0.1:8080`，交互接口文档在 `/docs`，OpenAPI 在 `/openapi.json`。没有模型配置也能启动并使用全文检索；配置 embedding 后可以融合向量检索。当前没有管理员密码和宿主令牌，只允许监听回环地址（`127.0.0.1`、`::1`、`localhost`）。首次设置、界面、后台调度和通过 HTTP 触发学习在后续 PR 实现。

下面的 Python 示例依次调用接收消息、回复准备和使用反馈；请求与响应均使用 UTF-8 JSON：

```python
import httpx

with httpx.Client(base_url="http://127.0.0.1:8080", timeout=10) as client:
    received = client.post("/api/v1/entries/group-a/messages", json={
        "platform": "chat", "sender": "小林", "account_id": "lin-001",
        "content": "我喜欢桂花乌龙茶", "occurred_at": "2026-09-29T20:00:00+08:00",
        "dedupe_key": "host-message-001", "kind": "message",
    })
    received.raise_for_status()
    print(received.json())  # message_ids、pending_count；重复键返回原消息

    prepared = client.post("/api/v1/entries/group-a/prepare", json={
        "text": "给小林准备什么饮料？", "participants": ["小林"],
        "known_memory_ids": [], "recent_limit": 20,
    })
    prepared.raise_for_status()
    material = prepared.json()
    print(material["persona"], material["memories"], material["recent_messages"])

    # 宿主生成回复后，仅填写确实使用的记忆；不要把所有返回记忆自动当作已使用。
    actually_used = []
    feedback = client.post("/api/v1/feedback", json={
        "recall_id": material["recall_id"], "memory_ids": actually_used,
    })
    feedback.raise_for_status()
```

接收接口也接受上述消息对象的数组，整批提交在一个事务中完成。正文上限为 32768 个 UTF-8 字节；超限返回 413，不截断。首次入口自动创建。接收并不执行学习：本 PR 仍使用离线 `iris learn group-a --force`；请在停止服务后运行学习命令，再启动服务载入索引，不要启动多个进程共同写同一个数据库。

准备结果还包含空的 `state`、未结束的 `goals`、运行 `hints` 和 `recall_id`。查询省略或为 null 时使用最近五条消息。`participants` 接受主体 ID 或无歧义名字／别名，同名账号用 ID；省略或为 null 时从最近 20 条消息推断，按最近发言顺序排列，传空数组不取人物要点。参与者不会排除关于我自己或其他人的记忆。

相关记忆先入选（`reason: "relevant"`），剩余名额按参与者轮流用其重要记忆补位（`reason: "person_highlight"`），要点合计最多三条，不包含我和场景。人物要点是对方的背景，不表示回答了当前问题。文本点名的主体或别名限定范围：记忆须涉及、出自或正文提及此人，同名主体都保留。`recent_limit=0` 关闭近期消息返回，记忆合计最多 8 条、1500 个估算 token，目标最多 10 个。模型失败时说明全文检索降级。定向 search 不补人物要点。

定向查询和状态示例：

```python
import httpx

result = httpx.post("http://127.0.0.1:8080/api/v1/memories/search", json={
    "text": "观星", "people": ["小林"], "kinds": ["计划"],
    "stances": ["亲历"], "time_from": "2026-09-01", "time_to": "2026-10-31",
    "include_forgotten": False, "limit": 8,
}).json()
print(result)
print(httpx.get("http://127.0.0.1:8080/api/v1/status").json())
```

查询可包含遗忘记忆，但读取不会改变其状态；自动遗忘和恢复规则到 M2 实现。反馈有效期为 24 小时，同次召回同条记忆只增加一次保留强度（+8，上限 100），不改变相信程度或修订号。错误码：400 参数／反馈无效、404 不存在／已删除、413 消息过大、503 暂不可用；409 保留用于修订冲突，当前宿主接口不修改记忆正文。

## 评测

```bash
uv run iris eval learning --split dev --judge-runs 1
uv run iris eval learning --judge-runs 2
uv run iris eval recall
uv run iris eval recall --split dev --calibrate
# 如需重新比较 1024／2048 维，再加 --compare-embeddings
uv run iris eval recall --corpus <外部 JSON 或 JSONL> --out <外部目录>
uv run python evals/benchmark_retrieval.py --default-config
```

语料格式、完整外部明细、标定方法和报告见 [evals/README.md](evals/README.md)。学习和召回的最终验收仍需规划者运行隐藏集。

当前回复检索默认 jieba＋2048 维 float32，向量下限 0.35、相对比例 0.75、向量／全文权重 2:1，查询加“为这个问题检索能回答它的个人记忆：”前缀；无向量时用 jieba。新数据库写入这些默认值，已有设置保留。学习材料保持独立的 PR #4 设置。

trigram 查询中的一字／两字实词会补查已有 jieba 索引；只询问已知姓名时，也能按涉及人、说话人及正文提及取回候选。长短词结果合并去重，人物与类型等过滤继续生效，混合检索和全文降级均覆盖短词。

召回评测默认运行四份公开集，共 144 条查询。GLM 阶段全部按 dev 使用，保留原 split 作历史分组；第四轮新增 16 条手写短词查询，先单独冻结，再按原规则在 122 条 dev 上重新标定。选参、逐项对照和已知问题见 [evals/README.md](evals/README.md)。无答案拒绝留到 M2。

点名检查仅读取检索和人物要点候选的正文。性能脚本加 `--default-config` 覆盖当前默认的 5 千／5 万条、点名／不点名四组；不带该参数还会比较维度和 dtype，并在合成测试库中关闭向量截断。性能方法和本次报告入口见 [evals/README.md](evals/README.md)。学习影响由「test: GLM 学习基线与 PR #5 对照」另行测量。
