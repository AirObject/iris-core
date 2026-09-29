# PR #3 执行故障与重试记录

本记录披露最终评测中的服务故障和执行重试。评分 v3、手写语料及学习提示词在故障后都没有修改；没有查看或寻找规划者隐藏集。

## 首次全量执行失败

在 `a111a67` 上执行 `uv run iris eval learning`。命令输出已完成 85/86 段，最后 F010 判分连续总超时，最终退出：

```text
Evaluation failed: judge failed for F010: total timeout
```

原评测器仅在全部案例完成后写报告；异常退出后，临时数据库已清理。没有生成全量报告，也无法恢复这次的完整判分和用量。已经完成的历史 holdout 确实执行过，但其判分没有用于分析、改进或选择结果。

## 单独检查公开 dev

只取冻结 `learning_v3.jsonl` 中原样的 F010，在仓库内 `data/` 临时文件中做服务检查。对话连接检查成功（1179 ms），embedding 连接检查成功（331 ms）。F010 的学习生成成功：finish_reason=stop，输出 8617 token，耗时 87796 ms；判分连续达到 120 秒超时，最终同样失败。

运行时只读检查了这个检查进程自己打开的 F010 数据库，观察到两次已记录的判分超时分别为 120006 和 120003 ms。随后尝试保存数据库时，该进程已经退出，数据库未能备份。没有枚举或读取其他评测进程的数据。

这两次失败执行没有完整报告，其用量不能混入后续成功报告或声称已经完整统计。

## 仅调整判分等待时间

学习请求仍为 120 秒，embedding 仍为 30 秒；仅 `learning_judge` 和 `learning_judge_repair` 总超时改为 240 秒，同时控制 HTTP 超时与 future 总等待。输出额度仍为 16000，不缩减推理，不改评分内容、重试次数和质量门槛。报告明示两类请求的不同超时设置。

使用之前成功保存的公开 dev F010 实际结果单独检查判分可用性，返回完整 JSON：finish_reason=stop，输出 5384 token，耗时 51898 ms。这次没有超过 120 秒，因此不把它当成“必须要 240 秒”的证明；延长等待时间是针对此前两次执行中反复超时的运行容错。

Python 3.12.14 和 3.13.13 的完整测试各 134 passed，涵盖学习 120 秒、判分 240 秒的 HTTP 和总超时，以及原有两次调用内重试。学习和评分文本、全部语料与故障前相同。

## 完整重试与被动保存

为获得完整评测报告，需要重新执行全量。因此本轮历史 holdout 的执行次数超过一次；这是服务故障后的执行重试，未依据 holdout 分数调参或择优。

重试复用正式 `run_learning_eval`、`_run_case`、学习和双次判分代码。外层脚本仅在案例成功返回后保存结果，并在模型网关原有记账后追加调用统计。源码、案例输入和模型配置的指纹必须一致，才复用已完成案例；不会按质量分数决定是否复用。这样后续再有服务故障，也能保留已完成案例，避免反复运行 holdout。

所有本地中间文件在忽略的 `data/` 中，写入前替换配置中的 API key。最终公开报告继续由正式评测器生成，失败执行、单独服务检查及最终报告的耗时／用量分别说明。

以下是本次使用的完整采集脚本，作为执行审计材料；不改变产品学习路径或新增 CLI 参数。

