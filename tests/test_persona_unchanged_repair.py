"""An unchanged failed claim cannot gain a fresh check by swapping scene deixis."""
import json

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import PersonaEngine, current_persona, select_evidence
from test_persona import FakeGateway, add_self, store


class OptimisticRepair(FakeGateway):
    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        request = self.calls[-1][1]
        value = json.loads(reply.content)
        if purpose == 'persona_check':
            for row, sentence in zip(value['sentences'], request['candidate']):
                if sentence['origin'] == 'memory' and not sentence['initial_setting']:
                    row.update(supported=False, fabricated=True, reason='原句所断言的结果没有发生记录。')
                    for part in row['clauses']:
                        part.update(supported=False, reason='本主张不受来源支持。')
        elif purpose == 'persona_sentence_repair':
            value = {'repairs': [{'index': p['index'],
                'text': request['replacement_text_by_ref'][p['allowed_replacement_refs'][0]]}
                for p in request['problems']]}
        return ModelReply(dumps(value), reply.finish_reason, reply.usage)


def run(store, original, replacement):
    mid = add_self(store, replacement)
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    model = OptimisticRepair(store, {'sentences': [{'text': original, 'basis': [ref]}]})
    result = PersonaEngine(store, model).regenerate(expected_version=current_persona(store)['id'])
    return result, model


@pytest.mark.parametrize('opening', ['那次', '在那次', '这次', '在这一次'])
def test_a_failed_claim_is_not_retried_after_only_changing_scene_deixis(store, opening):
    original = opening + '演练中，我没有翻看提示卡。'
    replacement = '在这次演练中，我没有翻看提示卡。'
    result, model = run(store, original, replacement)
    assert result['version']['checks']['passed']
    assert not any(purpose == 'persona_sentence_repair' for purpose, _ in model.calls)
    assert '没有翻看提示卡' not in result['version']['content']
    rejected = result['version']['checks']['repair_options_rejected']
    assert any('unchanged failed claim' in ' '.join(row['reasons']) for row in rejected)
    assert rejected[0]['option']['text'] == replacement
    rechecks = [payload for purpose, payload in model.calls if purpose == 'persona_recheck']
    assert rechecks and rechecks[0]['task'] == 'degree_only'


def test_exact_unchanged_option_is_filtered_before_calling_the_selector(store):
    text = '在那次演练中，我没有翻看提示卡。'
    result, model = run(store, text, text)
    assert not any(purpose == 'persona_sentence_repair' for purpose, _ in model.calls)
    assert '没有翻看提示卡' not in result['version']['content']


@pytest.mark.parametrize('original', [
    '那次演练中，我没有翻看提示卡。',
    '演练中，我把提示卡摆到了桌边。',
])
def test_a_changed_claim_or_new_bounded_scope_still_gets_semantic_recheck(store, original):
    replacement = '在这次演练中，我把提示卡摆到了桌边。'
    result, model = run(store, original, replacement)
    assert result['version']['checks']['passed']
    assert replacement in result['version']['content']
    assert any(purpose == 'persona_sentence_repair' for purpose, _ in model.calls)
    assert result['version']['checks']['repaired_sentences']
    assert any(payload['candidate'] for purpose, payload in model.calls if purpose == 'persona_recheck')
