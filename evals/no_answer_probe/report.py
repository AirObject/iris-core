"""Render aggregate reports/plots; raw queries and model outputs stay external."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

FAMILIES={'baseline':'现默认','cosine':'候选余弦阈值','coverage':'全文覆盖率','agreement':'两路一致',
    'margin':'最高余弦＋前两名差距','normalized':'按查询归一化余弦','cosine_or_coverage':'余弦或覆盖率',
    'blend':'信号加权','glm-low-k4':'GLM low/K4','glm-low-k8':'GLM low/K8','glm-high-k8':'GLM high/K8',
    'combination':'确定性预筛＋模型'}
METRICS=('recall_at_8','ndcg_at_8','irrelevant_return_rate','relevant_precision','average_returned')


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def fmt(x):return '—' if x is None else f'{x:.4f}'
def fp(m):return f"{round(m['irrelevant_return_rate']*m['no_answer'])}/{m['no_answer']} ({m['irrelevant_return_rate']:.4f})" if m['no_answer'] else '—'
def quality(t):return (t['metrics']['recall_at_8']+t['metrics']['ndcg_at_8'])/2


def table(rows):
    lines=['| 方法／范围 | Recall@8 | nDCG@8 | 无答案误返 | relevant 精确率 | 平均返回 |',
           '| --- | ---: | ---: | ---: | ---: | ---: |']
    for label,m in rows:
        lines.append(f"| {label} | {fmt(m['recall_at_8'])} | {fmt(m['ndcg_at_8'])} | {fp(m)} | {fmt(m['relevant_precision'])} | {fmt(m['average_returned'])} |")
    return '\n'.join(lines)


def main(root, report_dir, plots):
    out=root/'run-v1';summary=read(out/'summary.json');capture=read(out/'capture.json');protocol=read(out/'protocol.json')
    live=read(out/'live-check-summary.json');paced=read(out/'rate-check-summary.json') if (out/'rate-check-summary.json').exists() else None
    reps=summary['representatives'];chosen=summary['selected'];trials=summary['trials'];baseline=reps['baseline']
    report_dir.mkdir(parents=True,exist_ok=True);stem='m2-no-answer-probe-20261009'
    csv_path=report_dir/(stem+'-metrics.csv')
    with csv_path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f,lineterminator="\n");writer.writerow(['candidate','family','parameters','scope','queries','answerable','no_answer',*METRICS,'relevant_correct','relevant_returned','calls','fallbacks','observed_call_p50_ms','observed_call_p95_ms','prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens'])
        for t in trials:
            for scope,m in t['groups'].items():
                writer.writerow([t['id'],t['family'],json.dumps(t['params'],ensure_ascii=False,sort_keys=True),scope,
                    m['queries'],m['answerable'],m['no_answer'],*(m[k] for k in METRICS),m['relevant_correct'],m['relevant_returned'],
                    *([t['calls'],t['fallbacks'],t['p50_ms'],t['p95_ms'],*(t['usage'][k] for k in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens'))] if scope=='all' else ['']*8)])
        for label,r in [('live-check-10s',live),('rate-check-10s',paced)]:
            if not r:continue
            for scope,m in r['groups'].items():
                writer.writerow([label,'verification',json.dumps(r['parameters'],sort_keys=True),scope,m['queries'],m['answerable'],m['no_answer'],
                    *(m[k] for k in METRICS),m['relevant_correct'],m['relevant_returned'],*([r['calls'],r['fallbacks'],r['p50_ms'],r['p95_ms'],*(r['usage'][k] for k in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens'))] if scope=='all' else ['']*8)])
    with (report_dir/(stem+'-regressions.csv')).open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f,lineterminator="\n");w.writerow(['candidate','corpus','corpus_recall_drop','regressed_query_ids'])
        for t in [*trials,{'id':'live-check-10s','regressions':live['regressions']},*([{'id':'rate-check-10s','regressions':paced['regressions']}] if paced else [])]:
            for corpus,reg in t['regressions'].items():
                w.writerow([t['id'],corpus,reg['recall_drop'],','.join(q['id'] for q in reg['queries'])])
    calls={}
    import numpy as np
    for lane in ('low-k4','low-k8','high-k8'):
        actual=[read(p) for p in (out/'calls'/lane).glob('*.json') if read(p)['status']!='skipped_empty']
        calls[lane]={'count':len(actual),'statuses':dict(Counter(r['status'] for r in actual)),
            'p50_ms':float(np.percentile([r['duration_ms'] for r in actual],50)),
            'p95_ms':float(np.percentile([r['duration_ms'] for r in actual],95)),
            'max_ms':max(r['duration_ms'] for r in actual),
            'usage':{k:sum(r.get('usage',{}).get(k,0) for r in actual) for k in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens')}}
    plot_path=report_dir/(stem+'-curves.png')
    if plots:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})
        fig,axes=plt.subplots(1,2,figsize=(12,5.2),layout='constrained')
        curves=[('Deterministic frontier',[t for t in trials if t['id'].startswith('d')]),
            ('GLM low / K4',[t for t in trials if t['family']=='glm-low-k4' and t['params']['budget']==60]),
            ('GLM low / K8',[t for t in trials if t['family']=='glm-low-k8' and t['params']['budget']==60]),
            ('GLM high / K8',[t for t in trials if t['family']=='glm-high-k8' and t['params']['budget']==60]),
            ('Gate .45/.50 + high',[t for t in trials if t['family']=='combination' and t['params']['gate']==[.45,.5] and t['params']['effort']=='high' and t['params']['budget']==60])]
        for ax,scope,title in zip(axes,('all','conversation'),('All 228 public dev queries','88 conversation-prepare queries')):
            for label,points in curves:
                best={}
                for t in points:
                    m=t['groups'][scope];x=m['irrelevant_return_rate'];y=m['recall_at_8']
                    best[x]=max(best.get(x,0),y)
                ordered=sorted(best.items());frontier=[];top=-1
                for x,y in ordered:
                    if y>top:frontier.append((x,y));top=y
                ax.plot([p[0] for p in frontier],[p[1] for p in frontier],marker='o',markersize=4,label=label)
            m=baseline['groups'][scope];ax.scatter(m['irrelevant_return_rate'],m['recall_at_8'],c='black',marker='x',s=65,label='Current default',zorder=5)
            m=chosen['groups'][scope];ax.scatter(m['irrelevant_return_rate'],m['recall_at_8'],c='#C51B7D',marker='*',s=150,label='Selected (10s replay)',zorder=6)
            ax.axvline(.1,color='#888888',linestyle='--',linewidth=1);ax.axhline(.85,color='#888888',linestyle='--',linewidth=1)
            ax.set(xlabel='No-answer false-return rate',ylabel='Recall@8',title=title,xlim=(-.02,.85),ylim=(-.02,1.035));ax.grid(alpha=.16)
        handles,labels=axes[0].get_legend_handles_labels();fig.legend(handles,labels,loc='outside lower center',ncol=3,frameon=False)
        fig.suptitle('Answerability filtering: recall / false-return trade-off\n60s model curves; empirical points, no interpolation claims; nDCG gate also required',fontsize=12)
        fig.savefig(plot_path,dpi=180)
        svg_path=report_dir/(stem+'-curves.svg');fig.savefig(svg_path);plt.close(fig)
        svg_path.write_text('\n'.join(line.rstrip() for line in svg_path.read_text().splitlines())+'\n',encoding='utf-8')
    freeze='7d696a7d1145b701edbe7431e3e5ab4539c6e415'
    corpus_sha=protocol['corpus_sha256']['recall_no_answer_v1']
    notes_sha=hashlib.sha256((Path(__file__).resolve().parents[1]/'recall_no_answer_v1_notes.md').read_bytes()).hexdigest()
    lines=[
        '# M2 无答案 dev 集与拒绝方法探测（2026-10-09）', '',
        '实验在 2026-10-08 手写并冻结语料，2026-10-09 完成复核。产品默认、学习代码、数据库设置默认值、设计、DECISIONS、AGENTS 和 evals/README 均未修改。', '',
        f"预定规则在 550 个候选中找到 {summary['feasible_count']} 个公开 dev 可行候选，选择 **`{chosen['id']}`**：GLM 5.3 Flash high、前 8 条 relevant 候选、support≥50、独立总超时 10 秒。首次完整对照 Recall@8={chosen['metrics']['recall_at_8']:.4f}、nDCG@8={chosen['metrics']['ndcg_at_8']:.4f}、误返 {fp(chosen['metrics'])}、精确率 {chosen['metrics']['relevant_precision']:.4f}。各语料 Recall 降幅均未超过 0.05。", '',
        '**运行限制须一起看**：紧接着的并发 2、真实 10 秒复核遭遇 86 次 HTTP 429，按原召回降级后误返变成 18/66（0.2727）。这次故障结果完整保留，没有只补跑失败项。串行低速全量复核见下文。正常服务下的语义效果与出现 HTTP 429 时的可用性分别报告，不能宣称当前默认或 M2 已通过。隐藏门槛由规划者另行运行。', '',
        '## 冻结、输入与实验边界', '',
        f'- 基线：`fcf7d57`（docs: M2 计划），新 worktree `iris-core-m2-no-answer`，分支 `eval/m2-no-answer-probe`。语料单独冻结提交：`{freeze}`。',
        f'- `recall_no_answer_v1.json` SHA-256：`{corpus_sha}`。',
        f'- `recall_no_answer_v1_notes.md` SHA-256：`{notes_sha}`。',
        '- 新集手写 48 条记忆／40 条 dev 查询：22 无答案、18 有答案；显式 20（10/10）、对话准备 20（8/12）；私聊 15、群聊 14、直播 11。逐条标注复核理由在冻结 notes。冻结后两文件未改。',
        '- 合并七份语料：243 条固定记忆、228 条查询、162 有答案、66 无答案；对话准备 88 条、60 有答案、28 无答案。各语料独立建库；v2 原 22 条 holdout 仅是历史标记，全部公开数据参与 dev 选择。未寻找或读取隐藏集。',
        f"- 真实 embedding：doubao-embedding-vision，显式 2048 维 float32；383 个输入按端点／模型／维度／全文哈希复用已知公开缓存，新增 88 次。七集查询组成、锚点、排序和预算保持现默认；诊断子类逐查询断言 ID／顺序／reason 与普通 Retrieval 完全一致。原六集复现 26/44 误返。",
        f"- 产品源码 SHA-256（相对文件路径→SHA 的规范 JSON）：`{protocol['source_sha256']}`；执行驱动 SHA-256：`{protocol['probe_sha256']}`；判断提示词 SHA-256：`{protocol['prompt_sha256']}`。完整协议在外部 `run-v1/protocol.json`。", '',
        '所有方法只过滤现默认已经选出的 relevant，保留顺序和已有 person_highlight，不补位、不将拒绝项改标成人物要点。K 是 relevant 数，不扩候选池；本轮 141 条查询有 1 项、30 条有 2 项、20 条有 3 项、6 条有 4 项、3 条有 5 项，28 条没有 relevant。因而 K4/K8 的输入只在 3 条查询上不同，其余差异也包含模型重复调用的波动。', '',
        'Recall/nDCG 按有答案查询宏平均，计所有返回；误返只看无答案查询是否有 relevant；relevant 精确率按返回项微平均。空分母为空，不视为完美。人物要点可留在平均返回数中。评分完全由冻结 relevant 标签和正式 recall_metrics 计算，GLM 是被测的过滤器，不给评测判分。', '',
        '## 方舟可用能力调查', '',
        '| 探测 | 当前配置实测 | 结论边界 |',
        '| --- | --- | --- |',
        '| GLM 5.3 Flash / chat/completions | low、high 均 HTTP 200，正文完整；low/K4、low/K8、high/K8 三组各 200 次成功 | 当前已配置凭据可用，未关闭推理 |',
        '| doubao-embedding-vision / embeddings | 88 次新增成功，2048 维 | 继续使用既有向量模型 |',
        '| 当前 /api/plan/v3/models | HTTP 404 | 该端点不能枚举账号可见模型，不能据此推断整账号的所有权限 |',
        '| 当前 /api/plan/v3/rerank：doubao-seed-rerank、base-multilingual-rerank、m3-v2-rerank | 三次均 HTTP 404 | 未找到当前配置可调用的专用 rerank；404 是路由层证据，不是分别证明三个模型未授权 |', '',
        '公开 [VikingDB Rerank 文档](https://docs.volcengine.com/docs/vector_database_vikingdb/Rerank?lang=zh) 列出 `/api/knowledge/service/rerank` 和专用重排模型，描述为文档回答 query 的概率，并有独立服务／签名鉴权约定；这不是本机已配置的 Plan 数据面。没有读取其他凭据、创建云资源或跨服务复用密钥，所以本轮无专用 rerank 质量／延迟数值，不把未测写成效果差。当前可用的方舟内判断方案是 GLM；若以后试 VikingDB，须另行配置其服务与授权。', '',
        '当前端点是 Agent Plan。官方 [Plan 用量规则](https://docs.volcengine.com/docs/ark/agent-plan-personal-afp-credits-billing-rules?lang=zh) 列出 GLM 5.3 Flash 与 embedding 的输入／输出抵扣系数均为 0.5；下面按已报告 token 估算 AFP。公开模型目录不等于账号权限清单，本报告只对实际调用成功的两种模型作可用结论。', '',
        '## 方法、参数和选择', '',
        '99 个确定性网格覆盖余弦、非姓名词项覆盖、两路一致、最高余弦与差距、相对余弦、余弦或覆盖及加权分数。没有领域／话题词表，姓名沿用锚点，不新增姓名分。模型实际预算 60 秒、4096 输出 token、temperature=0、并发 2，每条一次，无重试／JSON 修正。GLM low/K4、low/K8、high/K8 为冻结的候选组顺序；实际按查询交错发送三组请求；每组 200 次实际调用、28 条空候选跳过。分数门槛 `{1,25,50,75,90,100}`，回放预算 `{2,5,10,20,60}` 秒。', '',
        '组合先按“最高 relevant 候选余弦≥c 或覆盖≥l”整条开／关，c∈{.45,.55,.65}，l∈{.5,.75}，再用 low/K8 或 high/K8。通过时模型请求完全相同，所以可精确复用同一次真实调用；这里的组合质量和节省调用数是回放，没有伪称新做独立调用。失败、拒绝、截断、非法 JSON／分数／ID、超时全部退回完整原召回。', '',
        '候选顺序、每个网格和公式见 [实验说明](../no_answer_probe/README.md) 及外部 protocol；协议在看成绩前冻结。首次协议往返把 Python tuple 与 JSON list 误判为不同，在任何质量调用前修复并补回归测试；原协议仍留在外部父目录，正式材料统一在 run-v1，没有拼接两个实验。', '',
        f"选择严格按 M2：先 R≥.85、N≥.75、F≤.10；全体可行候选最高 Q={max(quality(t) for t in trials if t['metrics']['recall_at_8']>=.85 and t['metrics']['ndcg_at_8']>=.75 and t['metrics']['irrelevant_return_rate']<=.1):.6f}，距最高 Q 严格小于 .01 才进入持平组，再精确率、低误返、冻结顺序。选中 Q={quality(chosen):.6f}。10/20/60 秒对选中阈值返回完全相同，固定顺序选择 10 秒；5 秒有 9 次回退，精确率更低。", '',
        '下面每个方法族按相同规则选代表。没有可行者用 R/N 合格范围内最低误返；两路一致没有任何 R/N 合格项，表中是最高 Q 诊断项，不能当成可行。完整 550 个候选及全部分组数值（含曲线数据）见 CSV，不仅保留族内赢家。', '',
        table([(FAMILIES[f]+' · '+t['id'],t['metrics']) for f,t in reps.items()]), '',
        '### 对话中准备（88 条）', '',
        table([(FAMILIES[f]+' · '+t['id'],t['groups']['conversation']) for f,t in reps.items()]), '',
        '### 显式查询（140 条）', '',
        table([(FAMILIES[f]+' · '+t['id'],t['groups']['explicit']) for f,t in reps.items()]), '',
        '确定性方法在 R/N 达标约束下最低误返为 30/66（0.4545），距离 ≤0.10 仍差 0.3545；至少还要消除 24 条误返才到不超过 6/66。该代表还严重损伤短词集，不能仅用合并 Recall 刚过线就接受。模型组合节省了一些请求，但预筛又丢掉低余弦的正确短词／省略查询，规则没有选它。', '',
        '## 各语料结果与退化', '',
        table([(corpus+' · '+FAMILIES[f],t['groups'][corpus]) for corpus in protocol['corpora']
               for f,t in reps.items()]), '',
        '选中方案没有任何语料 Recall 降幅超过 0.05。仍有一条局部损失：conversation_v1/T11 的 C09（grade=1）被拒绝，C10（grade=3）保留，单题 Recall 1→0.5；该组 Recall 0.9444→0.9167。C09 是白榆照料植物的背景经验，C10 直接回答白果托管菜园的结果。保留冻结弱相关标签和实际损失，不将其改成无关追分。唯一无答案误返是 recall_v1/Q36：输入陈述观星安排，模型仍认为 M05 的携带望远镜承诺可补充；没有为它加陈述句或词表特判。recall_v1 单组误返为 1/8，M2 门槛按七集合并。', '',
        '所有候选中，各语料 Recall 降幅超过 0.05 时的全部损失查询 ID，按候选／语料聚合列于回归 CSV。逐题前后 Recall、丢失记忆 ID 与完整返回留在外部 summary.json 和 results/。没有达到该降幅的语料不列；这份表涵盖全部网格。', '',
        '| 代表 | Recall 降幅 >0.05 的语料与损失查询 |', '| --- | --- |']
    for family,t in reps.items():
        desc='；'.join(c+f" (−{reg['recall_drop']:.4f}): "+', '.join(q['id'] for q in reg['queries']) for c,reg in t['regressions'].items()) or '无'
        lines.append(f"| {FAMILIES[family]} / {t['id']} | {desc} |")
    lines.extend(['', '## 方法族代表的判断资源', '',
        '| 方法 | 实际请求子集数 | 预算回退 | 观测 P50 ms | 观测 P95 ms | 输入 token | 输出 token | 推理 token | 输入缓存 token |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for family,t in reps.items():
        u=t['usage']
        lines.append(f"| {FAMILIES[family]} / {t['id']} | {t['calls']} | {t['fallbacks']} | {fmt(t['p50_ms'])} | {fmt(t['p95_ms'])} | {u['prompt_tokens']} | {u['completion_tokens']} | {u['reasoning_tokens']} | {u['cached_tokens']} |")
    lines.extend(['', '确定性过滤不新增模型调用。组合保留 176/200 次判断请求，节省 24 次（12%），输入／输出用量来自对应实际调用子集；它不是另一次真实消费，不计入总账。这里的分位数是 60 秒实际请求观测值；预算回退为候选预算的回放结果。', ''])
    lines.extend(['','## 取舍曲线与完整汇总数据','',
        f'![误返率与 Recall 曲线]({stem}-curves.png)', '',
        '图上模型曲线使用 60 秒实际调用、不同支持分阈值；确定性为二维 Pareto 前沿，组合为选中预筛配置的曲线。星号是规则选中的 10 秒回放。连线仅连接离散候选，不代表中间阈值经过实测；nDCG 仍单独参与门槛。', '',
        f'- [全部候选、七语料及全部／对话／显式分组的指标与曲线数据]({stem}-metrics.csv)',
        f'- [全部候选的 >0.05 语料退化及查询 ID 汇总]({stem}-regressions.csv)',
        f'- [可缩放曲线 SVG]({stem}-curves.svg)', '',
        '指标 CSV 的质量列按 scope 分组；调用次数、回退、延迟和用量只填 all 行，其他行留空，避免把全组消耗误认为某一语料的消耗。每个阈值／预算是同一实际调用的回放，不可将 550 个候选的 token 相加当成本次账单。', '',
        '## 耗时、用量、费用与超时降级', '',
        '| 实际调用组 | 请求数 | P50 ms | P95 ms | 最长 ms | 输入 token | 输出 token | 其中推理 token | 其中输入缓存 token | 估算 AFP |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for lane,r in calls.items():
        u=r['usage'];lines.append(f"| {lane} | {r['count']} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | {r['max_ms']:.1f} | {u['prompt_tokens']} | {u['completion_tokens']} | {u['reasoning_tokens']} | {u['cached_tokens']} | {(u['prompt_tokens']+u['completion_tokens'])*.5/10000:.4f} |")
    for label,r in [('真实 10 秒复核（并发 2）',live),('真实 10 秒复核（串行低速）',paced)]:
        if not r:continue
        u=r['usage'];lines.append(f"| {label} | {r['calls']} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | — | {u['prompt_tokens']} | {u['completion_tokens']} | {u['reasoning_tokens']} | {u['cached_tokens']} | {(u['prompt_tokens']+u['completion_tokens'])*.5/10000:.4f} |")
    embed_tokens=sum(r.get('prompt_tokens') or 0 for r in capture['embedding_calls'])
    total_in=sum(r['usage']['prompt_tokens'] for r in calls.values())+live['usage']['prompt_tokens']+(paced['usage']['prompt_tokens'] if paced else 0)
    total_out=sum(r['usage']['completion_tokens'] for r in calls.values())+live['usage']['completion_tokens']+(paced['usage']['completion_tokens'] if paced else 0)
    total_cached=sum(r['usage']['cached_tokens'] for r in calls.values())+live['usage']['cached_tokens']+(paced['usage']['cached_tokens'] if paced else 0)
    lines.extend(['',f'新增 embedding：88 次，已报告输入 {embed_tokens} token；本次未对复用的 383 个向量再收费。上表真实主实验／复核已报告 chat 输入 {total_in}、输出 {total_out} token；AFP 估算 `(输入+输出)×0.5/10000`，加新 embedding 合计 {(total_in+total_out+embed_tokens)*.5/10000:.4f} AFP。接口能力两次短 chat 探测共 124 token、429 后一次恢复诊断 42 token，另约 0.0083 AFP。', '',
        '这是按公开 Plan 系数估算的额度消耗，不是实际账单，也没有读取账户套餐档位／余额。HTTP 失败或提前取消时服务商未报告的 token 无法计算；原始 usage 均在外部。若只作按量单价比较，官方 [模型价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh&redirect=1) 为 GLM 未缓存输入 0.80／缓存输入 0.23／输出 2.80 元每百万 token，embedding 文本 0.70；扣除已报告输入缓存差价后，本次已报告用量的按量等值约 '+f'{((total_in-total_cached)*.8+total_cached*.23+total_out*2.8+embed_tokens*.7)/1e6:.4f} 元'+ '，不能据此声称 Plan 实际扣款。', '',
        '推理 token 已包含在输出 token 内，缓存 token 已包含在输入 token 内，均不重复累加。AFP 按当前公开抵扣公式计，未自行假设缓存折扣；按量等值仅作另一计价方式的参考，不含没有发生的显式缓存存储费用。', '',
        '每种模型阈值／超时组合的完整指标、请求数和回退数在指标 CSV。模型 P50/P95 为实际请求开始至响应校验结束，不含实验 semaphore 排队与主动节流；不把快速 HTTP 429 拉低的延迟理解为服务更快。组合的耗时分位数按实际需要调用的同一批请求子集计算。离线超时回放并未真取消请求，不能把其 usage 当成提前取消后的费用。', '',
        '### high/K8、support≥50 的预算回放', '',
        '| 预算秒 | 回退次数/200 | Recall@8 | nDCG@8 | 误返 | 精确率 |', '| ---: | ---: | ---: | ---: | ---: | ---: |'])
    for t in trials:
        if t['id'].startswith('high-k8-s50-'):
            m=t['metrics'];lines.append(f"| {t['params']['budget']} | {t['fallbacks']} | {fmt(m['recall_at_8'])} | {fmt(m['ndcg_at_8'])} | {fp(m)} | {fmt(m['relevant_precision'])} |")
    lines.extend(['','### 真实 10 秒截止时间复核','',
        '第一轮原样重跑全部七集，仍并发 2、不重试；114 次成功、86 次 HTTP 429、无真正超时，28 条空候选跳过。运行约 '+f"{live['elapsed_seconds']:.1f}"+' 秒。原实验只记 HTTP 状态，未保存这些错误的细分 code，所以不猜测具体 RPM／TPM／套餐限额。一次最短恢复诊断为 HTTP 200；后续复核增加安全的 error.code／Retry-After 记录。', '',
        table([('首次选择（60 秒调用按 10 秒回放）',chosen['metrics']),('并发 2 实际 10 秒／全部',live['groups']['all']),('并发 2 实际 10 秒／对话',live['groups']['conversation'])]), ''])
    if paced:
        lines.extend(['第一轮错误没有丢弃。完成离线检查、让限流窗口恢复后，预先记录串行、每次结束至少间隔 1 秒、同一提示词／候选／support≥50／10 秒总预算，重新调用所有公开查询（不是只补失败项），不把第二轮加入 550 个候选池重新挑选。', '',
            f"第二轮实际请求 {paced['calls']} 次，状态 {json.dumps(paced['status_counts'],ensure_ascii=False)}，回退 {paced['fallbacks']} 次；约 {paced['elapsed_seconds']:.1f} 秒。与原回放返回有 {len(paced['differences_from_replay'])} 条差异，全部保存在外部，不用更高分的一轮覆盖另一轮。", '',
            table([('串行实际 10 秒／全部',paced['groups']['all']),('串行实际 10 秒／对话',paced['groups']['conversation']),('串行实际 10 秒／显式',paced['groups']['explicit'])]), ''])
    if paced:
        lines.extend(['串行复核只有 recall_no_answer_v1/NA03 与首次回放不同：除 N09 外额外留下运输防碰撞的 N41。按冻结标签它是不正确 relevant，因此精确率降为 0.9822；不改标注。两轮 Recall、nDCG、无答案误返完全一致，且均无单语料 Recall 下降超过 0.05。串行加 1 秒间隔只是本轮可用节奏，不能据此保证账号长期限额或宣称消除了所有 429。', ''])
    lines.extend(['### 纯全文降级（单列，不作门槛）','',table([(scope,m) for scope,m in summary['fulltext'].items()]), '',
        '模型判断未配置、不可用、错误或超时，实验模拟完整退回原召回；运行提示需在后续产品实现中加入；若 embedding 也失败，则使用本表已有纯全文路径。本次没有把判断模型的失败处理改成“返回空”，也没有把纯全文成绩当混合门槛。', '',
        '## 需要的产品改动和规划者决定', '',
        '1. 正常服务条件下推荐独立可选的“召回判断／重排模型”用途，首个适配器复用 GLM Chat API；显式保存 model、reasoning_effort=high、support=50、K=8、独立 10 秒总预算。该模型只判断结构化候选，不生成角色回复。学习／试用 chat 用途不共用暂停开关；需要单独健康状态、用量、延迟、错误码和降级提示。未配置仍回原召回。',
        '2. 先按实验边界实现末段过滤，拒绝项不变为人物要点，不扩池、不改变排序、不自动补位。调用在数据库事务之外；网络等待后应重新核对记忆修订、生命周期和已知上下文，过期结果不能直接入召回记录。JSON 数量、ID、顺序、整数范围严格校验，任何失败返回原始结果并标记降级。',
        '3. 429 实测说明单纯“有超时”不够。实现需沿用服务商 error.code 优先分类，加入用途独立的并发／暂停和恢复探测，以及同账号共享的调用限额控制；排队时间也要有边界；达到限制时及时降级，不能继续密集发送。失败降级后不承诺无答案误返≤0.10；报告必须同时列正常路径与降级占比。当前没有证据可以写死账号 RPM／TPM 数值。',
        '4. 10 秒由预定候选规则选择，网络 P95 约 4.5 秒。是否接受这段回复准备等待，及是否给宿主更短的自选预算，由规划者／用户决定；2 秒回放误返 0.50，5 秒有 9 次回退，不能直接沿用 embedding 的 2 秒预算宣称同等质量。',
        '5. 设计 11.2 现写“回复准备不调用生成模型”，若允许 GLM 做结构化判断，应由规划者与 2026-10-08 的决定对齐其措辞；本 PR 不改设计。设置页和外部模型配置后续增加新用途。专用 VikingDB 或第二供应商均不在本次实现范围；方舟内 GLM 已有可行语义候选，暂没有必须换供应商的证据。',
        '6. 本轮低档位的主要损失在短词 search：提示词没有显式携带 mode/filters（结构筛选已由检索执行），low 有时将短关键词判为没有完整问题。若实现传入模式或调整提示词，须重新跑 dev；不要添加领域词表。高档位本轮保留全部短词答案。候选最大只有 5 条，生产 8 条复杂候选的延迟／输出上限也需再测。', '',
        '## 本地性能、学习隔离与验证', '',
        '本实验没有修改 src、迁移或学习提示词，真实模型评测期间未修改运行源码或重建虚拟环境。尽管无需共享模块对照，仍按 M2 规则运行 compare_learning_requests.py，用固定 main 提交的 archive 与本 worktree 逐批比较全部 86 段公开学习语料。完整请求在外部 learning-requests，结果见验证记录。', '',
        'R10 的外部模型网络耗时按 2026-10-08 决定不计入 500ms，但要单列。基线使用官方 benchmark_retrieval.py --default-config 测 5 千／5 万条、点名／不点名四组；这里不能将小语料的约 4ms 或额外追踪子类的耗时当成生产规模性能。产品集成尚未实现，最终含新快照／修订校验／状态逻辑的 R10 仍需在实现 PR 复测。', '',
        '<!-- VALIDATION -->', '',
        '## 完整材料与复现索引', '',
        f'外部根目录：`{root}`。', '',
        '- account-probe.json、rate-diagnostic.json：安全接口状态与恢复诊断；没有密钥或原始错误正文。',
        '- run-v1/protocol.json、capture.json、recall-embeddings.db、databases/：冻结协议、完整七集输入／特征／基线、隔离数据库和严格键向量缓存。',
        '- run-v1/calls/：三组各 228 条记录（200 真实＋28 空候选），原始响应正文、usage、延迟、输入指纹；不保存推理全文。',
        '- run-v1/results/、summary.json：550 个候选完整逐查询返回、reason、各分组和退化。',
        '- run-v1/live-check/、live-check-summary.json、live-check-v1.py：并发复核所有成功和 429 失败、降级结果及当时脚本。',
        '- run-v1/rate-check/、rate-check-protocol.json、rate-check-summary.json：低速全量复核，不覆盖前一轮。',
        '- baseline/、learning-requests/、performance/、pytest-313.log、pytest-312.log：固定基线与离线验证材料。', '',
        '仓库只提交手写冻结语料、实验工具、汇总报告、聚合 CSV 和图；完整正文／逐查询返回／原始模型输出不提交。'])
    validation=root/'validation.md'
    text='\n'.join(lines)+'\n'
    text=text.replace('<!-- VALIDATION -->',validation.read_text(encoding='utf-8') if validation.exists() else '验证尚在进行，发布前填写。')
    (report_dir/(stem+'.md')).write_text(text,encoding='utf-8')
    (root/'report-accounting.json').write_text(json.dumps({'calls':calls,'embedding_tokens':embed_tokens,'chat_prompt_tokens':total_in,'chat_completion_tokens':total_out},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(report_dir/(stem+'.md')),'candidates':len(trials),'metrics_rows':len(trials)*len(chosen['groups'])},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--artifacts',type=Path,required=True);p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--plots',action='store_true')
    a=p.parse_args();main(a.artifacts.resolve(),a.report_dir.resolve(),a.plots)
