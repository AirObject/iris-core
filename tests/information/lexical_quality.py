"""Retained real query observations for authored, still-unreviewed relevance.

The original sixty pairs remain unchanged. Additional corpus members make
person, world and semantic limitations inspectable. Suggested labels are never
human labels, and this producer computes no relevance metrics.
"""
import hashlib
import json
from pathlib import Path
import time
from typing import TypedDict
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput, SubjectProposal, FactProposal
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.content_codec import encode_content
from tests.information.host_support import host
from tests.information.local_profile import inspect_database
from tests.information.publication_support import build, index_port, publish
from tests.information.test_queries import query
from tests.runtime.configuration_support import event


class ReviewCase(TypedDict):
    case_id: str
    query_text: str
    purpose: str
    suggested_relevant: list[str]
    subject: str | None
    world: str | None
    include_goals: bool


def literal_pairs() -> list[tuple[str, str]]:
    raw = json.loads(Path(__file__).with_name('lexical_cases.json').read_text())
    if type(raw) is not list or len(raw) != 60: raise RuntimeError('Expected the unchanged sixty-pair material.')
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if type(item) is not list or len(item) != 2 or any(type(value) is not str for value in item):
            raise RuntimeError('Expected explicit authored query/body pairs.')
        pairs.append((item[0], item[1]))
    return pairs


def review_cases() -> list[ReviewCase]:
    result: list[ReviewCase] = []
    def add(key: str, query_text: str, purpose: str, relevant: list[str], *, subject: str | None = None,
            world: str | None = None, goals: bool = False) -> None:
        result.append({'case_id': key, 'query_text': query_text, 'purpose': purpose,
            'suggested_relevant': relevant, 'subject': subject, 'world': world, 'include_goals': goals})
    for index, (literal, _) in enumerate(literal_pairs()):
        add('literal-' + str(index + 1).zfill(2), literal, '原作者指定字面目标；其他候选仍需人工判断。', ['literal-' + str(index + 1).zfill(2)])
    add('exhibition-phrase', '北京的展览', '原文“周末去北京看展”；词面不完全一致。', ['literal-01'])
    add('first-real-person', '北京', '同名人物：sample_platform/first，现实世界。', ['real-first'], subject='first', world='REAL')
    add('second-real-person', '北京', '同名人物：other_platform/second，现实世界；不推断同一人。', ['real-second'], subject='second', world='REAL')
    add('fictional-person', '北京', '同一明确主体，虚构世界；上下文标签REAL不等于现实世界。', ['fiction'], subject='first', world='FICTIONAL')
    add('roleplay-person', '北京', '同一明确主体，角色扮演世界。', ['role'], subject='first', world='ROLEPLAY')
    add('negation', '没有去北京', '否定原文应可找到；词法命中不代表肯定去过。', ['real-first'], subject='first', world='REAL')
    add('historical-intent', '北京', '历史表态与当前目标分区显示；记忆不自动成为当前目标。', ['past-intent'], goals=True)
    add('synonym-limit', '伤心', '同义表达局限单列；不能把未召回难过解释为语义无关。', ['sad', 'hurt'])
    add('traditional-limit', '台北', '简繁不自动转换；传统字样本另列。', ['traditional'])
    add('fullwidth', 'ＡＩ', '全角与ASCII归一化，但不改正式正文。', ['literal-02'])
    add('single-character', '雨', '单字可能匹配多个相关对象，不能只按作者单目标评分。', ['literal-04', 'literal-12', 'literal-50'])
    add('negative-preference', '不喜欢榴莲', '否定偏好不因词法匹配变为喜欢。', ['literal-49'])
    add('true-zero', '火星洋葱', '本材料内的作者建议零相关；等待人工确认空集。', [])
    return result


def graph() -> SyntheticGraphInput:
    return SyntheticGraphInput('quality_boundaries', (
        SubjectProposal('first', 'PLATFORM_PERSON', '同名人物', 'sample_platform', 'first'),
        SubjectProposal('second', 'PLATFORM_PERSON', '同名人物', 'other_platform', 'second'),
        SubjectProposal('scene', 'CONTEXT', 'REAL', None, None),
        FactProposal('real_first', '没有去北京看展', ('first',), 'REAL', None),
        FactProposal('real_second', '周末去北京看展', ('second',), 'REAL', None),
        FactProposal('fiction', '周末去北京看展', ('first',), 'FICTIONAL', 'scene'),
        FactProposal('role', '周末去北京看展', ('first',), 'ROLEPLAY', 'scene'),
    ), 50)