```python
"""Persist completed production evaluation cases; never choose by quality score."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import threading

from iris import evaluation
from iris.cli import main
from iris.models import Gateway, load_test_models

source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
sources = sorted(p for p in Path('src/iris').rglob('*') if p.suffix in ('.py', '.md', '.sql'))
source_hash = hashlib.sha256(b''.join(p.as_posix().encode() + p.read_bytes() for p in sources)).hexdigest()
configs = load_test_models()
model_hash = hashlib.sha256(json.dumps({k: (v.base_url, v.model) for k, v in configs.items()}, sort_keys=True).encode()).hexdigest()
secrets = [v.api_key for v in configs.values() if v.api_key]
directory = Path('data/final-run-verified')
directory.mkdir(exist_ok=True)
original = evaluation._run_case
original_record = Gateway._record
context = threading.local()
trace_lock = threading.Lock()

def save(path, value):
    content = json.dumps(value, ensure_ascii=False, indent=2)
    for secret in secrets:
        content = content.replace(secret, '[REDACTED]')
    path.write_text(content, encoding='utf-8')

def record(configs, case):
    assert case['id'].isalnum()
    context.case_id = case['id']
    case_hash = hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    signature = {'source_sha256': source_hash, 'case_sha256': case_hash, 'model_sha256': model_hash}
    path = directory / (case['id'] + '.json')
    if path.exists():
        cached = json.loads(path.read_text(encoding='utf-8'))
        assert cached['signature'] == signature, 'Completed case inputs changed'
        print('Reuse completed case: ' + case['id'], flush=True)
        return cached['row']
    try:
        row = original(configs, case)
    except Exception as error:
        save(directory / (case['id'] + '-failure.json'), {'case': case['id'], 'signature': signature,
             'failed_at': datetime.now(timezone.utc).isoformat(), 'error': str(error)})
        raise
    save(path, {'signature': signature, 'source_commit': source_commit,
                'completed_at': datetime.now(timezone.utc).isoformat(), 'row': row})
    return row

def record_call(self, purpose, model, duration_ms, category, error, usage=None, flags=None,
                status_code=None, *, finish_reason=None, batch_id=None):
    original_record(self, purpose, model, duration_ms, category, error, usage, flags,
                    status_code, finish_reason=finish_reason, batch_id=batch_id)
    item = {'case': getattr(context, 'case_id', None), 'purpose': purpose,
            'duration_ms': duration_ms, 'result_category': category, 'error_summary': error,
            'finish_reason': finish_reason, 'completion_tokens': (usage or {}).get('completion_tokens')}
    content = json.dumps(item, ensure_ascii=False)
    for secret in secrets:
        content = content.replace(secret, '[REDACTED]')
    with trace_lock, (directory / 'calls.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(content + '\n')

Gateway._record = record_call
evaluation._run_case = record
raise SystemExit(main(sys.argv[1:] or ['eval', 'learning']))

```

## 重试中的耗时观察

固定版本 `1466c49` 的完整重试中，L007 一次判分成功耗时 153871 ms；F010 第一次判分成功耗时 189312 ms、输出 10905 token，均为 finish_reason=stop。两次都超过原 120 秒限制，并在新的 240 秒限制内返回完整结果，支持只延长判分等待时间的决定。F010 的学习请求成功耗时 105130 ms，学习 120 秒限制保持不变。

默认 CLI 仍在所有案例成功后才写完整报告，本次采集脚本没有成为产品的暂停或自动恢复功能。超时未得到服务商 usage 的调用保留空值；token 汇总只能覆盖已返回的用量，不代表完整账单。

## 完整结果

重试在同一进程中完成全部 86 段、203 批，未再次重跑任何已完成案例，耗时 2612.6 秒（43.5 分钟）。最终 F010 第二次判分耗时 229378 ms、输出 11928 token，finish_reason=stop；学习和判分均无超时。学习无长度截断；L029 有一次判分截断（16000 token），通过既有 JSON 修正完成。V033 第一批首轮没有 JSON 对象，模型修正成功；最终学习直接解析率 99.5%，总解析率 100%。

最终报告：[Markdown](reports/learning-20260929T072058342800Z-all.md) · [JSON](reports/learning-20260929T072058342800Z-all.json)。汇总指标与保存的全部 86 份正式路径结果逐项一致，语料 SHA-256 与冻结输入一致，60 项双判分歧逐项一致。记录生成后没有修改学习、评分或语料。全部及历史 holdout 达到仓库三项 M1 数值门槛；独立隐藏验收仍待规划者运行。
