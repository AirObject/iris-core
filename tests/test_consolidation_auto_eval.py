"""auto_v1 evaluation tooling on the public corpus with fake models; no quality claims."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1]/'evals/consolidation_eval'
for name in ('run', 'score', 'auto_score'):
    if name not in sys.modules or not hasattr(sys.modules[name], '__file__') or Path(sys.modules[name].__file__).parent != DIR:
        spec = importlib.util.spec_from_file_location(name, DIR/f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
runner, scorer = sys.modules['run'], sys.modules['auto_score']

from iris.models import ModelReply
from test_consolidation import Model

CORPUS = runner.ROOT/'evals/consolidation_v2.json'


@pytest.fixture(scope='module')
def corpus():
    return {c['id']: c for c in runner.load_corpus(CORPUS)}


def execute(case, tmp_path, answer=None):
    return runner.case_run(case, tmp_path/(case['id']+'.db'), lambda store, clock: Model(store, answer), 'broad', 'auto_v1')


def metadata(cases):
    return {'source_sha256': runner.source_hash(), 'resolution': 'auto_v1', 'corpus': {'sha256': runner._json_sha256(cases)}}


def by_key(row, key):
    keymap = row['input']['before']['key_map']
    return next(m for m in row['input']['after']['memories'] if m['id'] == keymap[key])


class LabelModel:
    """Answers from the corpus labels, so a mismatch between rules and labels shows up here."""
    health = None

    def __init__(self, store, case):
        self.case, self.keymap = case, {}
        with store.read() as conn:
            ids = [r[0] for r in conn.execute('SELECT id FROM memories ORDER BY id')]
        self.keymap = {m['key']: ids[i] for i, m in enumerate(case['memories'])}
        self.reverse = {v: k for k, v in self.keymap.items()}

    def label(self, payload):
        keys = {self.reverse[i] for i in payload['pair_ids']}
        return next((l for l in self.case['expected'].get('auto_contradictions', []) if set(l['keys']) == keys), None)

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        system, payload = messages[0]['content'], json.loads(messages[1]['content'])
        visible = [s['id'] for m in payload['memories'] for s in m['sources']]
        if '矛盾判定器' in system:
            label = self.label(payload)
            value = {'relation': label['relation'], 'old_id': self.keymap[label['old']], 'new_id': self.keymap[label['new']],
                     'evidence': visible, 'reason': '依据原话判定'}
        elif '依赖复核器' in system:
            losses = {l['id'] for l in payload['losses']}
            value = {'independent_support_ids': [m['id'] for m in payload['memories']
                                                 if m['id'] != payload['target_id'] and m['lifecycle'] == 'active' and m['id'] not in losses],
                     'reason': '复核依据'}
        elif self.label(payload):
            value = {'decision': 'conflict', 'reason': '双方说法不同', 'evidence': visible[:1], 'updates': [], 'unique_details': {'a': [], 'b': []}}
        else:
            value = {'decision': 'separate', 'reason': '不同事实', 'evidence': [], 'updates': [], 'unique_details': {'a': [], 'b': []}}
        return ModelReply(json.dumps(value, ensure_ascii=False), 'stop', {'prompt_tokens': 1, 'completion_tokens': 1})


def test_rules_execute_every_public_contradiction_and_dependency_label(corpus, tmp_path):
    results = []
    for case in corpus.values():
        row = runner.case_run(case, tmp_path/(case['id']+'.db'), lambda store, clock: LabelModel(store, case), 'broad', 'auto_v1')
        assert runner.run_validity(row)['valid'], case['id']
        assert not [i for i in row['report']['items'] if i['outcome'] == 'failed'], case['id']
        scored = scorer.score_case(row)
        assert scored['write_damage'] == [] and scored['unexpected_actions'] == [], case['id']
        results += [item for item in scored['items'] if item['kind'] in ('contradiction', 'dependency')]
    assert len(results) == 34 and {item['result'] for item in results} == {scorer.CORRECT}


def test_public_corpus_loads_and_every_case_runs_with_a_fake_model(corpus, tmp_path):
    assert len(corpus) == 24
    for case in corpus.values():
        row = execute(case, tmp_path)
        assert row['input']['forget_threshold'] == 20
        sys.modules['score'].validate_payload(row['input'])
        scorer.score_case(row)


def test_loader_rejects_labels_for_unknown_memories(corpus, tmp_path):
    case = copy.deepcopy(corpus['CV001'])
    case['expected']['auto_dependencies'] = [{'derived': 'zz', 'expected': 'keep'}]
    path = tmp_path/'bad.json'
    path.write_text(json.dumps({'cases': [case]}, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='unknown memory'):
        runner.load_corpus(path)


def test_declared_entry_visibility_is_applied_when_seeding(corpus, tmp_path):
    row = execute(corpus['CV012'], tmp_path)
    assert row['report']['consolidation']['skip_reason'] is None
    checks = {tuple(c['pair_keys']) for c in row['input']['merge_checks']}
    assert ('x', 'y') not in checks and ('y', 'x') not in checks


def manual_row(row, **after_fields):
    """A row whose after-state is edited directly, so scoring can be tested alone."""
    edited = copy.deepcopy(row)
    keymap = edited['input']['before']['key_map']
    for key, changes in after_fields.items():
        memory = next(m for m in edited['input']['after']['memories'] if m['id'] == keymap[key])
        memory.update(changes)
    return edited


def contradiction_row(corpus, tmp_path, cid):
    return execute(corpus[cid], tmp_path)


def result_for(report, **match):
    return next(i['result'] for i in report['items'] if all(i.get(k) == v for k, v in match.items()))


def annotation(kind, superseded_by=None):
    return {'id': 900, 'kind': kind, 'superseded_by': superseded_by, 'source_keys': [], 'text': 't', 'evidence': []}


def test_supersedes_scoring_direction(corpus, tmp_path):
    row = contradiction_row(corpus, tmp_path, 'CV001')
    assert result_for(scorer.score_case(row), kind='contradiction') == scorer.MISSED
    new_id = row['input']['before']['key_map']['b']
    good = manual_row(row, a={'belief': 49, 'retention': 19, 'lifecycle': 'forgotten', 'annotations': [annotation('superseded', new_id)]})
    assert result_for(scorer.score_case(good), kind='contradiction') == scorer.CORRECT
    backwards = manual_row(row, b={'belief': 49, 'lifecycle': 'forgotten', 'retention': 19})
    assert result_for(scorer.score_case(backwards), kind='contradiction') == scorer.WRONG
    wrong_pointer = manual_row(row, a={'belief': 49, 'retention': 19, 'lifecycle': 'forgotten', 'annotations': [annotation('superseded', 123456)]})
    assert result_for(scorer.score_case(wrong_pointer), kind='contradiction') == scorer.MISSED
    assert scorer.score_case(wrong_pointer)['write_damage'] and scorer.score_case(good)['write_damage'] == []


def test_reported_and_undecided_scoring(corpus, tmp_path):
    reported = contradiction_row(corpus, tmp_path, 'CV002')
    good = manual_row(reported, a={'belief': 89, 'annotations': [annotation('disputed')]})
    assert result_for(scorer.score_case(good), kind='contradiction') == scorer.CORRECT
    forgotten = manual_row(reported, a={'belief': 49, 'lifecycle': 'forgotten', 'retention': 19})
    assert result_for(scorer.score_case(forgotten), kind='contradiction') == scorer.WRONG
    undecided = contradiction_row(corpus, tmp_path, 'CV005')
    both = manual_row(undecided, a={'annotations': [annotation('disputed')]}, b={'annotations': [annotation('disputed')]})
    assert result_for(scorer.score_case(both), kind='contradiction') == scorer.CORRECT
    moved = manual_row(undecided, a={'belief': 10})
    assert result_for(scorer.score_case(moved), kind='contradiction') == scorer.WRONG


def test_dependency_scoring_and_protection(corpus, tmp_path):
    row = contradiction_row(corpus, tmp_path, 'CV012')
    assert result_for(scorer.score_case(row), kind='dependency') == scorer.MISSED
    assert all(check['passed'] for check in scorer.score_case(row)['protection'])
    before = next(m for m in row['input']['before']['memories'] if m['id'] == row['input']['before']['key_map']['d'])
    good = manual_row(row, d={'belief': min(before['belief'], 49), 'lifecycle': 'forgotten', 'retention': min(before['retention'], 19),
                              'annotations': [annotation('insufficient_support')]})
    assert result_for(scorer.score_case(good), kind='dependency') == scorer.CORRECT
    touched = manual_row(row, x={'belief': 1})
    assert not all(check['passed'] for check in scorer.score_case(touched)['protection'])


def test_unexpected_changes_count_against_handling(corpus, tmp_path):
    row = contradiction_row(corpus, tmp_path, 'CV001')
    edited = manual_row(row, c={'belief': 1})
    result = scorer.score_case(edited)
    assert [item['kind'] for item in result['unexpected_actions']] == ['unexpected_change']


def test_materials_detail_rounds_and_gates(corpus, tmp_path):
    case = corpus['CV001']
    row = execute(case, tmp_path)
    rows = [row]
    manifest = runner.export_auto(rows, tmp_path/'materials', metadata([case]))
    assert json.loads((tmp_path/'materials/manifest.json').read_text(encoding='utf-8'))['evaluation'] == 'consolidation_auto'
    report = scorer.score(tmp_path/'materials', tmp_path/'out0')
    assert report['detail_loss'] is None and report['gates']['detail_loss_zero'] is None
    assert report['call_counts']['calls'] == len(row['calls'])
    # A merge item with labels: inject a real one and re-export so the rounds have work.
    merged = copy.deepcopy(row)
    p = merged['input']
    keymap = p['before']['key_map']
    before = {m['id']: m for m in p['before']['memories']}
    c, d = before[keymap['c']], before[keymap['d']]
    result = {**d, 'content': c['content']+'，在北门等', 'original_keys': ['c', 'd'], 'sources': c['sources']+d['sources']}
    absorbed = {**c, 'lifecycle': 'deleted', 'merged_into': d['id'], 'is_placeholder': True}
    p['actual_merges'] = [{'action_id': 'co-1', 'input_keys': ['c', 'd'], 'input_ids': [d['id'], c['id']],
                           'before_memories': [c, d], 'result': result, 'absorbed_memories': [absorbed]}]
    for memory in p['after']['memories']:
        if memory['id'] == d['id']:
            memory.update(content=result['content'], original_keys=['c', 'd'], sources=result['sources'])
        if memory['id'] == c['id']:
            memory.update(lifecycle='deleted', merged_into=d['id'], is_placeholder=True)
    p['merge_checks'] = []
    merged['work_results'] = []
    runner.export_auto([merged], tmp_path/'m2', metadata([case]))
    items = runner.auto_detail_items(p)
    assert len(items) == 1 and items[0]['pair'] == ['c', 'd'] and items[0]['must_keep']
    manifest = json.loads((tmp_path/'m2/manifest.json').read_text(encoding='utf-8'))
    def write_round(name, keep_flag):
        directory = tmp_path/name
        directory.mkdir()
        (directory/'manifest.json').write_text(json.dumps({'materials_sha256': manifest['materials_sha256']}), encoding='utf-8')
        judgment = {'format_version': 1, 'case_id': case['id'], 'merge_detail_results': [
            {'action_id': 'co-1', 'must_keep_present': [keep_flag]*len(items[0]['must_keep']),
             'must_not_add_absent': [True]*len(items[0]['must_not_add']), 'reason': '逐项核对'}]}
        (directory/'0000.json').write_text(json.dumps(judgment), encoding='utf-8')
        return directory
    agree = scorer.score(tmp_path/'m2', tmp_path/'out1', detail_judgments=[write_round('r1', True), write_round('r2', True)], judge_model='judge')
    assert agree['detail_loss'] == {'numerator': 0, 'denominator': 1, 'rate': 0} and agree['gates']['detail_loss_zero'] is True
    split = scorer.score(tmp_path/'m2', tmp_path/'out2', detail_judgments=[write_round('r3', True), write_round('r4', False)], judge_model='judge')
    assert split['detail_loss']['numerator'] == 1 and split['detail_disagreements'] and split['gates']['detail_loss_zero'] is False
    assert split['mishandling']['rate'] is not None and agree['protection'] == split['protection']


def test_tampered_materials_are_refused(corpus, tmp_path):
    case = corpus['CV001']
    runner.export_auto([execute(case, tmp_path)], tmp_path/'materials', metadata([case]))
    path = tmp_path/'materials/cases/0000.json'
    path.write_text(path.read_text(encoding='utf-8')+' ', encoding='utf-8')
    with pytest.raises(ValueError):
        scorer.load_materials(tmp_path/'materials')


def test_detail_judgment_requires_booleans_and_matching_length(corpus, tmp_path):
    items = [{'action_id': 'co-1', 'must_keep': ['a'], 'must_not_add': []}]
    good = {'format_version': 1, 'case_id': 'x', 'merge_detail_results': [{'action_id': 'co-1', 'must_keep_present': [True], 'must_not_add_absent': [], 'reason': 'r'}]}
    scorer.validate_detail_judgment(good, items, 'x')
    bad = copy.deepcopy(good)
    bad['merge_detail_results'][0]['must_keep_present'] = [1]
    with pytest.raises(ValueError, match='boolean'):
        scorer.validate_detail_judgment(bad, items, 'x')
    bad = copy.deepcopy(good)
    bad['merge_detail_results'][0]['must_keep_present'] = []
    with pytest.raises(ValueError, match='length'):
        scorer.validate_detail_judgment(bad, items, 'x')
