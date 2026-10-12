"""Graceful interruption preserves finished model replies in the active stage."""
import json

import pytest

from iris.persona import PersonaEngine, current_persona, select_evidence
from test_persona import FakeGateway, add_self, store


def test_interruption_keeps_partial_check_replies_and_never_publishes(store):
    text = '那次共读时，我核对了目录，也标注了页码。'
    mid = add_self(store, text)
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    class Interrupted(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            if purpose == 'persona_check' and any(p == purpose for p, _ in self.calls):
                raise KeyboardInterrupt
            return super().chat(messages, purpose, **kwargs)
    model = Interrupted(store, {'sentences': [{'text': text, 'basis': [ref]}]})
    with pytest.raises(KeyboardInterrupt):
        PersonaEngine(store, model).regenerate(expected_version=1)
    assert current_persona(store)['id'] == 1
    with store.read() as conn:
        attempt = dict(conn.execute('SELECT * FROM persona_attempts ORDER BY id DESC LIMIT 1').fetchone())
    assert (attempt['state'], attempt['reason']) == ('failed', 'interrupted')
    outputs = json.loads(attempt['outputs_json'])
    assert len(outputs['persona_check_batches']) == 2
    assert json.loads(outputs['persona_check_batches'][0]['persona_check'])['sentences']
    assert outputs['persona_check_batches'][1] == {}  # No invented reply to the interrupted call.
