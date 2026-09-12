"""Offline human-review packet with actual results and explicitly authored labels.

The exporter leaves every human judgment empty. Metrics have a declared future
denominator but no computed relevance values until the user submits annotations.
"""
import hashlib
import json
from pathlib import Path
from tests.information.lexical_quality import review_cases


def packet(root: Path) -> dict[str, object]:
    corpus = json.loads((root / 'quality-corpus.json').read_text())
    documents = corpus['documents']; identities = corpus['identities']
    observations = json.loads((root / 'create-observations.json').read_text()) + json.loads((root / 'reopen-observations.json').read_text())
    rows: list[dict[str, object]] = []
    for case in review_cases():
        eligible = []
        for document in documents:
            if case['subject'] is not None and identities[case['subject']] not in document['subject_ids']: continue
            if case['world'] is not None and (document['world_kind'] != case['world'] or case['world'] != 'REAL' and document['world_context'] != identities['scene']): continue
            eligible.append(document['document_id'])
        suggested = case['suggested_relevant']
        if not set(suggested).issubset(eligible): raise RuntimeError('Authored suggestions violate the explicit structural scope.')
        rows.append(dict(case) | {'eligible_documents': eligible,
            'structurally_excluded_documents': [item['document_id'] for item in documents if item['document_id'] not in eligible],
            'author_suggested_relevant': suggested,
            'author_suggested_nonrelevant': [item for item in eligible if item not in suggested],
            'label_source': 'AUTHOR_SUGGESTIONS_ONLY', 'human_relevant': None, 'human_nonrelevant': None,
            'human_reviewed': False, 'human_notes': '',
            'observations': [item for item in observations if item['case_id'] == case['case_id']]})
    material: dict[str, object] = {'version': 1, 'review_status': 'PENDING_USER_REVIEW', 'reviewer': None,
        'relevance_metrics': None, 'documents': documents, 'identities': identities, 'cases': rows,
        'original_pairs_sha256': corpus['source_pairs_sha256'],
        'metric_definitions': {'precision_at_5': '每组前5名相关数/5；不足5名仍除以5，宏平均含人工确认的零相关组。',
            'recall_at_8': '每组前8名相关数/人工相关集大小；空相关集为null，不混入宏平均。',
            'mrr_at_8': '公开前8名中首个相关结果倒数；未出现为0。只报告MRR@8，不声称全部内部排序的MRR。',
            'zero_relevant': '人工确认相关集为空的组另报数量和零返回率。',
            'candidate_truncation_ratio': '存在CANDIDATE_LIMIT的查询数/全部查询数；INDEX_LAG_PARTIAL单列，返回8条本身不算候选截断。',
            'strata': 'dirty/active/reopened各自计算；原60组与新增边界组分别计算，不能混为已通过正式中文质量。'},
        'limitations': ['作者单目标及其补集只是可修改建议，未由人工标注。', '同义、简繁、否定不具有语义理解保证。',
            '主体缺失不推断同一人；上下文名REAL不等于现实世界。', '历史记忆与当前目标分区，不把目标计入记忆检索指标。']}
    fingerprint = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    material['packet_sha256'] = fingerprint
    return material


def export_review(root: Path, destination: Path) -> str:
    """Write one portable HTML and its complete JSON; never calculate relevance."""
    destination.mkdir(parents=True, exist_ok=True)
    data = packet(root)
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    (destination / 'review-packet.json').write_text(encoded + '\n')
    template = Path(__file__).with_name('lexical_review.html').read_text()
    (destination / 'review.html').write_text(template.replace('REVIEW_PACKET_DATA', encoded.replace('<', '\\u003c')))
    (destination / 'annotation-template.json').write_text(json.dumps({'packet_sha256': data['packet_sha256'],
        'reviewer': None, 'review_status': 'PENDING_USER_REVIEW', 'relevance_metrics': None,
        'cases': [{'case_id': case['case_id'], 'relevant_documents': None, 'nonrelevant_documents': None,
                   'reviewed': False, 'notes': ''} for case in review_cases()]}, ensure_ascii=False, indent=2) + '\n')
    summary = ['# 人工相关性审阅材料', '', '**尚未取得人工标注，正式相关性指标未计算。**', '',
        '打开同目录 review.html。可逐组查看完整68对象、三种索引状态的真实前8名、人物／世界边界及独立目标区。',
        '作者建议与人工标签分开；每组可采用建议后修改并确认。完成后导出标注JSON，手动交回用户会话。页面不联网、不自动提交、不计算正式指标。', '',
        '原60组语料未改；新增13组边界例。全部73组仍待人工审核，空相关集也需要明确确认。', '',
        '口径：P@5固定除以5；R@8按完整人工相关集，空集单列；MRR@8只看公开前8条；候选截断按CANDIDATE_LIMIT，不能把返回上限8条当候选截断。', '',
        'JSON保留全部正式对象、明确主体ID／世界身份、实际请求／响应、作者相关与不相关建议全集、空白人工标签和材料指纹。', '',
        '| 组 | 查询 | 审阅重点 | 作者建议相关对象（未人工确认） |', '| --- | --- | --- | --- |']
    for case in review_cases(): summary.append('| ' + ' | '.join((case['case_id'], case['query_text'], case['purpose'], ', '.join(case['suggested_relevant']) or '空集')) + ' |')
    summary.extend(('', '材料SHA256：`' + str(data['packet_sha256']) + '`', ''))
    (destination / 'README.md').write_text('\n'.join(summary))
    return str(data['packet_sha256'])