async def create_corpus(root: Path) -> None:
    """Every bounded candidate group uses a newly opened real host and original keys."""
    pairs = literal_pairs(); documents: list[dict[str, object]] = []; identities: dict[str, str] = {}
    candidates = [SyntheticCandidateInput('review_literals:' + str(group), tuple(body for _, body in pairs[start:start + 8]), 50)
                  for group, start in enumerate(range(0, 60, 8))]
    semantic_bodies = ('他今天很难过', '她说今天伤心', '昨天说去北京，但那是历史表态。', '計劃去臺北看展')
    sources = [*candidates, graph(), SyntheticCandidateInput('review_semantics', semantic_bodies, 50)]
    for group, source in enumerate(sources):
        h = host(root); h.candidates = source
        try:
            if type(await h.initialize('CREATE_NEW' if not group else 'OPEN_EXISTING')) is not Found:
                raise RuntimeError('Actual corpus initialization failed.')
            if not group and type(await h.register_entry('register', 'entry', 'host', 'sample_platform', 'conversation')) is not Committed:
                raise RuntimeError('Actual corpus entry registration failed.')
            if h.runtime is None or h.retrieval is None: raise RuntimeError('Expected actual corpus owners.')
            entry = h.runtime.bind_entry('entry')
            for offset in range(3 if not group else 2):
                key = 'review-input:' + str(group) + ':' + str(offset)
                value = event(key); value['event_version'] = 2
                if type(await entry.accept_event(key, value)) is not Committed: raise RuntimeError('Actual corpus input failed.')
            result = await entry.run_learning('review-learn:' + str(group))
            if type(result) is not Committed: raise RuntimeError('Actual corpus learning failed: ' + repr(result))
            refs = record(result.receipt.result)['object_refs']
            if type(refs) is not tuple: raise RuntimeError('Expected actual object references.')
            for reference in refs:
                oid = text(record(reference)['object_id']); current = await h.retrieval.memory.index_current(oid)
                if current is None: raise RuntimeError('A committed formal object is missing.')
                content = record(current['content']); body = text(content['body']); world = record(content['world_scope'])
                subjects = content['subject_ids']
                if type(subjects) is not tuple: raise RuntimeError('Expected explicit subject identities.')
                if group < 8:
                    alias = 'literal-' + str(next(i + 1 for i, (_, value) in enumerate(pairs) if value == body)).zfill(2)
                elif group == 8:
                    alias = 'real-first' if body.startswith('没有') else 'real-second' if world['kind'] == 'REAL' else 'fiction' if world['kind'] == 'FICTIONAL' else 'role'
                    if alias in ('real-first', 'real-second'): identities['first' if alias == 'real-first' else 'second'] = text(subjects[0])
                    else: identities['scene'] = text(world['context_id'])
                else: alias = ('sad', 'hurt', 'past-intent', 'traditional')[semantic_bodies.index(body)]
                documents.append({'document_id': alias, 'object_id': oid, 'body': body, 'subject_ids': list(subjects),
                    'world_kind': world['kind'], 'world_context': world['context_id'], 'revision': current['revision'],
                    'formal_object': json.loads(encode_content(current, 4096))})
            if group == len(sources) - 1:
                goals = await h.bind_management(HostIdentity('review-goal', 'principal', 'host', 'entry',
                    frozenset(('goal_inject_external',)), (), time.monotonic() + 300))
                result = await goals.execute('goal_inject_external', 'current-plan', {'content': '现在目标：明天去北京看展',
                    'subject_ids': (), 'world_scope': 'REAL', 'deadline': None, 'reminder_lead_seconds': None, 'route_id': None,
                    'source_id': 'review-external-current-plan'})
                if type(result) is not Committed: raise RuntimeError('Actual independent current goal failed.')
        finally:
            if not await h.close(): raise RuntimeError('Corpus group still owns actual resources.')
    if len(documents) != 68 or len({item['document_id'] for item in documents}) != 68:
        raise RuntimeError('The real corpus does not contain all authored documents.')
    (root / 'quality-corpus.json').write_text(json.dumps({'documents': documents, 'identities': identities,
        'source_pairs_sha256': hashlib.sha256(Path(__file__).with_name('lexical_cases.json').read_bytes()).hexdigest(),
        'human_review_status': 'PENDING', 'label_source': 'AUTHOR_SUGGESTIONS_ONLY'}, ensure_ascii=False, indent=2) + '\n')


async def capture_quality(root: Path, action: str) -> dict[str, object]:
    """Capture actual dirty, active and reopened results without assigning relevance."""
    if action == 'create': await create_corpus(root)
    elif action != 'reopen': raise ValueError('Expected create or reopen.')
    corpus = json.loads((root / 'quality-corpus.json').read_text()); identities = corpus['identities']
    object_map = {item['object_id']: item['document_id'] for item in corpus['documents']}
    h = host(root); observations: list[dict[str, object]] = []
    try:
        if type(await h.initialize('OPEN_EXISTING')) is not Found: raise RuntimeError('Actual corpus reopen failed.')
        calls = h.adapter.calls
        for phase in (('dirty', 'active') if action == 'create' else ('reopened',)):
            if phase == 'active':
                native = await index_port(h); _, generation = await build(h, native, 'quality-index')
                await publish(native, 'quality-publish', generation)
            port = await h.bind_query(HostIdentity('review-' + phase, 'principal', 'host', 'entry',
                frozenset(('search_memory',)), (), time.monotonic() + 300))
            for case in review_cases():
                world = case['world']
                world_wire = world if world in (None, 'REAL') else world + ':' + identities['scene']
                selected_subjects = () if case['subject'] is None else (identities[case['subject']],)
                request = query(phase + ':' + case['case_id'], query_text=case['query_text'], subject_ids=selected_subjects,
                    world_scope=world_wire, include_state=False, include_goals=case['include_goals'], require_complete=phase != 'dirty')
                result = await port.search_memory(request)
                if type(result) is not Found: raise RuntimeError('Actual review query failed: ' + repr(result))
                response = record(result.value); memories = record(response['sections'])['memories']
                if type(memories) is not tuple or len(memories) > 8: raise RuntimeError('Query exceeded its public result bound.')
                returned = []
                for item in memories:
                    current = record(item); oid = text(current['object_id'])
                    if oid not in object_map: raise RuntimeError('Query returned an object outside the retained corpus.')
                    content = record(current['content']); actual_world = record(content['world_scope'])
                    if world is not None and (actual_world['kind'] != world or world != 'REAL' and actual_world['context_id'] != identities['scene']):
                        raise RuntimeError('Actual world filter leaked across its boundary.')
                    subjects = content['subject_ids']
                    if selected_subjects and (type(subjects) is not tuple or selected_subjects[0] not in subjects):
                        raise RuntimeError('Actual subject filter leaked across its boundary.')
                    returned.append(object_map[oid])
                observations.append({'phase': phase, 'case_id': case['case_id'], 'returned_documents': returned,
                    'request': request, 'response': json.loads(encode_content(response, 131072))})
            if h.queries is None: raise RuntimeError('Expected real query capability owner.')
            h.queries.revoke(port)
        if calls or h.adapter.calls != calls: raise RuntimeError('Query, index or recovery called a model adapter.')
    finally:
        if not await h.close(): raise RuntimeError('Actual corpus resources did not finish closing.')
    prior = [] if action == 'create' else json.loads((root / 'create-observations.json').read_text())
    all_observations = [*prior, *observations]
    baseline: dict[str, object] = {}
    for observation in all_observations:
        key = observation['case_id']; returned = observation['returned_documents']
        if type(key) is not str: raise RuntimeError('Expected a stable review case identity.')
        if key in baseline and baseline[key] != returned: raise RuntimeError('Actual ranking changed after index publication or cold reopen.')
        baseline[key] = returned
    (root / (action + '-observations.json')).write_text(json.dumps(observations, ensure_ascii=False, indent=2) + '\n')
    return {'action': action, 'documents': 68, 'queries': len(review_cases()), 'observations': len(observations),
        'relevance_metrics': None, 'human_review_status': 'PENDING', 'model_calls': 0,
        'database': inspect_database(root), 'storage_execution': 'ACTUAL', 'candidate_origin': 'SYNTHETIC'}
